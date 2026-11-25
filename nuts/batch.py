"""
Batch jobs: callers append items one at a time, and the job runs once per
group of items (up to max_items, or whatever has arrived after
window_seconds). Items sharing a job, priority and batch_key are grouped.

A flush is an ordinary payload on the priority streams, so it gets the same
priority, concurrency limits, retries, heartbeats and reclaim as any job.
"""
import datetime
import json
from typing import Union
from redis import Redis
from . import dispatch
from . import lua

PREFIX = 'nuts|batch|'


def buffer_key(job_name: str, priority: str, batch_key: str) -> str:
    return f'{PREFIX}buffer|{job_name}|{priority}|{batch_key}'


def marker_key(job_name: str, priority: str, batch_key: str) -> str:
    return f'{PREFIX}flush|{job_name}|{priority}|{batch_key}'


def inflight_key(dispatch_id: str) -> str:
    return f'{PREFIX}inflight|{dispatch_id}'


def new_item(params: Union[dict, None], attempt: int = 1) -> dict:
    return {'item_id': dispatch.new_dispatch_id(), 'params': params or {}, 'attempt': attempt}


def flush_payload(
    job_name: str,
    priority: str,
    batch_key: str,
    max_items: int,
    window_seconds: float,
    max_attempts: int,
    concurrency_key: Union[str, None],
    concurrency_limit: int,
) -> dict:
    return dispatch.build_payload(
        job_name,
        priority=priority,
        max_attempts=max_attempts,
        concurrency_key=concurrency_key,
        concurrency_limit=concurrency_limit,
        batch={
            'buffer_key': buffer_key(job_name, priority, batch_key),
            'marker_key': marker_key(job_name, priority, batch_key),
            'batch_key': batch_key,
            'max_items': max_items,
            'window_seconds': window_seconds,
        },
    )


def append(
    redis: Redis,
    job_name: str,
    params: Union[dict, None],
    *,
    batch_key: str,
    max_items: int,
    window_seconds: float,
    priority: str,
    max_attempts: int,
    concurrency_key: Union[str, None],
    concurrency_limit: int,
) -> str:
    item = new_item(params)

    def flush():
        return flush_payload(job_name, priority, batch_key, max_items, window_seconds, max_attempts, concurrency_key, concurrency_limit)

    due = datetime.datetime.now(datetime.timezone.utc).timestamp() + window_seconds
    lua.batch_append(
        redis,
        buffer_key(job_name, priority, batch_key),
        marker_key(job_name, priority, batch_key),
        dispatch.stream_key(priority),
        dispatch.DELAYED_QUEUE,
        item,
        max_items,
        flush(),
        flush(),
        due,
        int(window_seconds) + 60,
    )
    return item['item_id']


def take(redis: Redis, payload: dict) -> tuple[list[dict], int]:
    '''Claim this flush's items. Returns (items, items still waiting in the buffer).'''
    b = payload['batch']
    return lua.batch_take(redis, b['buffer_key'], inflight_key(payload['dispatch_id']), b['marker_key'], b['max_items'])


def follow_up(payload: dict) -> dict:
    '''A fresh flush for the same batch, used when items are left in the buffer.'''
    return dict(payload, dispatch_id=dispatch.new_dispatch_id(), attempt=1)


def schedule_window_flush(redis: Redis, payload: dict) -> None:
    '''Make sure items left in the buffer get a flush within the window, unless one is already coming.'''
    b = payload['batch']
    window = b['window_seconds']
    if redis.set(b['marker_key'], '1', nx=True, ex=int(window) + 60):
        due = datetime.datetime.now(datetime.timezone.utc).timestamp() + window
        dispatch.dispatch_later(redis, dispatch.DELAYED_QUEUE, follow_up(payload), due)


def retry_items(redis: Redis, payload: dict, items: list[dict]) -> dict:
    '''A new flush preloaded with just these items, so they retry without waiting on the shared buffer.'''
    flush = follow_up(payload)
    key = inflight_key(flush['dispatch_id'])
    pipe = redis.pipeline()
    for item in items:
        pipe.rpush(key, json.dumps(item))
    pipe.expire(key, 604800)
    pipe.execute()
    return flush


def done(redis: Redis, payload: dict) -> None:
    redis.delete(inflight_key(payload['dispatch_id']))
