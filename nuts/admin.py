"""
Read and act on a NUTS deployment's state in Redis. Used by the web UI, and
usable directly from scripts or a shell.
"""
import datetime
import json
from typing import Any, Union
from redis import Redis

from . import dispatch
from . import instance
from . import batch
from .concurrency import WAITING_KEYS_SET
from .queue import WorkQueue
from .worker import LEADER_KEY

CONSUMER_GROUP = 'nuts|workers'
SCHEDULED_JOBS = 'nuts|jobs|scheduled'
SCHEDULED_WORKFLOWS = 'nuts|workflows|scheduled'
RETRY_QUEUE = 'nuts|retry|pending'
CONCURRENCY_PREFIX = 'nuts|concurrency|'
CONCURRENCY_WAITING_PREFIX = 'nuts|concurrency|waiting|'


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _entry_data(fields) -> Union[dict, None]:
    raw = (fields or {}).get(b'data', (fields or {}).get('data'))
    return json.loads(raw) if raw is not None else None


def _group_info(redis: Redis, stream: str) -> Union[dict, None]:
    try:
        groups = redis.xinfo_groups(stream)
    except Exception:
        return None
    for group in groups:
        if _text(group['name']) == CONSUMER_GROUP:
            return group
    return None


# ---- Overview ----

def overview(redis: Redis) -> dict:
    streams = []
    for priority in dispatch.PRIORITIES:
        stream = dispatch.stream_key(priority)
        group = _group_info(redis, stream) or {}
        in_flight = int(group.get('pending', 0))
        streams.append({
            'priority': priority,
            'queued': max(0, redis.xlen(stream) - in_flight),
            'in_flight': in_flight,
        })

    pipe = redis.pipeline(transaction=False)
    pipe.get(LEADER_KEY)
    pipe.ttl(LEADER_KEY)
    pipe.zcard(dispatch.DELAYED_QUEUE)
    pipe.zcard(RETRY_QUEUE)
    pipe.xlen(dispatch.DLQ_STREAM)
    pipe.scard(instance.ACTIVE_SET)
    pipe.scard(instance.DIRTY_SET)
    pipe.zcard(SCHEDULED_JOBS)
    pipe.zcard(SCHEDULED_WORKFLOWS)
    pipe.scard(WAITING_KEYS_SET)
    leader, leader_ttl, delayed, retrying, dead, active, dirty, cron, templates, gated = pipe.execute()

    return {
        'leader': _text(leader),
        'leader_ttl': leader_ttl,
        'streams': streams,
        'delayed': delayed,
        'retrying': retrying,
        'dead': dead,
        'active_instances': active,
        'dirty_instances': dirty,
        'cron_jobs': cron,
        'workflow_templates': templates,
        'keys_with_waiters': gated,
        'workers': workers(redis),
    }


def workers(redis: Redis, active_within_ms: int = 120000) -> list[dict]:
    '''Consumers seen recently on any priority stream, with how many entries each holds.'''
    seen: dict[str, dict] = {}
    for priority in dispatch.PRIORITIES:
        try:
            consumers = redis.xinfo_consumers(dispatch.stream_key(priority), CONSUMER_GROUP)
        except Exception:
            continue
        for c in consumers:
            name = _text(c['name'])
            w = seen.setdefault(name, {'id': name, 'holding': 0, 'idle_ms': c['idle']})
            w['holding'] += int(c['pending'])
            w['idle_ms'] = min(w['idle_ms'], c['idle'])
    return sorted((w for w in seen.values() if w['idle_ms'] <= active_within_ms), key=lambda w: w['id'])


# ---- Jobs ----

def queued(redis: Redis, priority: str, limit: int = 100) -> list[dict]:
    '''Entries not yet handed to any worker, oldest first.'''
    stream = dispatch.stream_key(priority)
    group = _group_info(redis, stream)
    start = f"({_text(group['last-delivered-id'])}" if group else '-'
    return [
        dict(_entry_data(fields) or {}, entry_id=_text(entry_id))
        for entry_id, fields in redis.xrange(stream, min=start, max='+', count=limit)
    ]


def in_flight(redis: Redis, limit: int = 100) -> list[dict]:
    '''Entries held by a worker: running, waiting in its pool, or held by a worker that died.'''
    rows = []
    for priority in dispatch.PRIORITIES:
        stream = dispatch.stream_key(priority)
        try:
            pending = redis.xpending_range(stream, CONSUMER_GROUP, min='-', max='+', count=limit)
        except Exception:
            continue
        if not pending:
            continue
        pipe = redis.pipeline(transaction=False)
        for p in pending:
            pipe.xrange(stream, min=p['message_id'], max=p['message_id'])
        for p, entries in zip(pending, pipe.execute()):
            payload = _entry_data(entries[0][1]) if entries else None
            rows.append(dict(payload or {}, entry_id=_text(p['message_id']), worker_id=_text(p['consumer']),
                             idle_ms=p['time_since_delivered'], deliveries=p['times_delivered'],
                             priority=priority))
    return rows[:limit]


