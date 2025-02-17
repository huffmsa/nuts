from ..nuts.worker import Worker
from redis import Redis
from .fixtures.jobs import add_one, scheduled_job
import json

jobs = [add_one, scheduled_job]
r = Redis()


def middleware_func():
    return 'I am middleware'


worker = Worker(redis=r, jobs=jobs, middleware=middleware_func)

r.flushall()


def test_worker():
    r.sadd(worker.pending_queue, json.dumps(['AddOne', {'base': 1}]))

    worker.run()

    assert 1 == 1


def test_scheduled_job():
    r.sadd(worker.completed_queue, 'ScheduledJob')

    worker.queue_completed_jobs()

    scheduled_jobs = r.zscan(worker.scheduled_queue, match='ScheduledJob')
    assert len(scheduled_jobs[1]) == 1
