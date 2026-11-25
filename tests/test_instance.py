import pytest
from nuts import instance as inst


def _plan(*step_ids_with_requires):
    return [
        {'step_id': sid, 'op': sid, 'requires': requires}
        for sid, requires in step_ids_with_requires
    ]


def test_create_instance_persists_and_tracks_active(redis):
    plan = _plan(('extract', None), ('transform', ['extract']))

    instance_id = inst.create_instance(redis, 'pipeline', plan, params={'vendor': 'A'})

    doc = inst.get_instance(redis, instance_id)
    assert doc['workflow_name'] == 'pipeline'
    assert doc['status'] == 'active'
    assert doc['params'] == {'vendor': 'A'}
    assert [s['step_id'] for s in doc['steps']] == ['extract', 'transform']
    assert instance_id.encode() in redis.smembers(inst.ACTIVE_SET)


def test_two_instances_of_same_workflow_can_have_different_plans(redis):
    """Skip = simply not included in this instance's plan -- no conditional needed."""
    full_plan = _plan(('op1', None), ('op2', ['op1']), ('op3', ['op2']))
    short_plan = _plan(('op1', None), ('op2', ['op1']))

    id_a = inst.create_instance(redis, 'pipeline', full_plan)
    id_b = inst.create_instance(redis, 'pipeline', short_plan)

    doc_a = inst.get_instance(redis, id_a)
    doc_b = inst.get_instance(redis, id_b)

    assert [s['step_id'] for s in doc_a['steps']] == ['op1', 'op2', 'op3']
    assert [s['step_id'] for s in doc_b['steps']] == ['op1', 'op2']


def test_create_instance_rejects_invalid_dag(redis):
    with pytest.raises(ValueError, match='[Cc]ircular'):
        inst.create_instance(redis, 'wf', _plan(('a', ['b']), ('b', ['a'])))


def test_create_instance_rejects_job_step_missing_op(redis):
    with pytest.raises(ValueError, match='op'):
        inst.create_instance(redis, 'wf', [{'step_id': 'a'}])


def test_create_instance_rejects_wait_step_missing_event(redis):
    with pytest.raises(ValueError, match='event'):
        inst.create_instance(redis, 'wf', [{'step_id': 'a', 'kind': 'wait'}])


def test_ready_steps_respects_requires(redis):
    plan = _plan(('extract', None), ('transform', ['extract']))
    instance_id = inst.create_instance(redis, 'pipeline', plan)

    doc = inst.get_instance(redis, instance_id)
    assert [s['step_id'] for s in inst.ready_steps(doc)] == ['extract']

    inst.update_instance(redis, instance_id, lambda d: next(
        s for s in d['steps'] if s['step_id'] == 'extract'
    ).update({'status': 'completed'}))

    doc = inst.get_instance(redis, instance_id)
    assert [s['step_id'] for s in inst.ready_steps(doc)] == ['transform']


def test_update_instance_cas_retry_converges(redis):
    instance_id = inst.create_instance(redis, 'wf', _plan(('a', None)))

    # Two independent mutations should both land -- update_instance always
    # re-reads the latest doc, so sequential calls never clobber each other.
    inst.update_instance(redis, instance_id, lambda d: d.update({'params': {'x': 1}}))
    inst.update_instance(redis, instance_id, lambda d: d['params'].update({'y': 2}))

    doc = inst.get_instance(redis, instance_id)
    assert doc['params'] == {'x': 1, 'y': 2}
    assert doc['_v'] == 3


def test_complete_instance_removes_from_active_and_sets_ttl(redis):
    instance_id = inst.create_instance(redis, 'wf', _plan(('a', None)))

    inst.complete_instance(redis, instance_id, ttl_seconds=100)

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'completed'
    assert instance_id.encode() not in redis.smembers(inst.ACTIVE_SET)
    assert redis.ttl(inst.instance_key(instance_id)) > 0


def test_fail_instance_records_error(redis):
    instance_id = inst.create_instance(redis, 'wf', _plan(('a', None)))

    inst.fail_instance(redis, instance_id, ttl_seconds=100, error='boom')

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'failed'
    assert doc['error'] == 'boom'


def test_signal_before_wait_step_reached_is_not_lost(redis):
    """The race: a signal arrives before the step ever reaches waiting_for."""
    plan = [
        {'step_id': 'extract', 'op': 'extract', 'requires': None},
        {'step_id': 'ack', 'kind': 'wait', 'event': 'vendor_ack', 'requires': ['extract']},
    ]
    instance_id = inst.create_instance(redis, 'wf', plan)
    inst.update_instance(redis, instance_id, lambda d: next(
        s for s in d['steps'] if s['step_id'] == 'extract'
    ).update({'status': 'completed'}))

    # Signal arrives before any sweep has ever looked at this instance.
    inst.signal_instance(redis, instance_id, 'vendor_ack', {'early': True})

    doc = inst.resolve_wait_steps(redis, instance_id)
    ack = next(s for s in doc['steps'] if s['step_id'] == 'ack')
    assert ack['status'] == 'completed'
    assert ack['result'] == {'early': True}


def test_signal_after_wait_step_reached(redis):
    plan = [
        {'step_id': 'extract', 'op': 'extract', 'requires': None},
        {'step_id': 'ack', 'kind': 'wait', 'event': 'vendor_ack', 'requires': ['extract']},
    ]
    instance_id = inst.create_instance(redis, 'wf', plan)
    inst.update_instance(redis, instance_id, lambda d: next(
        s for s in d['steps'] if s['step_id'] == 'extract'
    ).update({'status': 'completed'}))

    doc = inst.resolve_wait_steps(redis, instance_id)
    ack = next(s for s in doc['steps'] if s['step_id'] == 'ack')
    assert ack['status'] == 'waiting_for:vendor_ack'

    inst.signal_instance(redis, instance_id, 'vendor_ack', {'late': True})
    doc = inst.resolve_wait_steps(redis, instance_id)
    ack = next(s for s in doc['steps'] if s['step_id'] == 'ack')
    assert ack['status'] == 'completed'
    assert ack['result'] == {'late': True}
    assert redis.hgetall(inst.signal_key(instance_id)) == {}
