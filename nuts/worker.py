from uuid import uuid4
import os
import json
import random
from typing import Any
from redis import Redis
from .job import NutsJob, JobModule, JobRegistry
from .workflow import NutsWorkflow
from .cron import Cron
from .concurrency import ConcurrencyGate
from . import dispatch
from . import instance
import datetime
import logging
import yaml


class RetryPolicy:
    '''Exponential backoff with jitter, capped, plus a max attempt count before DLQ.'''
    def __init__(self, base: float = 5, cap: float = 300, max_attempts: int = 5):
        self.base = base
        self.cap = cap
        self.max_attempts = max_attempts

    def delay(self, attempt: int) -> float:
        raw = min(self.cap, self.base * (2 ** (attempt - 1)))
        jitter = raw * 0.2
        return max(0.0, raw + random.uniform(-jitter, jitter))


class Worker():
    '''
        A NUTS worker.

        Delivery is backed by Redis Streams + consumer groups: entries
        aren't gone until XACK, a crashed worker's claim sits in the Pending
        Entries List and gets reclaimed via XAUTOCLAIM, and priority is a
        small number of discrete streams checked in strict order. Workflow
        instances are durable, per-entity documents in Redis (nuts/instance.py)
        rather than worker-memory objects, so any worker can compute "what's
        next" from persisted state alone.
    '''
    id: str
    redis: Redis
    scheduler: Cron
    logger: logging.Logger
    registry: JobRegistry
    workflow_templates: list[NutsWorkflow]
    concurrency: ConcurrencyGate
    retry_policy: RetryPolicy
    kwargs: dict[str, Any]
    should_run: bool
    is_leader: bool

    def __init__(self, redis: Redis, jobs: list[JobModule], workflow_directory: str = None, **kwargs):
        self.id = str(uuid4())

        # Cron/delay due-time tracking.
        self.scheduled_queue = 'nuts|jobs|scheduled'                 # bare cron job names, deduped by name
        self.scheduled_workflow_queue = 'nuts|workflows|scheduled'   # bare workflow template names, deduped by name
        self.delayed_queue = 'nuts|jobs|delayed'                     # anonymous JSON payloads (WorkQueue delay_seconds)
        self.retry_queue = 'nuts|retry|pending'                      # anonymous JSON payloads (backoff-pending retries)
        self.cancel_queue = 'nuts|jobs|cancel'

        self.consumer_group = kwargs.pop('consumer_group', 'nuts|workers')
        self.pel_min_idle_ms = kwargs.pop('pel_min_idle_ms', 60000)
        self.instance_ttl_seconds = kwargs.pop('instance_ttl_seconds', 604800)
        self.concurrency_lease_seconds = kwargs.pop('concurrency_lease_seconds', 300)
        self.retry_policy = kwargs.pop('retry_policy', RetryPolicy())

        self.kwargs = kwargs
        self.should_run = True
        self.is_leader = False

        self.last_run = datetime.datetime.fromtimestamp(0)

        self.redis = redis
        self.scheduler = Cron()
        self.concurrency = ConcurrencyGate(redis)

        self.logger = logging.getLogger(f'worker|{self.id}')
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        self.registry = JobRegistry(jobs)
        self.workflow_templates = []

        self._ensure_consumer_groups()

        self.check_leader()

        if self.is_leader and workflow_directory:
            self._load_workflow_templates(workflow_directory)

        if self.is_leader:
            self._register_cron_jobs()

    def _ensure_consumer_groups(self):
        for priority in dispatch.PRIORITIES:
            self._ensure_group(dispatch.stream_key(priority))

    def _ensure_group(self, stream_key: str):
        try:
            self.redis.xgroup_create(stream_key, self.consumer_group, id='0', mkstream=True)
        except Exception as ex:
            if 'BUSYGROUP' not in str(ex):
                raise

    def _load_workflow_templates(self, workflow_directory: str):
        self.logger.info(f'Worker {self.id} assuming leadership')
        for filename in os.listdir(workflow_directory):
            if not filename.endswith('.yaml'):
                continue
            with open(os.path.join(workflow_directory, filename), 'r') as f:
                raw = yaml.safe_load(f)

            self.logger.info(f'Registering Workflow {filename}')
            template = NutsWorkflow(**raw['workflow'])

            is_valid, error = template.validate()
            if not is_valid:
                self.logger.error(f'Invalid workflow {template.name}: {error}')
                continue

            self.workflow_templates.append(template)
            next_execution = self.scheduler.get_next_execution(template.schedule).timestamp()
            self._register_recurring(self.scheduled_workflow_queue, template.name, next_execution)

    def _register_cron_jobs(self):
        for definition in self.registry.definitions():
            if not definition.schedule:
                continue
            self.logger.info(f'Registering Cron Job {definition.name}')
            next_execution = self.scheduler.get_next_execution(definition.schedule).timestamp()
            self._register_recurring(self.scheduled_queue, definition.name, next_execution)

    def _register_recurring(self, zset_key: str, name: str, next_due_ts: float):
        '''
            Seed/update a bare-name due-time entry, deduping on name so a
            worker restart doesn't pile up duplicate entries or drift the
            next-fire time away from a freshly computed value.
        '''
        already_scheduled = False
        try:
            pending = self.redis.zscan(zset_key, match=name)
            if pending[1]:
                already_scheduled = True
                if pending[1][0][1] != next_due_ts:
                    self.redis.zrem(zset_key, name)
                    self.redis.zadd(zset_key, {name: next_due_ts})
        except Exception:
            pass

        if not already_scheduled:
            self.redis.zadd(zset_key, {name: next_due_ts})

    def _due_names(self, zset_key: str, now_ts: float) -> list[str]:
        candidates = self.redis.zrange(zset_key, start=0, end=now_ts, byscore=True)
        names = []
        for member in candidates:
            if self.redis.zrem(zset_key, member):
                names.append(member.decode())
        return names

    def check_leader(self) -> bool:
        leadership_check = self.redis.setnx('leader_id', self.id)

        if not leadership_check:
            leader_id = self.redis.get('leader_id')
            if leader_id is not None and leader_id.decode() == self.id:
                # Puts a 60 second life on the current leader so we aren't left in limbo if the leader worker dies ungracefully.
                self.redis.expire('leader_id', 60)

                self.is_leader = True
            else:
                self.is_leader = False

        else:
            # Puts a 60 second life on the current leader so we aren't left in limbo if the leader worker dies ungracefully.
            self.redis.expire('leader_id', 60)
            self.is_leader = True

    def shutdown(self, signum, frame):
        self.logger.info(f'Received shutdown command {signum}.')
        self.should_run = False
        self.release_leader()

    def release_leader(self):
        if self.is_leader:
            self.logger.info('Shutdown: Releasing leadership')
            self.redis.expire('leader_id', -1)

    def is_job_cancelled(self, job_name: str) -> bool:
        """Check if a cancellation has been requested for this job."""
        return bool(self.redis.sismember(self.cancel_queue, job_name))

    def clear_job_cancellation(self, job_name: str):
        """Clear the cancellation flag for a job."""
        self.redis.srem(self.cancel_queue, job_name)

    # ---- Leader-only: sole producer into the priority streams and DLQ ----

    def drain_due_cron_jobs(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for name in self._due_names(self.scheduled_queue, now):
            definition = self.registry.get(name)
            if definition is None:
                continue
            payload = dispatch.build_payload(name, {}, priority='normal', max_attempts=definition.max_attempts)
            dispatch.enqueue_payload(self.redis, payload)

    def drain_due_delayed_jobs(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for payload in dispatch.due_items(self.redis, self.delayed_queue, now):
            self._dispatch_or_requeue(payload, self.delayed_queue)

    def drain_due_retries(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for payload in dispatch.due_items(self.redis, self.retry_queue, now):
            self._dispatch_or_requeue(payload, self.retry_queue)

    def _dispatch_or_requeue(self, payload: dict, zset_key: str):
        key = payload.get('concurrency_key')
        if key:
            holder = self._holder_token(payload)
            if not self.concurrency.acquire(key, holder, payload.get('concurrency_limit', 1), self.concurrency_lease_seconds):
                # No slot free yet -- put it straight back, the next tick's
                # due_items() call will pick it up and retry the check.
                now = datetime.datetime.now(datetime.timezone.utc).timestamp()
                dispatch.dispatch_later(self.redis, zset_key, payload, now)
                return
        dispatch.enqueue_payload(self.redis, payload)

    def drain_due_workflow_cron(self):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for name in self._due_names(self.scheduled_workflow_queue, now):
            template = next((t for t in self.workflow_templates if t.name == name), None)
            if template is None:
                continue
            # Reschedule at drain time, not completion -- overlapping
            # instances of the same template are allowed by design, since
            # concurrent instances are the whole point of this redesign.
            instance.create_instance(self.redis, template.name, template.to_plan())
            next_execution = self.scheduler.get_next_execution(template.schedule).timestamp()
            self._register_recurring(self.scheduled_workflow_queue, template.name, next_execution)

    def sweep_active_workflow_instances(self):
        for raw_id in self.redis.smembers(instance.ACTIVE_SET):
            instance_id = raw_id.decode()
            doc = instance.get_instance(self.redis, instance_id)
            if doc is None:
                self.redis.srem(instance.ACTIVE_SET, raw_id)
                continue

            instance.resolve_wait_steps(self.redis, instance_id)
            doc = instance.get_instance(self.redis, instance_id)
            if doc is None:
                continue

            for step in instance.ready_steps(doc):
                if step['kind'] != 'job':
                    continue

                if step['concurrency_key']:
                    holder = f"{instance_id}:{step['step_id']}"
                    if not self.concurrency.acquire(step['concurrency_key'], holder, step['concurrency_limit'], self.concurrency_lease_seconds):
                        continue

                definition = self.registry.get(step['op'])
                max_attempts = step['max_attempts']
                if max_attempts is None:
                    max_attempts = definition.max_attempts if definition else NutsJob.max_attempts

                payload = dispatch.build_payload(
                    step['op'],
                    self._step_params(doc, step),
                    priority=step['priority'],
                    max_attempts=max_attempts,
                    instance_id=instance_id,
                    step_id=step['step_id'],
                    concurrency_key=step['concurrency_key'],
                    concurrency_limit=step['concurrency_limit'],
                )
                dispatch.enqueue_payload(self.redis, payload)
                self._mark_step(instance_id, step['step_id'], status='dispatched')

            doc = instance.get_instance(self.redis, instance_id)
            if doc is None:
                continue
            statuses = [s['status'] for s in doc['steps']]
            if all(s == 'completed' for s in statuses):
                instance.complete_instance(self.redis, instance_id, self.instance_ttl_seconds)
            elif 'failed' in statuses:
                instance.fail_instance(self.redis, instance_id, self.instance_ttl_seconds, error=doc.get('error'))

    def _step_params(self, doc: dict, step: dict) -> dict:
        '''
            Merge instance-level params with each completed prerequisite
            step's result, keyed by that step's step_id -- gives a step
            access to upstream results via **kwargs without a special
            naming convention.
        '''
        params = dict(doc.get('params') or {})
        for req in (step.get('requires') or []):
            req_step = next((s for s in doc['steps'] if s['step_id'] == req), None)
            if req_step is not None:
                params[req] = req_step.get('result')
        return params

    def _mark_step(self, instance_id: str, step_id: str, **fields):
        def mutate(doc):
            step = next(s for s in doc['steps'] if s['step_id'] == step_id)
            step.update(fields)
        try:
            instance.update_instance(self.redis, instance_id, mutate)
        except KeyError:
            self.logger.error(f'Instance {instance_id} missing while updating step {step_id}')

    def reclaim_stale_pel_entries(self):
        for priority in dispatch.PRIORITIES:
            stream = dispatch.stream_key(priority)
            try:
                _, claimed, _ = self.redis.xautoclaim(
                    stream, self.consumer_group, self.id,
                    min_idle_time=self.pel_min_idle_ms, start_id='0-0', count=50,
                )
            except Exception as ex:
                self.logger.error(f'Unhandled Exception reclaiming PEL for {stream}: {ex}')
                continue

            for entry_id, fields in claimed:
                raw = fields.get(b'data', fields.get('data'))
                try:
                    payload = json.loads(raw)
                except Exception:
                    self.redis.xack(stream, self.consumer_group, entry_id)
                    continue

                attempt = payload.get('attempt', 1)
                max_attempts = payload.get('max_attempts', self.retry_policy.max_attempts)
                self.redis.xack(stream, self.consumer_group, entry_id)

                if attempt >= max_attempts:
                    message = 'Reclaimed from a crashed worker; retries exhausted'
                    dispatch.to_dlq(self.redis, payload, message)
                    if payload.get('instance_id'):
                        self._mark_step(payload['instance_id'], payload['step_id'], status='failed', error=message)
                else:
                    bumped = dict(payload, attempt=attempt + 1)
                    dispatch.enqueue_payload(self.redis, bumped)

    # ---- Every worker, leader or not: the consumer side ----

    def consume_one(self):
        for priority in dispatch.PRIORITIES:
            stream = dispatch.stream_key(priority)
            response = self.redis.xreadgroup(self.consumer_group, self.id, {stream: '>'}, count=1)
            if not response:
                continue
            _, messages = response[0]
            if not messages:
                continue
            entry_id, fields = messages[0]
            self._process_entry(stream, entry_id, fields)
            return

    def _process_entry(self, stream: str, entry_id, fields: dict):
        raw = fields.get(b'data', fields.get('data'))
        payload = json.loads(raw)
        job_name = payload['job_name']

        def ack_and_release():
            self.redis.xack(stream, self.consumer_group, entry_id)
            if payload.get('concurrency_key'):
                self.concurrency.release(payload['concurrency_key'], self._holder_token(payload))

        if self.is_job_cancelled(job_name):
            self.logger.info(f'Job {job_name} was cancelled, skipping execution')
            self.clear_job_cancellation(job_name)
            ack_and_release()
            return

        if job_name not in self.registry:
            self.logger.error(f'No job matches name {job_name}')
            ack_and_release()
            self._handle_failure(payload, f'Unknown job: {job_name}')
            return

        job = self.registry.new(job_name)
        try:
            params = payload.get('params')
            if isinstance(params, dict):
                job.run(**params, **self.kwargs)
            else:
                # For backwards compatibility or empty args
                job.run(**self.kwargs)
        except Exception as ex:
            # Deal with user error gracefully
            self.logger.error(f'Unhandled Exception In Job: {job_name}: {ex}')
            job.success = False
            job.error = ex

        ack_and_release()

        if job.success:
            self.logger.info(f'SUCCESS: {job_name}, {job.result}')
            if payload.get('instance_id'):
                self._mark_step(payload['instance_id'], payload['step_id'], status='completed', result=job.result)
            elif job.next:
                # Light DAG support: chain to the next job using this job's result as params.
                dispatch.dispatch_now(self.redis, job.next, job.result)
            if job.schedule:
                self._reschedule_cron(job_name, job.schedule)
        else:
            self.logger.error(f'Error running job {job_name}: {job.error}')
            self._handle_failure(payload, str(job.error) if job.error else 'Unknown error')
            if job.schedule:
                self._reschedule_cron(job_name, job.schedule)

        self.last_run = datetime.datetime.now(datetime.timezone.utc)

    def _handle_failure(self, payload: dict, error_message: str):
        attempt = payload.get('attempt', 1)
        max_attempts = payload.get('max_attempts', self.retry_policy.max_attempts)

        if attempt >= max_attempts:
            self.logger.error(f"{payload['job_name']} exhausted retries, sending to DLQ")
            dispatch.to_dlq(self.redis, payload, error_message)
            if payload.get('instance_id'):
                self._mark_step(payload['instance_id'], payload['step_id'], status='failed', error=error_message)
        else:
            bumped = dict(payload, attempt=attempt + 1)
            delay = self.retry_policy.delay(attempt)
            due_ts = datetime.datetime.now(datetime.timezone.utc).timestamp() + delay
            self.logger.info(f"Retrying {payload['job_name']} (attempt {attempt + 1}/{max_attempts}) in {delay:.1f}s")
            dispatch.dispatch_later(self.redis, self.retry_queue, bumped, due_ts)

    def _reschedule_cron(self, job_name: str, schedule: str):
        next_execution = self.scheduler.get_next_execution(schedule).timestamp()
        self.redis.zadd(self.scheduled_queue, {job_name: next_execution})

    def _holder_token(self, payload: dict) -> str:
        if payload.get('instance_id'):
            return f"{payload['instance_id']}:{payload['step_id']}"
        return f"{payload['job_name']}:{payload.get('created_at')}"

    def run(self):
        # Check that we have a leader each time this runs so we don't leave any jobs in limbo if the leader has gone down
        self.check_leader()

        if self.is_leader:
            try:
                self.drain_due_cron_jobs()
                self.drain_due_delayed_jobs()
                self.drain_due_retries()
                self.drain_due_workflow_cron()
                self.sweep_active_workflow_instances()
                self.reclaim_stale_pel_entries()
            except Exception as ex:
                self.logger.error(f'Unhandled Exception in leader tick: {ex}')

        try:
            self.consume_one()
        except Exception as ex:
            self.logger.error(f'Unhandled Exception in worker run process: {ex}')
