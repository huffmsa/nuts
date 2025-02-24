from redis import Redis
from ..nuts.queue import WorkQueue

r = Redis()


def test_queue():
    r.flushall()

    wq = WorkQueue(r)

    wq.publish('TestQueuJob', {})

    assert 1 == r.scard(wq.pending_queue)
