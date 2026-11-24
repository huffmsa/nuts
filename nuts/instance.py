"""
Durable, per-entity workflow instances. A plan is assembled per-instance from
a known, composable catalog of step/op types supplied by the caller at
creation time -- "skip" is just "the caller didn't include that op," no
runtime conditional-skip logic is needed, and two instances of the same
workflow_name can carry entirely different steps/ordering for free.

Instance state lives entirely in Redis (never in worker memory), so any
process can compute "what's next" from a persisted document alone -- this is
what makes crash-survivability, concurrent per-entity instances, and
wait-for-signal steps all work.
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
INSTANCE_KEY_PREFIX = 'nuts|workflows|instance|'
SIGNAL_KEY_PREFIX = 'nuts|workflows|signals|'
WAITING_PREFIX = 'waiting_for:'


def instance_key(instance_id: str) -> str:
    return f'{INSTANCE_KEY_PREFIX}{instance_id}'


def signal_key(instance_id: str) -> str:
    return f'{SIGNAL_KEY_PREFIX}{instance_id}'


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
        'status': None,
        'result': None,
        'error': None,
        'attempt': 0,
    }


def create_instance(redis: Redis, workflow_name: str, plan: list[dict], params: Union[dict, None] = None) -> str:
    '''
        Create a durable workflow instance from a per-instance plan (a list
        of step dicts drawn from a known catalog of ops -- see
        _normalize_step for the accepted shape). Structurally validates the
        requires graph (cycles/missing-deps/orphans) but deliberately does
        NOT validate that `op` names are registered with any particular
        Worker -- this must stay callable from any process, same as
        WorkQueue.publish() is decoupled from any specific Worker today. An
        unknown op simply fails the first time a worker tries to resolve it,
        flowing through the normal retry/backoff/DLQ path like any failure.
    '''
    steps = [_normalize_step(s) for s in plan]
    is_valid, error = validate_dag([(s['step_id'], s['requires']) for s in steps])
    if not is_valid:
        raise ValueError(error)

    instance_id = str(uuid4())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    doc = {
        'instance_id': instance_id,
        'workflow_name': workflow_name,
        'status': 'active',
        'params': params or {},
        '_v': 1,
        'created_at': now,
        'updated_at': now,
        'error': None,
        'steps': steps,
    }
    redis.set(instance_key(instance_id), json.dumps(doc))
    redis.sadd(ACTIVE_SET, instance_id)
    return instance_id


def get_instance(redis: Redis, instance_id: str) -> Union[dict, None]:
    raw = redis.get(instance_key(instance_id))
    return json.loads(raw) if raw is not None else None


def update_instance(redis: Redis, instance_id: str, mutate_fn: Callable[[dict], Any], max_retries: int = 5) -> dict:
    '''
        Compare-and-swap retry loop: re-read the instance, apply mutate_fn
        in place, and try to commit via a single atomic Lua script. Retries
        on version conflict (another process committed first) up to
        max_retries. mutate_fn may be called more than once -- it must be
        safe to re-run against a freshly re-read doc each attempt.
    '''
    for _ in range(max_retries):
        doc = get_instance(redis, instance_id)
        if doc is None:
            raise KeyError(f'No such workflow instance: {instance_id}')

        new_doc = copy.deepcopy(doc)
        mutate_fn(new_doc)
        new_doc['_v'] = doc['_v'] + 1
        new_doc['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if lua.cas_set(redis, instance_key(instance_id), doc['_v'], new_doc):
            return new_doc

    raise RuntimeError(f'CAS retry exhausted updating workflow instance {instance_id}')


def complete_instance(redis: Redis, instance_id: str, ttl_seconds: int) -> dict:
    doc = update_instance(redis, instance_id, lambda d: d.update({'status': 'completed'}))
    redis.srem(ACTIVE_SET, instance_id)
    redis.expire(instance_key(instance_id), ttl_seconds)
    redis.expire(signal_key(instance_id), ttl_seconds)
    return doc


def fail_instance(redis: Redis, instance_id: str, ttl_seconds: int, error: Union[str, None] = None) -> dict:
    doc = update_instance(redis, instance_id, lambda d: d.update({'status': 'failed', 'error': error}))
    redis.srem(ACTIVE_SET, instance_id)
    redis.expire(instance_key(instance_id), ttl_seconds)
    redis.expire(signal_key(instance_id), ttl_seconds)
    return doc


def _is_ready(step: dict, completed_ids: set) -> bool:
    return step['status'] is None and all(r in completed_ids for r in (step.get('requires') or []))


def ready_steps(doc: dict) -> list[dict]:
    '''Steps (of any kind) that haven't started and whose requires are all completed.'''
    completed_ids = {s['step_id'] for s in doc['steps'] if s['status'] == 'completed'}
    return [s for s in doc['steps'] if _is_ready(s, completed_ids)]


def signal_instance(redis: Redis, instance_id: str, event: str, payload: Any = None) -> None:
    '''
        Record that `event` happened for this instance. Deliberately never
        touches the instance document directly -- only resolve_wait_steps
        (run by the leader's sweep) ever consumes this, which is what avoids
        any CAS-race reasoning in the signal call itself: a signal recorded
        before a step ever reaches 'waiting_for:<event>' is picked up on the
        very next sweep tick regardless of arrival order.
    '''
    redis.hset(signal_key(instance_id), event, json.dumps(payload))


def resolve_wait_steps(redis: Redis, instance_id: str) -> Union[dict, None]:
    '''
        For every wait-kind step that's ready or already waiting: check the
        signal hash *before* marking it waiting (closes the race where a
        signal arrives before the step gets there) and complete it if a
        signal is present. The signal is only deleted from the hash *after*
        the CAS commit succeeds, so a lost CAS race never drops a signal
        that was read but not yet durably consumed.
    '''
    consumed_events: list[str] = []

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
            elif is_ready:
                step['status'] = f'{WAITING_PREFIX}{step["event"]}'

    if get_instance(redis, instance_id) is None:
        return None

    doc = update_instance(redis, instance_id, mutate)
    if consumed_events:
        redis.hdel(signal_key(instance_id), *consumed_events)
    return doc
