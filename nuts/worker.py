from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from uuid import uuid4
import os
import json
import random
import threading
import time
from typing import Any, Callable, Union
from redis import Redis
from .job import NutsJob, JobModule, JobRegistry
from .workflow import NutsWorkflow
from .cron import Cron
from .concurrency import ConcurrencyGate
from . import dispatch
from . import instance
from . import batch
from . import lua
import datetime
import logging
import yaml

LEADER_KEY = 'leader_id'


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


@dataclass
class _Claim:
    '''A stream entry this worker has read and not yet acked.'''
    stream: str
    entry_id: Any
    payload: dict
    started_at: Union[float, None] = None
    timeout_seconds: Union[float, None] = None
    holds_slot: bool = False
    timed_out: bool = False
    key: tuple = field(init=False)

    def __post_init__(self):
        self.key = (self.stream, self.entry_id)


def _now_ts() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


class Worker():
    '''
        A NUTS worker.

        Delivery is backed by Redis Streams + consumer groups: entries
        aren't gone until acked, a crashed worker's claim sits in the
        Pending Entries List and gets reclaimed, and priority is a small
        number of discrete streams checked in strict order. While a job
        runs, a heartbeat keeps renewing the worker's claim so a slow job
        isn't mistaken for a crashed one.

        serve() is the production loop: leader duties run in their own
        thread and jobs run in a pool of max_concurrent_jobs threads, so
        shared resources passed as kwargs must be thread-safe. run() is a
        single step (leader tick + one job) kept for simple loops and tests.
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

        self.scheduled_queue = 'nuts|jobs|scheduled'                 # bare cron job names, deduped by name
        self.scheduled_workflow_queue = 'nuts|workflows|scheduled'   # bare workflow template names, deduped by name
        self.delayed_queue = dispatch.DELAYED_QUEUE                  # JSON payloads (delayed publishes, batch window flushes)
        self.retry_queue = 'nuts|retry|pending'                      # JSON payloads waiting out retry backoff
        self.cancel_queue = 'nuts|jobs|cancel'                       # job names: skip the next run of that job

        self.consumer_group = kwargs.pop('consumer_group', 'nuts|workers')
        self.pel_min_idle_ms = kwargs.pop('pel_min_idle_ms', 60000)
        self.instance_ttl_seconds = kwargs.pop('instance_ttl_seconds', instance.DEFAULT_TTL_SECONDS)
        self.concurrency_lease_seconds = kwargs.pop('concurrency_lease_seconds', 300)
        self.retry_policy = kwargs.pop('retry_policy', RetryPolicy())
        self.max_concurrent_jobs = kwargs.pop('max_concurrent_jobs', 1)
        self.block_ms = kwargs.pop('block_ms', 1000)
        self.leader_interval_seconds = kwargs.pop('leader_interval_seconds', 1.0)
        self.leader_ttl_seconds = kwargs.pop('leader_ttl_seconds', 60)
        self.full_sweep_interval_seconds = kwargs.pop('full_sweep_interval_seconds', 300)
        self.history_length = kwargs.pop('history_length', 1000)
        self.consumer_prune_after_ms = kwargs.pop('consumer_prune_after_ms', 3600000)
        self.leader_tick_budget_seconds = kwargs.pop('leader_tick_budget_seconds', 5.0)
        self.heartbeat_interval_seconds = kwargs.pop(
            'heartbeat_interval_seconds', max(0.01, self.pel_min_idle_ms / 1000 / 3)
        )

        self.kwargs = kwargs
        self.should_run = True
        self.is_leader = False

        self.last_run = datetime.datetime.fromtimestamp(0)
        self._last_full_sweep = 0.0

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
        self.workflow_directory = workflow_directory
        self.workflow_templates = []
        self._templates_loaded = False

        self._claims: dict[tuple, _Claim] = {}
        self._claims_changed = threading.Condition()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Union[threading.Thread, None] = None
        self._leader_stop = threading.Event()
        self._serving = False

        self._ensure_consumer_groups()
        self.check_leader()

    def _ensure_consumer_groups(self):
        for priority in dispatch.PRIORITIES:
            try:
                self.redis.xgroup_create(dispatch.stream_key(priority), self.consumer_group, id='0', mkstream=True)
            except Exception as ex:
                if 'BUSYGROUP' not in str(ex):
                    raise

    # ---- Leadership ----

    def check_leader(self) -> bool:
        was_leader = self.is_leader
        if self.redis.set(LEADER_KEY, self.id, nx=True, ex=self.leader_ttl_seconds):
            self.is_leader = True
        else:
            self.is_leader = lua.expire_if_equals(self.redis, LEADER_KEY, self.id, self.leader_ttl_seconds)

        if self.is_leader and not was_leader:
            self._on_become_leader()
        return self.is_leader

    def _on_become_leader(self):
        self.logger.info(f'Worker {self.id} assuming leadership')
        if self.workflow_directory and not self._templates_loaded:
            self._load_workflow_templates(self.workflow_directory)
            self._templates_loaded = True
        self._register_cron_jobs()

    def release_leader(self):
        if self.is_leader:
            self.logger.info('Releasing leadership')
            lua.del_if_equals(self.redis, LEADER_KEY, self.id)
            self.is_leader = False

    def shutdown(self, signum, frame):
        '''Signal handler. Under serve(), in-flight jobs finish before leadership is released.'''
        self.logger.info(f'Received shutdown command {signum}.')
        self.should_run = False
        if not self._serving:
            self.release_leader()

    # ---- Leader-only: schedules, retries, workflow advancement, recovery ----

    def _load_workflow_templates(self, workflow_directory: str):
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
        '''Seed or correct a bare-name due-time entry. Keyed by name, so restarts don't pile up duplicates.'''
        if self.redis.zscore(zset_key, name) != next_due_ts:
            self.redis.zadd(zset_key, {name: next_due_ts})

    def _due_names(self, zset_key: str, now_ts: float) -> list[str]:
        candidates = self.redis.zrange(zset_key, start=0, end=now_ts, byscore=True)
        names = []
        for member in candidates:
            if self.redis.zrem(zset_key, member):
                names.append(member.decode())
        return names

    def leader_tick(self):
        steps: list[Callable[[], Any]] = [
            self.drain_due_cron_jobs,
            self.drain_due_delayed_jobs,
            self.drain_due_retries,
            self.drain_due_workflow_cron,
            self.drain_due_wait_deadlines,
            self.advance_dirty_instances,
            self.pump_concurrency_waiters,
            self.reclaim_stale_pel_entries,
        ]
        if time.monotonic() - self._last_full_sweep >= self.full_sweep_interval_seconds:
            steps.append(self.sweep_active_workflow_instances)
            steps.append(self.prune_idle_consumers)
        for step in steps:
            if not self.is_leader:
                return
            try:
                step()
            except Exception as ex:
                self.logger.error(f'Unhandled Exception in leader step {step.__name__}: {ex}')

    def drain_due_cron_jobs(self):
        for name in self._due_names(self.scheduled_queue, _now_ts()):
            definition = self.registry.get(name)
            if definition is None:
                continue
            payload = dispatch.build_payload(name, {}, priority='normal', max_attempts=definition.max_attempts)
            dispatch.enqueue_payload(self.redis, payload)

    def drain_due_delayed_jobs(self):
        for payload in dispatch.due_items(self.redis, self.delayed_queue, _now_ts()):
            dispatch.enqueue_payload(self.redis, payload)

    def drain_due_retries(self):
        for payload in dispatch.due_items(self.redis, self.retry_queue, _now_ts()):
            dispatch.enqueue_payload(self.redis, payload)

    def drain_due_workflow_cron(self):
        for name in self._due_names(self.scheduled_workflow_queue, _now_ts()):
            template = next((t for t in self.workflow_templates if t.name == name), None)
            if template is None:
                continue
            # Reschedule at drain time, not completion: overlapping instances
            # of the same template are allowed.
            instance.create_instance(self.redis, template.name, template.to_plan())
            next_execution = self.scheduler.get_next_execution(template.schedule).timestamp()
            self.redis.zadd(self.scheduled_workflow_queue, {template.name: next_execution})

    def drain_due_wait_deadlines(self):
        for instance_id in self._due_names(instance.WAIT_DEADLINES, _now_ts()):
            instance.mark_dirty(self.redis, instance_id)

    def advance_dirty_instances(self):
        '''Advance instances that something has touched since the last pass, within the tick's time budget.'''
        started = time.monotonic()
        while time.monotonic() - started < self.leader_tick_budget_seconds:
            ids = self.redis.spop(instance.DIRTY_SET, 200)
            if not ids:
                return
            for raw_id in ids:
                self._advance_instance(raw_id.decode())
            if not self.check_leader():
                return

    def sweep_active_workflow_instances(self):
        '''
            Advance every active instance. Catches anything whose dirty mark
            was lost (e.g. the leader died mid-pass). Instances with nothing
            to do aren't written.
        '''
        self._last_full_sweep = time.monotonic()
        chunk = []
        for raw_id in self.redis.sscan_iter(instance.ACTIVE_SET, count=500):
            chunk.append(raw_id.decode())
            if len(chunk) == 500:
                self._sweep_chunk(chunk)
                chunk = []
                if not self.check_leader():
                    return
        if chunk:
            self._sweep_chunk(chunk)

    def _sweep_chunk(self, instance_ids: list[str]):
        '''Read a chunk in two round trips and only advance the instances that can actually move.'''
        pipe = self.redis.pipeline(transaction=False)
        for instance_id in instance_ids:
            pipe.get(instance.instance_key(instance_id))
            pipe.exists(instance.signal_key(instance_id))
        results = pipe.execute()
        now = _now_ts()
        for n, instance_id in enumerate(instance_ids):
            raw, has_signals = results[2 * n], results[2 * n + 1]
            doc = json.loads(raw) if raw is not None else None
            if doc is None or has_signals or instance.needs_advance(doc, now):
                self._advance_instance(instance_id)

    def _advance_instance(self, instance_id: str):
        try:
            doc = instance.get_instance(self.redis, instance_id)
            if doc is None or doc['status'] != 'active':
                self.redis.srem(instance.ACTIVE_SET, instance_id)
                return

            doc = instance.resolve_wait_steps(self.redis, instance_id)
            if doc is None:
                return

            for step in instance.ready_steps(doc):
                if step['kind'] != 'job':
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
                # Enqueue before marking: a crash in between re-dispatches
                # the step (a duplicate) rather than stranding it.
                dispatch.enqueue_payload(self.redis, payload)
                self._mark_step(instance_id, step['step_id'], only_if_unstarted=True,
                                status='dispatched', dispatch_id=payload['dispatch_id'])

            doc = instance.get_instance(self.redis, instance_id)
            if doc is None:
                return
            statuses = [s['status'] for s in doc['steps']]
            if all(s == 'completed' for s in statuses):
                instance.complete_instance(self.redis, instance_id, self.instance_ttl_seconds)
            elif 'failed' in statuses:
                failed = next(s for s in doc['steps'] if s['status'] == 'failed')
                instance.fail_instance(self.redis, instance_id, self.instance_ttl_seconds,
                                       error=doc.get('error') or failed.get('error'))
        except Exception as ex:
            self.logger.error(f'Error advancing instance {instance_id}: {ex}')

    def _step_params(self, doc: dict, step: dict) -> dict:
        '''Instance params plus each prerequisite step's result, keyed by that step's step_id.'''
        params = dict(doc.get('params') or {})
        for req in (step.get('requires') or []):
            req_step = next((s for s in doc['steps'] if s['step_id'] == req), None)
            if req_step is not None:
                params[req] = req_step.get('result')
        return params

    def _mark_step(self, instance_id: str, step_id: str, only_if_unstarted: bool = False, **fields):
        def mutate(doc):
            step = next((s for s in doc['steps'] if s['step_id'] == step_id), None)
            if step is None:
                return
            if only_if_unstarted and step['status'] is not None:
                return
            step.update(fields)
        try:
            instance.update_instance(self.redis, instance_id, mutate)
        except KeyError:
            self.logger.error(f'Instance {instance_id} missing while updating step {step_id}')
            return
        if not only_if_unstarted:
            instance.mark_dirty(self.redis, instance_id)

    def pump_concurrency_waiters(self):
        '''Hand slots freed by expired leases (crashed holders) to parked jobs.'''
        for key in self.concurrency.waiting_keys():
            self.concurrency.pump(key, self.concurrency_lease_seconds)

    def prune_idle_consumers(self):
        '''Every worker joins the consumer group under its own id; drop ids of workers long gone so the group doesn't grow forever.'''
        for priority in dispatch.PRIORITIES:
            stream = dispatch.stream_key(priority)
            for consumer in self.redis.xinfo_consumers(stream, self.consumer_group):
                if consumer['pending'] == 0 and consumer['idle'] > self.consumer_prune_after_ms:
                    self.redis.xgroup_delconsumer(stream, self.consumer_group, consumer['name'])

    def reclaim_stale_pel_entries(self):
        for priority in dispatch.PRIORITIES:
            stream = dispatch.stream_key(priority)
            try:
                _, claimed, *_ = self.redis.xautoclaim(
                    stream, self.consumer_group, self.id,
                    min_idle_time=self.pel_min_idle_ms, start_id='0-0', count=50,
                )
            except Exception as ex:
                self.logger.error(f'Unhandled Exception reclaiming PEL for {stream}: {ex}')
                continue

            for entry_id, fields in claimed:
                raw = (fields or {}).get(b'data', (fields or {}).get('data'))
                try:
                    payload = json.loads(raw)
                except Exception:
                    dispatch.ack(self.redis, stream, self.consumer_group, entry_id)
                    continue

                attempt = payload.get('attempt', 1)
                max_attempts = payload.get('max_attempts', self.retry_policy.max_attempts)
                if attempt >= max_attempts:
                    message = 'Reclaimed from a stalled or crashed worker; retries exhausted'
                    self._record(payload, 'dead', error=message)
                    self._give_up(payload, message)
                else:
                    self._record(payload, 'reclaimed', error='Reclaimed from a stalled or crashed worker')
                    dispatch.enqueue_payload(self.redis, dict(payload, attempt=attempt + 1))
                dispatch.ack(self.redis, stream, self.consumer_group, entry_id)

    # ---- Every worker: the consumer side ----

    def _claim(self, stream: str, entry_id, fields: dict) -> _Claim:
        raw = fields.get(b'data', fields.get('data'))
        payload = json.loads(raw)
        payload.setdefault('dispatch_id', dispatch.holder_token(payload))
        definition = self.registry.get(payload.get('job_name'))
        claim = _Claim(stream, entry_id, payload, timeout_seconds=definition.timeout_seconds if definition else None)
        with self._claims_changed:
            self._claims[claim.key] = claim
        self._ensure_heartbeat()
        return claim

    def _release_claim(self, claim: _Claim):
        with self._claims_changed:
            self._claims.pop(claim.key, None)
            self._claims_changed.notify_all()

    def _read(self, count: int, block_ms: int = 0) -> list[_Claim]:
        '''Claim up to count entries, highest priority first. If none are waiting, optionally block for new ones.'''
        claims = []
        for priority in dispatch.PRIORITIES:
            need = count - len(claims)
            if need <= 0:
                break
            stream = dispatch.stream_key(priority)
            response = self.redis.xreadgroup(self.consumer_group, self.id, {stream: '>'}, count=need)
            for _, messages in response or []:
                for entry_id, fields in messages:
                    claims.append(self._claim(stream, entry_id, fields))

        if not claims and block_ms:
            streams = {dispatch.stream_key(p): '>' for p in dispatch.PRIORITIES}
            response = self.redis.xreadgroup(self.consumer_group, self.id, streams, count=1, block=block_ms)
            by_stream = {(s.decode() if isinstance(s, bytes) else s): m for s, m in (response or [])}
            for priority in dispatch.PRIORITIES:
                stream = dispatch.stream_key(priority)
                for entry_id, fields in by_stream.get(stream, []):
                    claims.append(self._claim(stream, entry_id, fields))
        return claims

    def consume_one(self, block_ms: int = 0) -> bool:
        '''Run one job inline. Returns False if there was nothing to run.'''
        claims = self._read(1, block_ms)
        for claim in claims:
            self._execute(claim)
        return bool(claims)

    def _is_cancelled(self, payload: dict) -> bool:
        if dispatch.is_cancelled(self.redis, payload.get('dispatch_id')):
            return True
        if self.redis.sismember(self.cancel_queue, payload['job_name']):
            self.redis.srem(self.cancel_queue, payload['job_name'])
            return True
        if payload.get('instance_id'):
            doc = instance.get_instance(self.redis, payload['instance_id'])
            return doc is None or doc['status'] != 'active'
        return False

    def _execute(self, claim: _Claim):
        payload = claim.payload
        job_name = payload['job_name']
        key = payload.get('concurrency_key')
        holder = payload['dispatch_id']
        try:
            if self._is_cancelled(payload):
                self.logger.info(f'Job {job_name} ({holder}) was cancelled, skipping execution')
                self._record(payload, 'cancelled')
                if payload.get('batch'):
                    batch.done(self.redis, payload)
                claim.holds_slot = bool(key)  # it may have been handed a slot while parked
                self._ack(claim)
                return

            if key:
                parked = json.dumps(dict(payload, attempt=payload.get('attempt', 1)))
                if not self.concurrency.acquire(key, holder, payload.get('concurrency_limit', 1),
                                                self.concurrency_lease_seconds, park_payload=parked):
                    # Parked atomically by acquire(); it comes back when a slot frees.
                    dispatch.ack(self.redis, claim.stream, self.consumer_group, claim.entry_id)
                    return
                claim.holds_slot = True

            claim.started_at = time.monotonic()
            if payload.get('batch'):
                self._run_batch(claim)
            else:
                self._run_single(claim)
            self._ack(claim)
            self.last_run = datetime.datetime.now(datetime.timezone.utc)
        except Exception as ex:
            # Left unacked on purpose: the entry is reclaimed and retried.
            self.logger.error(f'Unhandled Exception processing {job_name} ({holder}): {ex}')
        finally:
            self._release_claim(claim)

    def _ack(self, claim: _Claim):
        dispatch.ack(self.redis, claim.stream, self.consumer_group, claim.entry_id)
        if claim.holds_slot:
            self.concurrency.release(claim.payload['concurrency_key'], claim.payload['dispatch_id'],
                                     self.concurrency_lease_seconds)
            claim.holds_slot = False

    def _new_job(self, job_name: str) -> Union[NutsJob, None]:
        if job_name not in self.registry:
            self.logger.error(f'No job matches name {job_name}')
            return None
        return self.registry.new(job_name)

    def _call(self, job: NutsJob, job_name: str, params: dict):
        try:
            job.run(**params, **self.kwargs)
        except Exception as ex:
            self.logger.error(f'Unhandled Exception In Job: {job_name}: {ex}')
            job.success = False
            job.error = ex

    def _run_single(self, claim: _Claim):
        payload = claim.payload
        job_name = payload['job_name']
        job = self._new_job(job_name)
        if job is None:
            self._handle_failure(payload, f'Unknown job: {job_name}', started=claim.started_at)
            return

        params = payload.get('params')
        self._call(job, job_name, params if isinstance(params, dict) else {})

        if job.success:
            self.logger.info(f'SUCCESS: {job_name}, {job.result}')
            self._record(payload, 'succeeded', started=claim.started_at)
            if payload.get('instance_id'):
                self._mark_step(payload['instance_id'], payload['step_id'], status='completed', result=job.result)
            elif job.next:
                dispatch.dispatch_now(self.redis, job.next, job.result)
        else:
            self.logger.error(f'Error running job {job_name}: {job.error}')
            self._handle_failure(payload, str(job.error) if job.error else 'Unknown error', started=claim.started_at)

        if job.schedule:
            self._reschedule_cron(job_name, job.schedule)

    def _run_batch(self, claim: _Claim):
        payload = claim.payload
        job_name = payload['job_name']
        items, remaining = batch.take(self.redis, payload)
        if remaining >= payload['batch']['max_items']:
            dispatch.enqueue_payload(self.redis, batch.follow_up(payload))
        elif remaining > 0:
            batch.schedule_window_flush(self.redis, payload)
        if not items:
            return

        job = self._new_job(job_name)
        if job is None:
            self._handle_failure(payload, f'Unknown job: {job_name}', started=claim.started_at)
            return

        self._call(job, job_name, {'items': [i['params'] for i in items]})
        if not job.success:
            self.logger.error(f'Error running batch {job_name}: {job.error}')
            self._handle_failure(payload, str(job.error) if job.error else 'Unknown error', started=claim.started_at)
            return

        self.logger.info(f'SUCCESS: {job_name} batch of {len(items)}')
        failed_count = len(job.failed_items or {})
        self._record(payload, 'succeeded', started=claim.started_at,
                     items=len(items), failed_items=failed_count)
        retry = []
        for index, error in (job.failed_items or {}).items():
            item = dict(items[index], attempt=items[index].get('attempt', 1) + 1)
            if item['attempt'] > payload.get('max_attempts', self.retry_policy.max_attempts):
                self._item_to_dlq(payload, item, str(error))
            else:
                retry.append(item)
        if retry:
            flush = batch.retry_items(self.redis, payload, retry)
            delay = self.retry_policy.delay(min(i['attempt'] for i in retry) - 1)
            dispatch.dispatch_later(self.redis, self.retry_queue, flush, _now_ts() + delay)
        batch.done(self.redis, payload)

    def _item_to_dlq(self, flush: dict, item: dict, error: str):
        dispatch.to_dlq(self.redis, {
            'kind': 'batch_item',
            'job_name': flush['job_name'],
            'params': item['params'],
            'item_id': item['item_id'],
            'attempt': item['attempt'],
            'max_attempts': flush.get('max_attempts'),
            'priority': flush['priority'],
            'batch_key': flush['batch'].get('batch_key'),
            'batch': flush['batch'],
            'concurrency_key': flush.get('concurrency_key'),
            'concurrency_limit': flush.get('concurrency_limit', 1),
        }, error)

    def _handle_failure(self, payload: dict, error_message: str, started: Union[float, None] = None):
        attempt = payload.get('attempt', 1)
        max_attempts = payload.get('max_attempts', self.retry_policy.max_attempts)

        if attempt >= max_attempts:
            self.logger.error(f"{payload['job_name']} exhausted retries, sending to DLQ")
            self._record(payload, 'dead', error=error_message, started=started)
            self._give_up(payload, error_message)
        else:
            self._record(payload, 'retrying', error=error_message, started=started)
            bumped = dict(payload, attempt=attempt + 1)
            delay = self.retry_policy.delay(attempt)
            self.logger.info(f"Retrying {payload['job_name']} (attempt {attempt + 1}/{max_attempts}) in {delay:.1f}s")
            dispatch.dispatch_later(self.redis, self.retry_queue, bumped, _now_ts() + delay)

    def _record(self, payload: dict, outcome: str, error: Union[str, None] = None,
                started: Union[float, None] = None, **extra):
        try:
            dispatch.record_history(self.redis, {
                'dispatch_id': payload.get('dispatch_id'),
                'job_name': payload.get('job_name'),
                'outcome': outcome,
                'error': error,
                'attempt': payload.get('attempt', 1),
                'priority': payload.get('priority'),
                'instance_id': payload.get('instance_id'),
                'step_id': payload.get('step_id'),
                'worker_id': self.id,
                'finished_at': _now_ts(),
                'duration_seconds': round(time.monotonic() - started, 3) if started is not None else None,
                **extra,
            }, self.history_length)
        except Exception as ex:
            self.logger.error(f'Could not record history: {ex}')

    def _give_up(self, payload: dict, error: str):
        if payload.get('batch'):
            key = batch.inflight_key(payload['dispatch_id'])
            items = [json.loads(i) for i in self.redis.lrange(key, 0, -1)]
            for item in items:
                self._item_to_dlq(payload, item, error)
            if not items:
                dispatch.to_dlq(self.redis, payload, error)
            batch.done(self.redis, payload)
            return
        dispatch.to_dlq(self.redis, payload, error)
        if payload.get('instance_id'):
            self._mark_step(payload['instance_id'], payload['step_id'], status='failed', error=error)

    def _reschedule_cron(self, job_name: str, schedule: str):
        next_execution = self.scheduler.get_next_execution(schedule).timestamp()
        self.redis.zadd(self.scheduled_queue, {job_name: next_execution})

    # ---- Heartbeat ----

    def _ensure_heartbeat(self):
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name=f'nuts-heartbeat-{self.id}', daemon=True)
            self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(self.heartbeat_interval_seconds):
            try:
                self.heartbeat()
            except Exception as ex:
                self.logger.error(f'Heartbeat failed: {ex}')

    def heartbeat(self):
        '''
            Renew this worker's claim on every entry it holds, and the
            concurrency lease of every running job. A job past its
            timeout_seconds stops being renewed, so it will be reclaimed and
            may run again elsewhere.
        '''
        with self._claims_changed:
            claims = list(self._claims.values())
        now = time.monotonic()
        by_stream: dict[str, list] = {}
        for claim in claims:
            if claim.started_at is not None and claim.timeout_seconds is not None \
                    and now - claim.started_at > claim.timeout_seconds:
                if not claim.timed_out:
                    claim.timed_out = True
                    self.logger.warning(f"{claim.payload['job_name']} ({claim.payload['dispatch_id']}) "
                                        f"passed its {claim.timeout_seconds}s timeout; no longer renewing its claim")
                continue
            by_stream.setdefault(claim.stream, []).append(claim.entry_id)
            if claim.holds_slot:
                self.concurrency.refresh(claim.payload['concurrency_key'], claim.payload['dispatch_id'],
                                         self.concurrency_lease_seconds)
        for stream, entry_ids in by_stream.items():
            self.redis.xclaim(stream, self.consumer_group, self.id, min_idle_time=0,
                              message_ids=entry_ids, justid=True)

    # ---- Loops ----

    def run(self):
        '''One step: a leader tick if this worker leads, then at most one job. Doesn't block when idle.'''
        self.check_leader()

        if self.is_leader:
            self.leader_tick()

        try:
            self.consume_one()
        except Exception as ex:
            self.logger.error(f'Unhandled Exception in worker run process: {ex}')

    def serve(self):
        '''
            Run until should_run is cleared (e.g. by shutdown() as a signal
            handler). Waits for in-flight jobs before returning.
        '''
        self._serving = True
        self._leader_stop.clear()
        leader_thread = threading.Thread(target=self._leader_loop, name=f'nuts-leader-{self.id}', daemon=True)
        leader_thread.start()
        self._ensure_heartbeat()

        pool = ThreadPoolExecutor(max_workers=self.max_concurrent_jobs, thread_name_prefix=f'nuts-job-{self.id}')
        try:
            while self.should_run:
                with self._claims_changed:
                    free = self.max_concurrent_jobs - len(self._claims)
                    if free <= 0:
                        self._claims_changed.wait(timeout=self.block_ms / 1000)
                        continue
                try:
                    claims = self._read(free, self.block_ms)
                except Exception as ex:
                    self.logger.error(f'Error reading streams: {ex}')
                    time.sleep(1)
                    continue
                for claim in claims:
                    pool.submit(self._execute, claim)
        finally:
            pool.shutdown(wait=True)
            self._leader_stop.set()
            leader_thread.join()
            self._heartbeat_stop.set()
            self.release_leader()
            self._serving = False

    def _leader_loop(self):
        while not self._leader_stop.is_set():
            try:
                if self.check_leader():
                    self.leader_tick()
            except Exception as ex:
                self.logger.error(f'Unhandled Exception in leader loop: {ex}')
            self._leader_stop.wait(self.leader_interval_seconds)
