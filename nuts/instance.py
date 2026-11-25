"""
Durable, per-entity workflow instances. A plan is assembled per-instance from
a known, composable catalog of step/op types supplied by the caller at
creation time -- "skip" is just "the caller didn't include that op," no
runtime conditional-skip logic is needed, and two instances of the same
workflow_name can carry entirely different steps/ordering for free.

Instance state lives entirely in Redis (never in worker memory), so any
process can compute "what's next" from a persisted document alone.

Anything that can move an instance forward (creation, a step finishing, a
signal, a wait timing out) adds it to DIRTY_SET. The leader only looks at
dirty instances, plus an occasional full pass as a safety net.
"""
import copy
import datetime
import json
from typing import Any, Callable, Union
from uuid import uuid4
from redis import Redis

from . import lua
from .workflow import validate_dag

ACTIVE_SET = 'nuts|workflows|active'
DIRTY_SET = 'nuts|workflows|dirty'
WAIT_DEADLINES = 'nuts|workflows|wait_deadlines'
INSTANCE_KEY_PREFIX = 'nuts|workflows|instance|'
SIGNAL_KEY_PREFIX = 'nuts|workflows|signals|'
ENTITY_KEY_PREFIX = 'nuts|workflows|entity|'
WAITING_PREFIX = 'waiting_for:'
DEFAULT_TTL_SECONDS = 604800
FINISHED_STATUSES = ('completed', 'failed', 'cancelled')


class EntityBusyError(Exception):
    '''An active instance already exists for this entity_key and replace was not requested.'''


def instance_key(instance_id: str) -> str:
    return f'{INSTANCE_KEY_PREFIX}{instance_id}'


def signal_key(instance_id: str) -> str:
    return f'{SIGNAL_KEY_PREFIX}{instance_id}'


def entity_key_name(entity_key: str) -> str:
    return f'{ENTITY_KEY_PREFIX}{entity_key}'


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _normalize_step(raw: dict) -> dict:
    step_id = raw.get('step_id', raw.get('op'))
    if not step_id:
        raise ValueError('plan step is missing step_id/op')
    kind = raw.get('kind', 'job')
    if kind == 'wait' and not raw.get('event'):
        raise ValueError(f'wait step {step_id!r} is missing event')
    if kind == 'job' and not raw.get('op'):
        raise ValueError(f'job step {step_id!r} is missing op')
    return {
        'step_id': step_id,
        'op': raw.get('op'),
        'kind': kind,
        'event': raw.get('event'),
        'requires': raw.get('requires'),
        'priority': raw.get('priority', 'normal'),
        'concurrency_key': raw.get('concurrency_key'),
        'concurrency_limit': raw.get('concurrency_limit', 1),
        'max_attempts': raw.get('max_attempts'),
        'timeout_seconds': raw.get('timeout_seconds'),
        'wait_deadline': None,
        'dispatch_id': None,
        'status': None,
        'result': None,
        'error': None,
        'attempt': 0,
    }


def create_instance(
    redis: Redis,
    workflow_name: str,
    plan: list[dict],
    params: Union[dict, None] = None,
    *,
    entity_key: Union[str, None] = None,
    replace: bool = False,
) -> str:
    '''
        Create a durable workflow instance from a per-instance plan (see
        _normalize_step for the step shape). Validates the requires graph but
        not that `op` names are registered with any Worker -- an unknown op
        fails through the normal retry/DLQ path when a worker reaches it.

        entity_key ties the instance to something outside nuts (a SIM, an
        order) so callers can signal or cancel it without knowing its id.
        Only one active instance per entity_key is allowed: with
        replace=True the existing one is cancelled, otherwise
        EntityBusyError is raised.

        A wait step may set timeout_seconds; if no signal arrives in time,
        the step and the instance fail.
    '''
    steps = [_normalize_step(s) for s in plan]
    is_valid, error = validate_dag([(s['step_id'], s['requires']) for s in steps])
    if not is_valid:
        raise ValueError(error)

    instance_id = str(uuid4())
    now = _now().isoformat()
    doc = {
        'instance_id': instance_id,
        'workflow_name': workflow_name,
        'entity_key': entity_key,
        'status': 'active',
        'params': params or {},
        '_v': 1,
        'created_at': now,
        'updated_at': now,
        'error': None,
        'steps': steps,
    }
    redis.set(instance_key(instance_id), json.dumps(doc))

    if entity_key is not None:
        index = entity_key_name(entity_key)
        if replace:
            previous = redis.set(index, instance_id, get=True)
            if previous is not None and previous.decode() != instance_id:
                cancel_instance(redis, previous.decode(), reason=f'replaced by {instance_id}')
        elif not redis.set(index, instance_id, nx=True):
            redis.delete(instance_key(instance_id))
            raise EntityBusyError(f'Entity {entity_key!r} already has an active instance')

    pipe = redis.pipeline()
    pipe.sadd(ACTIVE_SET, instance_id)
    pipe.sadd(DIRTY_SET, instance_id)
    pipe.execute()
    return instance_id


