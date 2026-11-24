from ..nuts.queue import WorkQueue
from ..nuts import dispatch


def test_publish_immediate_dispatches_to_priority_stream(redis):
    wq = WorkQueue(redis)

    wq.publish('TestQueueJob', {'x': 1})

    entries = redis.xrange(dispatch.stream_key('normal'))
    assert len(entries) == 1


def test_publish_delayed_lands_in_zset_not_stream(redis):
    wq = WorkQueue(redis)

    wq.publish('DelayedJob', {'y': 2}, delay_seconds=3600)

    assert redis.xlen(dispatch.stream_key('normal')) == 0
    assert redis.zcard(wq.delayed_queue) == 1


def test_publish_routes_by_priority(redis):
    wq = WorkQueue(redis)

    wq.publish('HighJob', priority='high')
    wq.publish('LowJob', priority='low')

    assert redis.xlen(dispatch.stream_key('high')) == 1
    assert redis.xlen(dispatch.stream_key('low')) == 1
    assert redis.xlen(dispatch.stream_key('normal')) == 0


def test_publish_carries_concurrency_fields(redis):
    wq = WorkQueue(redis)

    wq.publish('GatedJob', concurrency_key='vendor:1', concurrency_limit=3)

    entries = redis.xrange(dispatch.stream_key('normal'))
    import json
    payload = json.loads(entries[0][1][b'data'])
    assert payload['concurrency_key'] == 'vendor:1'
    assert payload['concurrency_limit'] == 3