def parked(redis: Redis, limit: int = 100) -> list[dict]:
    '''Jobs a worker picked up but couldn't start because their concurrency key was full, in the order they'll get slots.'''
    rows = []
    for raw_key in sorted(redis.smembers(WAITING_KEYS_SET)):
        key = _text(raw_key)
        for raw in redis.lrange(f'{CONCURRENCY_WAITING_PREFIX}{key}', 0, limit - 1):
            rows.append(dict(json.loads(raw), waiting_on=key))
    return rows[:limit]


def _zset_payloads(redis: Redis, key: str, limit: int) -> list[dict]:
    return [dict(json.loads(member), due_at=score) for member, score in redis.zrange(key, 0, limit - 1, withscores=True)]


def delayed(redis: Redis, limit: int = 100) -> list[dict]:
    return _zset_payloads(redis, dispatch.DELAYED_QUEUE, limit)


def retrying(redis: Redis, limit: int = 100) -> list[dict]:
    return _zset_payloads(redis, RETRY_QUEUE, limit)


def _find_in_zset(redis: Redis, key: str, dispatch_id: str) -> Union[bytes, None]:
    for member, _ in redis.zscan_iter(key):
        if json.loads(member).get('dispatch_id') == dispatch_id:
            return member
    return None


def run_now(redis: Redis, dispatch_id: str) -> bool:
    '''Make a delayed or retrying dispatch due immediately. The leader queues it on its next tick.'''
    for key in (dispatch.DELAYED_QUEUE, RETRY_QUEUE):
        member = _find_in_zset(redis, key, dispatch_id)
        if member is not None:
            redis.zadd(key, {member: _now()}, xx=True)
            return True
    return False


def drop_waiting(redis: Redis, dispatch_id: str) -> bool:
    '''Remove a delayed or retrying dispatch before it runs.'''
    for key in (dispatch.DELAYED_QUEUE, RETRY_QUEUE):
        member = _find_in_zset(redis, key, dispatch_id)
        if member is not None:
            return bool(redis.zrem(key, member))
    return False


def cancel(redis: Redis, dispatch_id: str) -> None:
    dispatch.cancel(redis, dispatch_id)


def publish(redis: Redis, job_name: str, params: Union[dict, None] = None, priority: str = 'normal', delay_seconds: float = 0) -> str:
    return WorkQueue(redis).publish(job_name, params, priority=priority, delay_seconds=delay_seconds)


def history(redis: Redis, limit: int = 200, job_name: Union[str, None] = None, outcome: Union[str, None] = None) -> list[dict]:
    rows = []
    for entry_id, fields in redis.xrevrange(dispatch.HISTORY_STREAM, count=limit if not (job_name or outcome) else 5000):
        row = _entry_data(fields) or {}
        if job_name and row.get('job_name') != job_name:
            continue
        if outcome and row.get('outcome') != outcome:
            continue
        rows.append(dict(row, entry_id=_text(entry_id)))
        if len(rows) >= limit:
            break
    return rows


# ---- Schedules ----

def schedules(redis: Redis) -> dict:
    def rows(key):
        return [{'name': _text(n), 'next_run': s} for n, s in redis.zrange(key, 0, -1, withscores=True)]
    return {'jobs': rows(SCHEDULED_JOBS), 'workflows': rows(SCHEDULED_WORKFLOWS)}


def run_schedule_now(redis: Redis, kind: str, name: str) -> bool:
    '''Make a cron job or workflow template due now. Its schedule continues from there.'''
    key = SCHEDULED_JOBS if kind == 'job' else SCHEDULED_WORKFLOWS
    return bool(redis.zadd(key, {name: _now()}, xx=True, ch=True))


# ---- Dead letters ----

def dead_letters(redis: Redis, limit: int = 100) -> list[dict]:
    return [dict(_entry_data(fields) or {}, entry_id=_text(entry_id))
            for entry_id, fields in redis.xrevrange(dispatch.DLQ_STREAM, count=limit)]