def get_instance(redis: Redis, instance_id: str) -> Union[dict, None]:
    raw = redis.get(instance_key(instance_id))
    return json.loads(raw) if raw is not None else None


def get_instance_id_for_entity(redis: Redis, entity_key: str) -> Union[str, None]:
    raw = redis.get(entity_key_name(entity_key))
    return raw.decode() if raw is not None else None


def update_instance(redis: Redis, instance_id: str, mutate_fn: Callable[[dict], Any], max_retries: int = 5) -> dict:
    '''
        Compare-and-swap retry loop: re-read the instance, apply mutate_fn
        to a copy, and commit only if nobody else committed in between.
        mutate_fn may run more than once, so it must be safe to re-run
        against a freshly read doc. If mutate_fn changes nothing, nothing is
        written.
    '''
    for _ in range(max_retries):
        doc = get_instance(redis, instance_id)
        if doc is None:
            raise KeyError(f'No such workflow instance: {instance_id}')

        new_doc = copy.deepcopy(doc)
        mutate_fn(new_doc)
        if new_doc == doc:
            return doc
        new_doc['_v'] = doc['_v'] + 1
        new_doc['updated_at'] = _now().isoformat()

        if lua.cas_set(redis, instance_key(instance_id), doc['_v'], new_doc):
            return new_doc

    raise RuntimeError(f'CAS retry exhausted updating workflow instance {instance_id}')


def mark_dirty(redis: Redis, instance_id: str) -> None:
    redis.sadd(DIRTY_SET, instance_id)


def _finish(redis: Redis, instance_id: str, status: str, ttl_seconds: int, error: Union[str, None] = None) -> Union[dict, None]:
    def mutate(d: dict):
        if d['status'] not in FINISHED_STATUSES:
            d['status'] = status
            d['error'] = error

    try:
        doc = update_instance(redis, instance_id, mutate)
    except KeyError:
        redis.srem(ACTIVE_SET, instance_id)
        return None

    pipe = redis.pipeline()
    pipe.srem(ACTIVE_SET, instance_id)
    pipe.zrem(WAIT_DEADLINES, instance_id)
    pipe.expire(instance_key(instance_id), ttl_seconds)
    pipe.expire(signal_key(instance_id), ttl_seconds)
    pipe.execute()
    if doc.get('entity_key') is not None:
        lua.del_if_equals(redis, entity_key_name(doc['entity_key']), instance_id)
    return doc


