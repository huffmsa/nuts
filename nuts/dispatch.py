"""
The one generalized dispatch/delay primitive used by every kind of work this
library moves: cron jobs, delayed ad-hoc jobs, retry backoff, DAG `job.next`
chaining, batch flushes, and workflow-instance steps. All of them are "a
payload flowing through a priority stream, optionally staged in a due-time
sorted set first."
"""
import json
import datetime
from typing import Union
from uuid import uuid4
from redis import Redis

PRIORITIES = ('high', 'normal', 'low')
STREAM_PREFIX = 'nuts|stream|'
DLQ_STREAM = 'nuts|jobs|dlq'
HISTORY_STREAM = 'nuts|jobs|history'
DELAYED_QUEUE = 'nuts|jobs|delayed'
CANCELLED_PREFIX = 'nuts|jobs|cancelled|'
CANCEL_TTL_SECONDS = 604800


def stream_key(priority: str) -> str:
    if priority not in PRIORITIES:
        raise ValueError(f'Unknown priority {priority!r}, must be one of {PRIORITIES}')
    return f'{STREAM_PREFIX}{priority}'


def new_dispatch_id() -> str:
    return uuid4().hex


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
    batch: Union[dict, None] = None,
) -> dict:
    stream_key(priority)
    return {
        'dispatch_id': new_dispatch_id(),
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
        'batch': batch,
    }


def holder_token(payload: dict) -> str:
    '''Identifies a dispatch across retries and redeliveries. Payloads from before dispatch_id existed fall back to name + creation time.'''
    return payload.get('dispatch_id') or f"{payload['job_name']}:{payload.get('created_at')}"


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


def due_items(redis: Redis, zset_key: str, now_ts: float, limit: int = 1000) -> list[dict]:
    '''
        Pop payloads in zset_key whose score (due timestamp) is <= now_ts.
        Per-item ZREM guards against a double-leader edge case during a
        leadership handoff: only a payload this call actually removed is
        returned.
    '''
    candidates = redis.zrange(zset_key, start=0, end=now_ts, byscore=True, offset=0, num=limit)
    items = []
    for member in candidates:
        if redis.zrem(zset_key, member):
            items.append(json.loads(member))
    return items


def ack(redis: Redis, stream: str, group: str, entry_id) -> None:
    '''XACK and delete the entry. Streams don't drop acked entries on their own, so without XDEL they grow forever.'''
    pipe = redis.pipeline(transaction=False)
    pipe.xack(stream, group, entry_id)
    pipe.xdel(stream, entry_id)
    pipe.execute()


def to_dlq(redis: Redis, payload: dict, error: str) -> None:
    payload = dict(payload, error=error)
    redis.xadd(DLQ_STREAM, {'data': json.dumps(payload)})


def record_history(redis: Redis, entry: dict, max_length: int) -> None:
    '''Append to a capped log of finished dispatches. Exists only for visibility; nothing reads it to make decisions.'''
    if max_length > 0:
        redis.xadd(HISTORY_STREAM, {'data': json.dumps(entry)}, maxlen=max_length, approximate=True)


def cancel(redis: Redis, dispatch_id: str) -> None:
    '''Cancel one dispatch by id. It's skipped when a worker next picks it up, including retries and parked entries.'''
    redis.set(f'{CANCELLED_PREFIX}{dispatch_id}', '1', ex=CANCEL_TTL_SECONDS)


def is_cancelled(redis: Redis, dispatch_id: Union[str, None]) -> bool:
    return bool(dispatch_id) and bool(redis.exists(f'{CANCELLED_PREFIX}{dispatch_id}'))
