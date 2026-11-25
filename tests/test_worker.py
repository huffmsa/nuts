import time
import json
from ..nuts.worker import Worker, RetryPolicy
from ..nuts.job import NutsJob
from ..nuts.queue import WorkQueue
from ..nuts import dispatch


def _worker(redis, jobs, **kwargs):
    return Worker(redis=redis, jobs=jobs, **kwargs)


def test_worker_is_leader_by_default(redis):
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Noop'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [Mod])
    assert worker.is_leader


def test_cron_job_registered_and_drained_into_stream(redis):
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'CronJob'
                self.schedule = '0 * * * * ? *'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [Mod])
    assert redis.zrange(worker.scheduled_queue, 0, -1) == [b'CronJob']

    # Force it due right now and drain.
    redis.zrem(worker.scheduled_queue, 'CronJob')
    redis.zadd(worker.scheduled_queue, {'CronJob': time.time() - 1})
    worker.drain_due_cron_jobs()

    entries = redis.xrange(dispatch.stream_key('normal'))
    assert len(entries) == 1
    payload = json.loads(entries[0][1][b'data'])
    assert payload['job_name'] == 'CronJob'


def test_cron_registration_is_restart_safe(redis):
    """Worker restarts must not pile up duplicate due-time entries."""
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'CronJob'
                self.schedule = '0 * * * * ? *'

            def run(self, **kwargs):
                self.success = True

    w1 = _worker(redis, [Mod])
    assert redis.zcard(w1.scheduled_queue) == 1

    redis.delete('leader_id')  # simulate the old leader dying
    w2 = _worker(redis, [Mod])
    assert w2.is_leader
    assert redis.zcard(w1.scheduled_queue) == 1


def test_claim_execute_ack_removes_work(redis):
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'DoThing'

            def run(self, **kwargs):
                self.result = kwargs.get('x', 0) + 1
                self.success = True

    worker = _worker(redis, [Mod])
    WorkQueue(redis).publish('DoThing', {'x': 1})

    worker.run()

    pending = redis.xpending(dispatch.stream_key('normal'), worker.consumer_group)
    assert pending['pending'] == 0


def test_job_next_dag_chaining(redis):
    seen = []

    class FirstMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'First'
                self.next = None

            def run(self, **kwargs):
                self.result = {'value': kwargs.get('value', 0) + 1}
                self.success = True
                self.next = 'Second'

    class SecondMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Second'

            def run(self, **kwargs):
                seen.append(kwargs.get('value'))
                self.success = True

    worker = _worker(redis, [FirstMod, SecondMod])
    WorkQueue(redis).publish('First', {'value': 10})

    worker.run()
    worker.run()

    assert seen == [11]
    assert redis.xlen(dispatch.stream_key('normal')) == 0


def test_priority_order_is_strict(redis):
    order = []

    class LowMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'LowJob'

            def run(self, **kwargs):
                order.append('low')
                self.success = True

    class HighMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'HighJob'

            def run(self, **kwargs):
                order.append('high')
                self.success = True

    worker = _worker(redis, [LowMod, HighMod])
    q = WorkQueue(redis)
    q.publish('LowJob', priority='low')
    q.publish('HighJob', priority='high')

    # Both streams have one entry each; consume_one must drain 'high' first
    # even though 'low' was published first.
    worker.consume_one()
    worker.consume_one()
    assert order == ['high', 'low']


def test_retry_then_dlq_on_exhaustion(redis):
    class AlwaysFailMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'AlwaysFail'

            def run(self, **kwargs):
                raise RuntimeError('boom')

    worker = _worker(redis, [AlwaysFailMod], retry_policy=RetryPolicy(base=0.01, cap=0.05, max_attempts=2))
    WorkQueue(redis).publish('AlwaysFail', max_attempts=2)

    worker.run()  # attempt 1 fails -> scheduled for retry
    assert redis.zcard(worker.retry_queue) == 1

    time.sleep(0.1)
    worker.run()  # leader tick drains the due retry and dispatches attempt 2
    worker.run()  # attempt 2 fails -> exhausted -> DLQ

    dlq = redis.xrange(dispatch.DLQ_STREAM)
    assert len(dlq) == 1
    payload = json.loads(dlq[0][1][b'data'])
    assert payload['job_name'] == 'AlwaysFail'
    assert payload['attempt'] == 2


def test_pel_reclaim_recovers_a_crashed_claim(redis):
    class CrashyMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Crashy'

            def run(self, **kwargs):
                self.success = True

    worker1 = _worker(redis, [CrashyMod], pel_min_idle_ms=50)
    WorkQueue(redis).publish('Crashy')

    stream = dispatch.stream_key('normal')
    # Simulate worker1 claiming the entry via XREADGROUP and then crashing
    # before it ever XACKs.
    redis.xreadgroup(worker1.consumer_group, worker1.id, {stream: '>'}, count=1)

    time.sleep(0.1)  # exceed pel_min_idle_ms

    worker2 = _worker(redis, [CrashyMod], pel_min_idle_ms=50)
    worker2.reclaim_stale_pel_entries()
    worker2.consume_one()

    assert redis.xlen(dispatch.DLQ_STREAM) == 0
    pending_high = redis.xpending(stream, worker2.consumer_group)
    assert pending_high['pending'] == 0


def test_unknown_job_name_is_not_lost_goes_to_dlq_eventually(redis):
    class KnownMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Known'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [KnownMod])
    WorkQueue(redis).publish('TotallyUnknownJob', max_attempts=1)

    worker.run()

    dlq = redis.xrange(dispatch.DLQ_STREAM)
    assert len(dlq) == 1
    assert json.loads(dlq[0][1][b'data'])['job_name'] == 'TotallyUnknownJob'


def test_job_cancellation_is_respected(redis):
    class CancelMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Cancellable'

            def run(self, **kwargs):
                raise AssertionError('should never run once cancelled')

    worker = _worker(redis, [CancelMod])
    WorkQueue(redis).publish('Cancellable')
    redis.sadd(worker.cancel_queue, 'Cancellable')

    worker.run()  # must not raise

    assert not redis.sismember(worker.cancel_queue, 'Cancellable')