def complete_instance(redis: Redis, instance_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    return _finish(redis, instance_id, 'completed', ttl_seconds)


def fail_instance(redis: Redis, instance_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, error: Union[str, None] = None) -> dict:
    return _finish(redis, instance_id, 'failed', ttl_seconds, error)


def cancel_instance(redis: Redis, instance_id: str, reason: Union[str, None] = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Union[dict, None]:
    '''
        Stop an instance. Steps not yet dispatched never run; a step already
        queued is skipped when a worker picks it up; a step already running
        finishes but nothing after it is dispatched.
    '''
    return _finish(redis, instance_id, 'cancelled', ttl_seconds, reason or 'cancelled')


def cancel_entity(redis: Redis, entity_key: str, reason: Union[str, None] = None) -> Union[str, None]:
    instance_id = get_instance_id_for_entity(redis, entity_key)
    if instance_id is not None:
        cancel_instance(redis, instance_id, reason)
    return instance_id


def _is_ready(step: dict, completed_ids: set) -> bool:
    return step['status'] is None and all(r in completed_ids for r in (step.get('requires') or []))


def ready_steps(doc: dict) -> list[dict]:
    '''Steps (of any kind) that haven't started and whose requires are all completed.'''
    completed_ids = {s['step_id'] for s in doc['steps'] if s['status'] == 'completed'}
    return [s for s in doc['steps'] if _is_ready(s, completed_ids)]


def needs_advance(doc: dict, now_ts: float) -> bool:
    '''Whether the leader has anything to do for this instance, ignoring pending signals.'''
    if doc['status'] != 'active' or ready_steps(doc):
        return True
    statuses = [s['status'] for s in doc['steps']]
    if all(s == 'completed' for s in statuses) or 'failed' in statuses:
        return True
    return any(
        s.get('wait_deadline') is not None and now_ts >= s['wait_deadline']
        for s in doc['steps']
        if isinstance(s['status'], str) and s['status'].startswith(WAITING_PREFIX)
    )


def signal_instance(redis: Redis, instance_id: str, event: str, payload: Any = None) -> bool:
    '''
        Record that `event` happened for this instance. Never touches the
        instance document directly -- only resolve_wait_steps consumes it,
        so a signal that arrives before the instance reaches its wait step
        is still picked up. Returns False if the instance doesn't exist.
    '''
    if not redis.exists(instance_key(instance_id)):
        return False
    pipe = redis.pipeline()
    pipe.hset(signal_key(instance_id), event, json.dumps(payload))
    pipe.expire(signal_key(instance_id), DEFAULT_TTL_SECONDS)
    pipe.sadd(DIRTY_SET, instance_id)
    pipe.execute()
    return True


def signal_entity(redis: Redis, entity_key: str, event: str, payload: Any = None) -> Union[str, None]:
    '''Signal the active instance for entity_key. Returns its id, or None if there isn't one.'''
    instance_id = get_instance_id_for_entity(redis, entity_key)
    if instance_id is None:
        return None
    return instance_id if signal_instance(redis, instance_id, event, payload) else None


def resolve_wait_steps(redis: Redis, instance_id: str) -> Union[dict, None]:
    '''
        For every wait step that's ready or already waiting: complete it if
        its signal has arrived, fail it if its timeout has passed, otherwise
        mark it waiting. Signals are checked before marking a step waiting,
        and only deleted after the commit succeeds, so a lost CAS race never
        drops one.
    '''
    consumed_events: list[str] = []
    now = _now().timestamp()

    def mutate(doc: dict):
        consumed_events.clear()
        completed_ids = {s['step_id'] for s in doc['steps'] if s['status'] == 'completed'}
        for step in doc['steps']:
            if step.get('kind') != 'wait':
                continue
            is_ready = _is_ready(step, completed_ids)
            is_waiting = isinstance(step['status'], str) and step['status'].startswith(WAITING_PREFIX)
            if not (is_ready or is_waiting):
                continue

            raw = redis.hget(signal_key(instance_id), step['event'])
            if raw is not None:
                step['status'] = 'completed'
                step['result'] = json.loads(raw)
                consumed_events.append(step['event'])
            elif is_waiting and step.get('wait_deadline') is not None and now >= step['wait_deadline']:
                step['status'] = 'failed'
                step['error'] = f"timed out waiting for {step['event']}"
                doc['error'] = f"step {step['step_id']} timed out waiting for {step['event']}"
            elif is_ready:
                step['status'] = f'{WAITING_PREFIX}{step["event"]}'
                if step.get('timeout_seconds') is not None:
                    step['wait_deadline'] = now + step['timeout_seconds']

    if get_instance(redis, instance_id) is None:
        return None

    doc = update_instance(redis, instance_id, mutate)
    if consumed_events:
        redis.hdel(signal_key(instance_id), *consumed_events)
    deadlines = [
        s['wait_deadline'] for s in doc['steps']
        if s.get('wait_deadline') is not None and isinstance(s['status'], str) and s['status'].startswith(WAITING_PREFIX)
    ]
    if deadlines:
        redis.zadd(WAIT_DEADLINES, {instance_id: min(deadlines)})
    else:
        redis.zrem(WAIT_DEADLINES, instance_id)
    return doc