def redrive(redis: Redis, entry_id: str) -> str:
    '''
        Queue a dead-lettered job again with a fresh attempt count and
        remove it from the dead-letter stream. Returns the new dispatch id.
        Workflow steps can't be redriven: their instance has already failed.
    '''
    entries = redis.xrange(dispatch.DLQ_STREAM, min=entry_id, max=entry_id)
    if not entries:
        raise KeyError(f'No dead letter {entry_id}')
    payload = _entry_data(entries[0][1])
    if payload.get('instance_id'):
        raise ValueError('Workflow steps belong to a failed instance; create a new instance instead')

    if payload.get('kind') == 'batch_item':
        flush = dispatch.build_payload(
            payload['job_name'],
            priority=payload['priority'],
            max_attempts=payload.get('max_attempts') or 5,
            concurrency_key=payload.get('concurrency_key'),
            concurrency_limit=payload.get('concurrency_limit', 1),
            batch=payload['batch'],
        )
        item = {'item_id': payload['item_id'], 'params': payload['params'], 'attempt': 1}
        key = batch.inflight_key(flush['dispatch_id'])
        redis.rpush(key, json.dumps(item))
        redis.expire(key, 604800)
    else:
        flush = {k: v for k, v in payload.items() if k != 'error'}
        flush.update(dispatch_id=dispatch.new_dispatch_id(), attempt=1)
        if flush.get('batch'):
            # A whole batch that died has no inflight items left; nothing to redo.
            raise ValueError('Dead-lettered batch with no items; nothing to redrive')

    dispatch.enqueue_payload(redis, flush)
    redis.xdel(dispatch.DLQ_STREAM, entry_id)
    return flush['dispatch_id']


def delete_dead_letter(redis: Redis, entry_id: str) -> bool:
    return bool(redis.xdel(dispatch.DLQ_STREAM, entry_id))


def purge_dead_letters(redis: Redis) -> int:
    count = redis.xlen(dispatch.DLQ_STREAM)
    redis.delete(dispatch.DLQ_STREAM)
    return count


# ---- Workflow instances ----

def _load_instances(redis: Redis, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    pipe = redis.pipeline(transaction=False)
    for instance_id in ids:
        pipe.get(instance.instance_key(instance_id))
    return [json.loads(raw) for raw in pipe.execute() if raw is not None]


def active_instances(redis: Redis, limit: int = 200, workflow_name: Union[str, None] = None) -> list[dict]:
    '''Up to limit active instances, newest first. Redis sets are unordered, so a large set is sampled, not paged.'''
    ids = []
    for raw_id in redis.sscan_iter(instance.ACTIVE_SET, count=500):
        ids.append(_text(raw_id))
        if len(ids) >= (limit if not workflow_name else limit * 10):
            break
    docs = _load_instances(redis, ids)
    if workflow_name:
        docs = [d for d in docs if d['workflow_name'] == workflow_name]
    return sorted(docs, key=lambda d: d['created_at'], reverse=True)[:limit]


def finished_instances(redis: Redis, limit: int = 100) -> list[dict]:
    ids = [_text(i) for i in redis.lrange(instance.RECENT_FINISHED, 0, limit - 1)]
    return _load_instances(redis, ids)


def find_instance(redis: Redis, id_or_entity: str) -> Union[str, None]:
    if redis.exists(instance.instance_key(id_or_entity)):
        return id_or_entity
    return instance.get_instance_id_for_entity(redis, id_or_entity)


def instance_detail(redis: Redis, instance_id: str) -> Union[dict, None]:
    doc = instance.get_instance(redis, instance_id)
    if doc is None:
        return None
    signals = {_text(k): json.loads(v) for k, v in redis.hgetall(instance.signal_key(instance_id)).items()}
    return dict(doc, pending_signals=signals)


def cancel_instance(redis: Redis, instance_id: str, reason: str = 'cancelled from admin') -> None:
    instance.cancel_instance(redis, instance_id, reason)


def signal_instance(redis: Redis, instance_id: str, event: str, payload: Any = None) -> bool:
    return instance.signal_instance(redis, instance_id, event, payload)


# ---- Concurrency ----

def concurrency(redis: Redis) -> list[dict]:
    now = _now()
    keys: dict[str, dict] = {}
    for raw in redis.scan_iter(match=f'{CONCURRENCY_PREFIX}*', count=500):
        name = _text(raw)
        if name == WAITING_KEYS_SET:
            continue
        if name.startswith(CONCURRENCY_WAITING_PREFIX):
            key = name[len(CONCURRENCY_WAITING_PREFIX):]
            keys.setdefault(key, {'key': key, 'holders': [], 'waiting': 0})['waiting'] = redis.llen(name)
        else:
            key = name[len(CONCURRENCY_PREFIX):]
            row = keys.setdefault(key, {'key': key, 'holders': [], 'waiting': 0})
            row['holders'] = [
                {'dispatch_id': _text(h), 'lease_seconds_left': round(expiry - now, 1)}
                for h, expiry in redis.zrange(name, 0, -1, withscores=True)
            ]
    return sorted(keys.values(), key=lambda r: r['key'])
