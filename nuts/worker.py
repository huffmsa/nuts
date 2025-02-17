import os
import json
from redis import Redis
from .job import NutsJob
from .cron import Cron
import datetime
import logging


class Worker():
    '''
        A NUTS worker
    '''
    id: int
    redis: Redis
    scheduler: Cron
    logger: logging.Logger
    jobs: list[NutsJob]
    kwargs: dict[str, any]
    scheduled_queue: str
    pending_queue: str
    completed_queue: str
    last_run: datetime.datetime
    running_list: str

    def __init__(self, redis: Redis, jobs: list[NutsJob], **kwargs):
        self.id = os.getpid()
        self.scheduled_queue = 'nuts|jobs|scheduled'
        self.running_list = 'nuts|job|running'
        self.pending_queue = 'nuts|jobs|pending'
        self.completed_queue = 'nuts|jobs|completed'
        self.kwargs = kwargs

        self.last_run = datetime.datetime.fromtimestamp(0)

        self.redis = redis

        self.scheduler = Cron()

        self.logger = logging.getLogger(f'worker|{self.id}')

        self.jobs = []

        logging.basicConfig(level=logging.INFO)

        is_leader = self.check_leader()
        if is_leader:
            self.logger.info(f'Worker {self.id} assuming leadership')

        for job in jobs:
            j = job.Job()

            self.jobs.append(j)

            if is_leader and j.schedule:
                self.logger.info(f'Registering Cron Job {j.name}')

                next_execution = self.scheduler.get_next_execution(j.schedule).timestamp()
                scheduled_job = False
                running_job = None
                try:
                    pending = self.redis.zscan('nuts|jobs|pending', match=j.name)
                    if pending[1][0]:
                        if pending[1][1] != next_execution:
                            # Schedule has changed
                            self.redis.zrem(self.scheduled_queue, job.name)
                            self.redis.zadd(self.scheduled_queue, {job.name: next_execution})
                        else:
                            scheduled_job = True

                except Exception:
                    pass

                running_job = self.redis.hget(self.running_list, j.name)

                if not scheduled_job and not running_job:
                    self.redis.zadd(self.pending_queue, {j.name: next_execution})

    def check_leader(self) -> bool:
        is_leader = False
        leadership_check = self.redis.setnx('leader_id', self.id)

        if not leadership_check:
            leader_id = self.redis.get('leader_id')
            if leader_id == self.id:
                self.redis.expire('leader_id', 600)

                is_leader = True

        else:
            self.redis.expire('leader_id', 600)
            is_leader = True

        return is_leader

    def schedule_pending_job(self, job_name):
        self.redis.sadd(self.pending_queue, json.dumps([job_name, []]))

    def move_scheduled_to_pending(self):
        now = round(datetime.datetime.now(datetime.timezone.utc).timestamp())
        start = round(self.last_run.timestamp())
        ready_jobs = self.redis.zrange(self.scheduled_queue, start=start, end=now, withscores=True)

        if len(ready_jobs) > 0:
            latest = max(j[0] for j in ready_jobs)
            self.redis.zremrangebyscore(self.scheduled_queue, min=self.last_run.timestamp(), max=latest)

        for job in ready_jobs:
            self.schedule_pending_job(job[0])

    def move_pending_to_running(self, job: NutsJob, job_args: list):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.redis.hset(self.running_list, f'{self.id}|{job.name}', json.dumps({'timestamp': now, 'args': job_args}))

    def remove_running(self, job: NutsJob):

        self.redis.hdel(self.running_list, f'{self.id}|{job.name}')

    def move_to_completed(self, job: NutsJob):
        self.redis.sadd(self.completed_queue, job.name)

    def queue_completed_jobs(self):

        completed_jobs = self.redis.smembers(self.completed_queue)

        for job_name in completed_jobs:
            self.redis.srem(self.completed_queue, job_name)
            job = [j for j in self.jobs if j.name == job_name.decode()][0]

            next_execution = self.scheduler.get_next_execution(job.schedule).timestamp()

            self.redis.zadd(self.scheduled_queue, {job.name: next_execution})

    def run(self):

        is_leader = self.check_leader()
        if is_leader:
            # Move ready jobs to the pending queue
            self.move_scheduled_to_pending()

        try:
            data = self.redis.spop(self.pending_queue, 1)
            [job_name, job_args] = json.loads(data[0])

            job = [j for j in self.jobs if j.name == job_name][0]

            self.move_pending_to_running(job, job_args)

        except Exception as ex:
            self.logger.error(ex)

        try:
            job.run(**job_args, **self.kwargs)

            self.remove_running(job)

            if job.success:
                msg = f'SUCCESS: {job.name}, {job.result}'

                self.logger.info(msg)
            else:
                self.logger.error(f'Error running job {job.name}: {job.error}')
            if job.schedule:
                self.move_job_to_completed(job)

        except Exception as ex:
            msg = f'Unhandled Error in job {job.name}: {ex}'
            self.logger.error(ex)

        # Post Execution

        if is_leader:
            self.queue_completed_jobs()

