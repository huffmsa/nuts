from uuid import uuid4
import os
import json
from typing import Any, Protocol
from redis import Redis
from .job import NutsJob
from .workflow import NutsWorkflow
from .cron import Cron
import datetime
import logging
import yaml


class JobModule(Protocol):
    """Protocol for job modules that contain a Job class."""
    Job: type[NutsJob]


class Worker():
    '''
        A NUTS worker
    '''
    id: str
    redis: Redis
    scheduler: Cron
    logger: logging.Logger
    jobs: list[NutsJob]
    workflows: list[NutsWorkflow]
    kwargs: dict[str, Any]
    scheduled_queue: str
    pending_queue: str
    completed_queue: str
    last_run: datetime.datetime
    running_queue: str
    should_run: bool

    def __init__(self, redis: Redis, jobs: list[JobModule], workflow_directory: str = None, **kwargs):
        self.id = str(uuid4())
        self.scheduled_queue = 'nuts|jobs|scheduled'
        self.running_queue = 'nuts|jobs|running'
        self.pending_queue = 'nuts|jobs|pending'
        self.completed_queue = 'nuts|jobs|completed'
        self.scheduled_workflow_queue = 'nuts|workflows|scheduled'
        self.running_workflow_queue = 'nuts|workflows|running'
        self.completed_workflow_queue = 'nuts|workflows|completed'
        self.kwargs = kwargs
        self.should_run = True
        self.is_leader = False

        self.last_run = datetime.datetime.fromtimestamp(0)

        self.redis = redis

        self.scheduler = Cron()

        self.logger = logging.getLogger(f'worker|{self.id}')
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        self.jobs = []
        self.workflows = []

        self.check_leader()

        if self.is_leader and workflow_directory:
            self.logger.info(f'Worker {self.id} assuming leadership')
            # Register the workflows
            print(os.listdir(workflow_directory))
            for workflow in os.listdir(workflow_directory):
                if not workflow.endswith('.yaml'):
                    continue
                with open(os.path.join(workflow_directory, workflow), 'r') as f:
                    wf = yaml.safe_load(f)
                    self.logger.info(f'Registering Workflow {workflow}')

                    workflow = NutsWorkflow(**wf['workflow'])

                    # Validate workflow configuration
                    is_valid, error = workflow.validate()
                    if not is_valid:
                        self.logger.error(f'Invalid workflow {workflow.name}: {error}')
                        continue

                    self.workflows.append(workflow)

                    next_execution = self.scheduler.get_next_execution(workflow.schedule)
                    next_execution = next_execution.timestamp()
                    running_workflow = None
                    try:
                        pending = self.redis.zscan(self.scheduled_workflow_queue, match=workflow.name)
                        if pending[1][0]:
                            if pending[1][1] != next_execution:
                                # Schedule has changed
                                self.redis.zrem(self.scheduled_workflow_queue, workflow.name)
                                self.redis.zadd(self.scheduled_workflow_queue, {workflow.name: next_execution})

                    except Exception:
                        pass

                    running_workflow = self.redis.hget(self.running_workflow_queue, workflow.name)

                    if not running_workflow:
                        self.redis.zadd(self.scheduled_workflow_queue, {workflow.name: next_execution})
                    else:
                        print(running_workflow)
                        # Type guard: running_workflow is bytes here (not None)
                        assert isinstance(running_workflow, bytes)
                        workflow = NutsWorkflow(**json.loads(running_workflow))

        # Register jobs with the worker
        for job in jobs:
            j = job.Job()

            self.jobs.append(j)

            # If this worker is the leader, register the job schedules
            if self.is_leader and j.schedule:
                self.logger.info(f'Registering Cron Job {j.name}')

                next_execution = self.scheduler.get_next_execution(j.schedule).timestamp()

                try:
                    pending = self.redis.zscan('nuts|jobs|pending', match=j.name)
                    if pending[1][0]:
                        if pending[1][1] != next_execution:
                            # Schedule has changed
                            self.redis.zrem(self.scheduled_queue, job.name)
                            self.redis.zadd(self.scheduled_queue, {job.name: next_execution})

                except Exception:
                    pass

    def check_leader(self) -> bool:
        leadership_check = self.redis.setnx('leader_id', self.id)

        if not leadership_check:
            leader_id = self.redis.get('leader_id')
            if leader_id == self.id:
                # Puts a 60 second life on the current leader so we aren't left in limbo if the leader worker dies ungracefully.
                self.redis.expire('leader_id', 60)

                self.is_leader = True

        else:
            # Puts a 60 second life on the current leader so we aren't left in limbo if the leader worker dies ungracefully.
            self.redis.expire('leader_id', 60)
            self.is_leader = True

    def shutdown(self, signum, frame):
        self.logger.info(f'Received shutdown command {signum}.')
        self.should_run = False

        if self.is_leader:
            for workflow in self.workflows:
                if workflow.status == 'active':
                    self.logger.info(f'Workflow {workflow.name} is active, persisting state')
                    self.redis.hset(self.running_workflow_queue, workflow.name, json.dumps(workflow, default=lambda o: o.__dict__))
            self.release_leader()

    def release_leader(self):
        if self.is_leader:
            self.logger.info('Shutdown: Releasing leadership')
            self.redis.expire('leader_id', -1)

    def schedule_pending_job(self, job_name, job_params=[]):
        self.redis.sadd(self.pending_queue, json.dumps([job_name, job_params]))

    def move_scheduled_to_pending(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        now_timestamp = round(now.timestamp())

        ready_jobs = self.redis.zrange(self.scheduled_queue, start=0, end=now_timestamp, withscores=True)

        if len(ready_jobs) > 0:
            latest = max(j[1] for j in ready_jobs)
            self.redis.zremrangebyscore(self.scheduled_queue, min=self.last_run.timestamp(), max=latest)

            for job in ready_jobs:
                self.schedule_pending_job(job[0].decode())

    def move_scheduled_workflows_to_running(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        now_timestamp = round(now.timestamp())

        ready_workflows = self.redis.zrange(self.scheduled_workflow_queue, start=0, end=now_timestamp, withscores=True)

        if len(ready_workflows) > 0:
            latest = max(j[1] for j in ready_workflows)
            self.redis.zremrangebyscore(self.scheduled_workflow_queue, min=self.last_run.timestamp(), max=latest)

        for workflow in ready_workflows:
            wf_name = workflow[0].decode()
            wf = [w for w in self.workflows if w.name == wf_name][0]

            wf.status = 'active'

    def move_pending_to_running(self, job: NutsJob, job_args: list):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.redis.hset(self.running_queue, f'{self.id}|{job.name}', json.dumps({'timestamp': now, 'args': job_args}))

    def remove_running(self, job: NutsJob):

        self.redis.hdel(self.running_queue, f'{self.id}|{job.name}')

    def move_to_completed(self, job: NutsJob, workflow_name: str = None):
        if workflow_name:
            name = f'{workflow_name}|{job.name}'
        else:
            name = job.name

        job_data = {'success': job.success}
        if hasattr(job, 'error') and job.error:
            job_data['error'] = str(job.error)

        self.redis.hset(self.completed_queue, name, json.dumps(job_data))

    def queue_completed_jobs(self):

        completed_jobs = self.redis.hkeys(self.completed_queue)

        for job_name in completed_jobs:
            job_results_raw = self.redis.hget(self.completed_queue, job_name)
            if not job_results_raw:
                continue  # Skip if no results found
            # Type guard: job_results_raw is bytes here (not None)
            assert isinstance(job_results_raw, bytes)
            job_results = json.loads(job_results_raw)
            status = 'completed' if job_results.get('success', None) else 'failed'
            # Remove from the completed queue
            self.redis.hdel(self.completed_queue, job_name)

            if 'workflow' in job_name.decode():
                [workflow_name, job_name] = job_name.decode().split('|')
                workflow_name = workflow_name.replace('workflow-', '')
                wf = [w for w in self.workflows if w.name == workflow_name][0]

                # Pass error information if job failed
                error_msg = job_results.get('error', None) if status == 'failed' else None
                wf.update(job_name, status, error_msg)

            else:
                job = [j for j in self.jobs if j.name == job_name.decode()][0]

                next_execution = self.scheduler.get_next_execution(job.schedule).timestamp()

                self.redis.zadd(self.scheduled_queue, {job.name: next_execution})

    def run_workflows(self):
        """
        Execute active workflows by checking dependencies and scheduling ready jobs.

        This method:
        - Moves scheduled workflows to running state
        - Checks for job failures (stops workflow if any job failed)
        - Identifies next ready job based on dependencies
        - Schedules ready jobs to pending queue
        - Reschedules completed/failed workflows for next run

        Workflows are executed by the leader worker only.
        """
        # Move ready  to the pending queue
        self.move_scheduled_workflows_to_running()

        try:
            for workflow in self.workflows:
                if workflow.status == 'active':
                    self.logger.info(f'Running workflow {workflow.name}')

                    # Check for failures first
                    if workflow.has_failures():
                        self.logger.error(f'Workflow {workflow.name} failed: {workflow.error}')
                        workflow.status = 'failed'
                        # Reschedule for next run
                        workflow.reset()
                        next_execution = self.scheduler.get_next_execution(workflow.schedule).timestamp()
                        self.redis.zadd(self.scheduled_workflow_queue, {workflow.name: next_execution})
                        continue

                    try:
                        next_job = workflow.run()
                        if not next_job and workflow.completed():
                            self.logger.info(f'Workflow {workflow.name} completed successfully')
                            workflow.reset()
                            next_execution = self.scheduler.get_next_execution(workflow.schedule).timestamp()
                            print(self.scheduler.get_next_execution(workflow.schedule), datetime.datetime.now(datetime.timezone.utc))
                            self.redis.zadd(self.scheduled_workflow_queue, {workflow.name: next_execution})
                        elif next_job:
                            self.logger.info(f'Workflow {workflow.name} next job {next_job}')

                            self.schedule_pending_job(f'workflow-{workflow.name}|{next_job}')
                            workflow.update(next_job, 'pending')

                    except Exception as ex:
                        # Deal with user error gracefully
                        self.logger.error(f'Unhandled Exception In Workflow: {workflow.name}: {ex}')
                        workflow.status = 'failed'
                        workflow.error = str(ex)
                        workflow.reset()
                        next_execution = self.scheduler.get_next_execution(workflow.schedule).timestamp()
                        self.redis.zadd(self.scheduled_workflow_queue, {workflow.name: next_execution})

        except Exception as ex:
            self.logger.error(f'Unhandled Exception in run_workflows: {ex}')

    def run(self):
        # Check that we have a leader each time this runs so we don't leave any jobs in limbo if the leader has gone down
        self.check_leader()

        if self.is_leader:
            # Move ready jobs to the pending queue
            self.move_scheduled_to_pending()
            self.run_workflows()

        try:
            data = self.redis.spop(self.pending_queue, 1)
            if not len(data):
                return
            else:
                # Type guard: data[0] is bytes (from redis)
                assert isinstance(data[0], bytes)
                [job_name, job_args] = json.loads(data[0])
                for_workflow = False
                workflow_name = None
                if 'workflow' in job_name:
                    for_workflow = True
                    [workflow_name, job_name] = job_name.split('|')

                jobs = [j for j in self.jobs if j.name == job_name]

                if not len(jobs):
                    self.logger.info(f'No job matches name {job_name}')
                    return
                else:
                    job = jobs[0]
                    self.move_pending_to_running(job, job_args)

                    try:
                        # Handle both dict arguments and backwards compatibility
                        if isinstance(job_args, dict):
                            job.run(**job_args, **self.kwargs)
                        else:
                            # For backwards compatibility or empty args
                            job.run(**self.kwargs)
                    except Exception as ex:
                        # Deal with user error gracefully
                        self.logger.error(f'Unhandled Exception In Job: {job.name}: {ex}')

                    self.remove_running(job)

                    if job.success:
                        self.logger.info(f'SUCCESS: {job.name}, {job.result}')
                        # Light DAG support, can chain together jobs in a workflow by defining the next step that should
                        # be taken after a job completes
                        if job.next:
                            self.schedule_pending_job(job.next, job.result)

                    else:
                        self.logger.error(f'Error running job {job.name}: {job.error}')

                    if job.schedule or for_workflow:
                        self.move_to_completed(job, workflow_name)

                self.last_run = datetime.datetime.now(datetime.timezone.utc)

        except Exception as ex:
            self.logger.error(f'Unhandled Exception in worker run process: {ex}')

        # Post Execution
        if self.is_leader:
            self.queue_completed_jobs()
