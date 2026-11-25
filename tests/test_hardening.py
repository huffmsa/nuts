import json
import threading
import time
import pytest
from nuts.worker import Worker, RetryPolicy
from nuts.job import NutsJob
from nuts.queue import WorkQueue
from nuts import dispatch
from nuts import instance as inst


def make_job(name, fn, **attrs):
    class Mod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = name
                for k, v in attrs.items():
                    setattr(self, k, v)

            def run(self, **kwargs):
                fn(self, **kwargs)
    return Mod


def ok(job, **kwargs):
    job.success = True


def normal_stream():
    return dispatch.stream_key('normal')


def drain(worker, ticks=10, delay=0.02):
    for _ in range(ticks):
        worker.run()
        time.sleep(delay)


def test_immediate_publish_respects_concurrency_limit(redis):
    ran = []
    other = {}

    def first(job, n, **kwargs):
        ran.append(n)
        if n == 1:
            # While job 1 holds the only slot, a second worker picks up job 2.
            other['worker'].consume_one()
            assert ran == [1]
        job.success = True

    mod = make_job('Gated', first)
    w1 = Worker(redis, [mod])
    other['worker'] = Worker(redis, [mod])
    q = WorkQueue(redis)
    q.publish('Gated', {'n': 1}, concurrency_key='partner-api', concurrency_limit=1)
    q.publish('Gated', {'n': 2}, concurrency_key='partner-api', concurrency_limit=1)

    w1.consume_one()
    assert ran == [1]

    w1.consume_one()
    assert ran == [1, 2]
    assert redis.zcard(w1.concurrency.holders_key('partner-api')) == 0
    assert redis.llen(w1.concurrency.waiting_key('partner-api')) == 0


def test_parked_jobs_get_slots_in_arrival_order(redis):
    ran = []
    mod = make_job('Gated', lambda job, n, **kw: (ran.append(n), setattr(job, 'success', True)))
    worker = Worker(redis, [mod])
    q = WorkQueue(redis)
    worker.concurrency.acquire('k', 'blocker', limit=1, lease_seconds=60)
    for n in range(4):
        q.publish('Gated', {'n': n}, concurrency_key='k', concurrency_limit=1)

    drain(worker, ticks=6)
    assert ran == []

    worker.concurrency.release('k', 'blocker')
    drain(worker, ticks=8)
    assert ran == [0, 1, 2, 3]


def test_expired_slot_is_handed_to_waiter_by_leader(redis):
    ran = []
    mod = make_job('Gated', lambda job, **kw: (ran.append(1), setattr(job, 'success', True)))
    worker = Worker(redis, [mod])
    worker.concurrency.acquire('k', 'crashed-holder', limit=1, lease_seconds=0.2)
    WorkQueue(redis).publish('Gated', concurrency_key='k', concurrency_limit=1)

    worker.run()
    assert ran == []
    time.sleep(0.3)
    drain(worker, ticks=3)
    assert ran == [1]


def test_long_running_job_is_not_reclaimed_while_alive(redis):
    runs = []

    def slow(job, **kwargs):
        runs.append(1)
        time.sleep(0.4)
        job.success = True

    mod = make_job('Slow', slow)
    w1 = Worker(redis, [mod], pel_min_idle_ms=100)
    leader = Worker(redis, [mod], pel_min_idle_ms=100)
    WorkQueue(redis).publish('Slow')

    t = threading.Thread(target=w1.consume_one)
    t.start()
    time.sleep(0.25)
    leader.reclaim_stale_pel_entries()
    t.join()
    leader.consume_one()

    assert runs == [1]
    assert redis.xlen(normal_stream()) == 0


def test_job_past_its_timeout_is_reclaimed(redis):
    def slow(job, **kwargs):
        time.sleep(0.4)
        job.success = True

    mod = make_job('Slow', slow, timeout_seconds=0.05)
    w1 = Worker(redis, [mod], pel_min_idle_ms=100)
    leader = Worker(redis, [mod], pel_min_idle_ms=100)
    WorkQueue(redis).publish('Slow')

    t = threading.Thread(target=w1.consume_one)
    t.start()
    time.sleep(0.3)
    leader.reclaim_stale_pel_entries()
    t.join()

    entries = redis.xrange(normal_stream())
    assert len(entries) == 1
    assert json.loads(entries[0][1][b'data'])['attempt'] == 2


def test_acked_entries_are_removed_from_streams(redis):
    worker = Worker(redis, [make_job('Noop', ok)])
    q = WorkQueue(redis)
    for _ in range(50):
        q.publish('Noop')
    for _ in range(50):
        worker.consume_one()
    assert redis.xlen(normal_stream()) == 0


