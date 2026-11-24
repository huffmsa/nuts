"""
The one generalized dispatch/delay primitive used by every kind of work this
library moves: cron jobs, delayed ad-hoc jobs, retry backoff, DAG `job.next`
chaining, and workflow-instance steps. All of them are "a payload flowing
through a priority stream, optionally staged in a due-time sorted set first."
"""
import json
import datetime
from typing import Any, Union
from redis import Redis

PRIORITIES = ('high', 'normal', 'low')
STREAM_PREFIX = 'nuts|stream|'
DLQ_STREAM = 'nuts|jobs|dlq'


def stream_key(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ValueError(f'Unknown priority {priority!r}, must be one of {PRIORITIES}')
    return f'{STREAM_PREFIX}{priority}'


def build_payload(
    job_name: str,
    params: Union[dict, None] = None,
    priority: str = 'normal',
    attempt: int = 1,
    max_attempts: int = 5,
    instance_id: Union[str, None] = None,
    step_id: Union[str, None] = None,
    concurrency_key: Union[str, None] = None,
    concurrency_limit: int = 1,
) -> dict:
    return {
        'job_name': job_name,
        'params': params or {},
        'priority': priority,
        'attempt': attempt,
        'max_attempts': max_attempts,
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'instance_id': instance_id,
        'step_id': step_id,
        'concurrency_key': concurrency_key,
        'concurrency_limit': concurrency_limit,
    }


def enqueue_payload(redis: Redis, payload: dict) -> bytes:
    '''XADD an already-built payload into its priority stream.'''
    return redis.xadd(stream_key(payload['priority']), {'data': json.dumps(payload)})


def dispatch_now(redis: Redis, job_name: str, params: Union[dict, None] = None, priority: str = 'normal', **meta) -> bytes:
    '''Build a payload and immediately push it onto the right priority stream.'''
    payload = build_payload(job_name, params, priority, **meta)
    return enqueue_payload(redis, payload)


def dispatch_later(redis: Redis, zset_key: str, payload: dict, due_ts: float) -> None:
    '''Stage a payload in a due-time sorted set instead of dispatching it now.'''
    redis.zadd(zset_key, {json.dumps(payload): due_ts})


def due_items(redis: Redis, zset_key: str, now_ts: float) -> list[dict]:
    '''
        Atomically pop every payload in zset_key whose score (due timestamp)
        is <= now_ts. Per-item ZREM (rather than a single zremrangebyscore)
        guards against a double-leader edge case during a leadership handoff:
        only a payload this call actually removed is returned.
    '''
    candidates = redis.zrange(zset_key, start=0, end=now_ts, byscore=True)
    items = []
    for member in candidates:
        if redis.zrem(zset_key, member):
            items.append(json.loads(member))
    return items


def to_dlq(redis: Redis, payload: dict, error: str) -> None:
    payload = dict(payload, error=error)
    redis.xadd(DLQ_STREAM, {'data': json.dumps(payload)})