def test_idle_waiting_instance_is_not_rewritten(redis):
    worker = Worker(redis, [])
    instance_id = inst.create_instance(redis, 'wf', [{'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received'}])
    worker.sweep_active_workflow_instances()
    version = inst.get_instance(redis, instance_id)['_v']
    for _ in range(5):
        worker.sweep_active_workflow_instances()
        worker.advance_dirty_instances()
    assert inst.get_instance(redis, instance_id)['_v'] == version


def test_leader_only_touches_dirty_instances(redis):
    ran = []
    worker = Worker(redis, [make_job('Apply', lambda job, order, **kw: (ran.append(order), setattr(job, 'success', True)))])
    plan = [
        {'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received'},
        {'step_id': 'apply', 'op': 'Apply', 'requires': ['paid']},
    ]
    ids = [inst.create_instance(redis, 'fulfilment', plan, {'order': n}) for n in range(300)]
    worker.advance_dirty_instances()
    assert redis.scard(inst.DIRTY_SET) == 0

    inst.signal_instance(redis, ids[7], 'payment_received')
    assert redis.smembers(inst.DIRTY_SET) == {ids[7].encode()}

    drain(worker, ticks=4)
    assert ran == [7]
    assert inst.get_instance(redis, ids[7])['status'] == 'completed'
    assert inst.get_instance(redis, ids[8])['status'] == 'active'


def test_entity_key_lookup_signal_and_cleanup(redis):
    worker = Worker(redis, [make_job('Apply', ok)])
    plan = [
        {'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received'},
        {'step_id': 'apply', 'op': 'Apply', 'requires': ['paid']},
    ]
    instance_id = inst.create_instance(redis, 'fulfilment', plan, entity_key='order:42')
    assert inst.get_instance_id_for_entity(redis, 'order:42') == instance_id

    with pytest.raises(inst.EntityBusyError):
        inst.create_instance(redis, 'fulfilment', plan, entity_key='order:42')

    assert inst.signal_entity(redis, 'order:42', 'payment_received') == instance_id
    drain(worker, ticks=4)

    assert inst.get_instance(redis, instance_id)['status'] == 'completed'
    assert inst.get_instance_id_for_entity(redis, 'order:42') is None
    assert inst.signal_entity(redis, 'order:42', 'payment_received') is None


def test_replace_cancels_previous_instance_for_entity(redis):
    ran = []
    worker = Worker(redis, [make_job('Apply', lambda job, op, **kw: (ran.append(op), setattr(job, 'success', True)))])
    plan = [
        {'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received'},
        {'step_id': 'apply', 'op': 'Apply', 'requires': ['paid']},
    ]
    old = inst.create_instance(redis, 'fulfilment', plan, {'op': 'old'}, entity_key='order:1')
    new = inst.create_instance(redis, 'fulfilment', plan, {'op': 'new'}, entity_key='order:1', replace=True)

    assert inst.get_instance(redis, old)['status'] == 'cancelled'
    inst.signal_entity(redis, 'order:1', 'payment_received')
    drain(worker, ticks=4)

    assert ran == ['new']
    assert inst.get_instance(redis, new)['status'] == 'completed'


def test_cancelled_instance_skips_its_queued_step(redis):
    ran = []
    worker = Worker(redis, [make_job('Step', lambda job, **kw: (ran.append(1), setattr(job, 'success', True)))])
    instance_id = inst.create_instance(redis, 'wf', [{'step_id': 's', 'op': 'Step'}])
    worker.advance_dirty_instances()
    assert redis.xlen(normal_stream()) == 1

    inst.cancel_instance(redis, instance_id, reason='operator')
    worker.consume_one()

    assert ran == []
    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'cancelled'
    assert doc['error'] == 'operator'


def test_wait_step_times_out(redis):
    worker = Worker(redis, [])
    instance_id = inst.create_instance(redis, 'wf', [
        {'step_id': 'paid', 'kind': 'wait', 'event': 'payment_received', 'timeout_seconds': 0.1},
    ], entity_key='order:9')
    worker.run()
    assert inst.get_instance(redis, instance_id)['status'] == 'active'

    time.sleep(0.2)
    worker.run()

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'failed'
    assert 'timed out' in doc['error']
    assert inst.get_instance_id_for_entity(redis, 'order:9') is None


def test_cancel_by_dispatch_id(redis):
    ran = []
    worker = Worker(redis, [make_job('Job', lambda job, n, **kw: (ran.append(n), setattr(job, 'success', True)))])
    q = WorkQueue(redis)
    first = q.publish('Job', {'n': 1})
    q.publish('Job', {'n': 2})
    q.cancel(first)

    worker.consume_one()
    worker.consume_one()
    assert ran == [2]


def test_batch_runs_full_batches_then_the_window_remainder(redis):
    batches = []
    worker = Worker(redis, [make_job('Merge', lambda job, items, **kw: (batches.append([i['order'] for i in items]), setattr(job, 'success', True)))])
    q = WorkQueue(redis)
    for order in range(5):
        q.publish_to_batch('Merge', {'order': order}, batch_key='partner-api', max_items=3, window_seconds=0.2)

    drain(worker, ticks=3)
    assert batches == [[0, 1, 2]]

    time.sleep(0.25)
    drain(worker, ticks=3)
    assert batches == [[0, 1, 2], [3, 4]]


def test_batch_retries_only_failed_items_then_dead_letters_them(redis):
    batches = []

    def merge(job, items, **kwargs):
        batches.append([i['order'] for i in items])
        job.failed_items = {n: 'partner API rejected' for n, i in enumerate(items) if i['order'] == 1}
        job.success = True

    worker = Worker(redis, [make_job('Merge', merge)], retry_policy=RetryPolicy(base=0.01, cap=0.02))
    q = WorkQueue(redis)
    for order in range(3):
        q.publish_to_batch('Merge', {'order': order}, max_items=3, window_seconds=60, max_attempts=2)

    drain(worker, ticks=8)

    assert batches == [[0, 1, 2], [1]]
    dlq = [json.loads(e[1][b'data']) for e in redis.xrange(dispatch.DLQ_STREAM)]
    assert [(d['params'], d['error']) for d in dlq] == [({'order': 1}, 'partner API rejected')]
    assert not redis.keys('nuts|batch|inflight|*')


def test_batch_exception_retries_same_items(redis):
    calls = []

    def flaky(job, items, **kwargs):
        calls.append([i['order'] for i in items])
        if len(calls) == 1:
            raise RuntimeError('timeout')
        job.success = True

    worker = Worker(redis, [make_job('Merge', flaky)], retry_policy=RetryPolicy(base=0.01, cap=0.02))
    q = WorkQueue(redis)
    for order in range(2):
        q.publish_to_batch('Merge', {'order': order}, max_items=2)
    q.publish_to_batch('Merge', {'order': 99}, max_items=2, window_seconds=60)

    drain(worker, ticks=8)
    assert calls[:2] == [[0, 1], [0, 1]]


def test_new_leader_registers_cron_jobs(redis):
    cron = make_job('Nightly', ok, schedule='0 0 2 ? * * *')
    first = Worker(redis, [])
    second = Worker(redis, [cron])
    assert first.is_leader and not second.is_leader
    assert redis.zcard(second.scheduled_queue) == 0

    first.release_leader()
    second.check_leader()

    assert second.is_leader
    assert redis.zrange(second.scheduled_queue, 0, -1) == [b'Nightly']


def test_serve_runs_jobs_concurrently_and_drains_on_shutdown(redis):
    done = []
    lock = threading.Lock()

    def slow(job, n, **kwargs):
        time.sleep(0.3)
        with lock:
            done.append(n)
        job.success = True

    worker = Worker(redis, [make_job('Slow', slow)], max_concurrent_jobs=4, block_ms=100, leader_interval_seconds=0.05)
    q = WorkQueue(redis)
    for n in range(8):
        q.publish('Slow', {'n': n})

    started = time.monotonic()
    t = threading.Thread(target=worker.serve)
    t.start()
    while len(done) < 8 and time.monotonic() - started < 5:
        time.sleep(0.02)
    elapsed = time.monotonic() - started
    worker.shutdown(None, None)
    t.join(timeout=5)

    assert sorted(done) == list(range(8))
    assert elapsed < 1.2
    assert not t.is_alive()
    assert redis.get('leader_id') is None


def test_idle_serve_does_not_spin(redis):
    worker = Worker(redis, [], block_ms=200, leader_interval_seconds=0.5)
    redis.config_resetstat()
    t = threading.Thread(target=worker.serve)
    t.start()
    time.sleep(1)
    worker.shutdown(None, None)
    t.join(timeout=5)

    stats = redis.info('commandstats')
    reads = stats.get('cmdstat_xreadgroup', {}).get('calls', 0)
    assert reads < 40
