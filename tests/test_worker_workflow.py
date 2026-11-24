import time
import tempfile
import os
import shutil
import yaml
from ..nuts.worker import Worker
from ..nuts.job import NutsJob
from ..nuts import instance as inst
from ..nuts import dispatch


def _worker(redis, jobs, **kwargs):
    return Worker(redis=redis, jobs=jobs, **kwargs)


def _run_until_idle(worker, redis, ticks=10, delay=0.02):
    for _ in range(ticks):
        worker.run()
        time.sleep(delay)


def test_instance_creation_with_chosen_subset_and_order(redis):
    """
    A plan is assembled per-instance from a known, composable catalog of
    ops. Two instances of the same workflow_name can carry entirely
    different steps -- "skip" is just "not included," no conditional-skip
    logic needed.
    """
    class ExtractMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Extract'

            def run(self, **kwargs):
                self.result = {'rows': 100}
                self.success = True

    class TransformMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Transform'

            def run(self, **kwargs):
                extract = kwargs.get('extract', {})
                self.result = {'rows': extract.get('rows', 0) * 2}
                self.success = True

    worker = _worker(redis, [ExtractMod, TransformMod])

    full_plan = [
        {'step_id': 'extract', 'op': 'Extract', 'requires': None},
        {'step_id': 'transform', 'op': 'Transform', 'requires': ['extract']},
    ]
    short_plan = [
        {'step_id': 'extract', 'op': 'Extract', 'requires': None},
    ]

    id_a = inst.create_instance(redis, 'pipeline', full_plan, params={'vendor': 'A'})
    id_b = inst.create_instance(redis, 'pipeline', short_plan, params={'vendor': 'B'})

    _run_until_idle(worker, redis)

    doc_a = inst.get_instance(redis, id_a)
    doc_b = inst.get_instance(redis, id_b)

    assert doc_a['status'] == 'completed'
    assert next(s for s in doc_a['steps'] if s['step_id'] == 'transform')['result'] == {'rows': 200}
    assert doc_b['status'] == 'completed'
    assert [s['step_id'] for s in doc_b['steps']] == ['extract']  # transform never existed for B


def test_sweep_advances_ready_steps_in_dependency_order(redis):
    order = []

    class AMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'A'

            def run(self, **kwargs):
                order.append('A')
                self.success = True

    class BMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'B'

            def run(self, **kwargs):
                order.append('B')
                self.success = True

    worker = _worker(redis, [AMod, BMod])
    plan = [
        {'step_id': 'a', 'op': 'A', 'requires': None},
        {'step_id': 'b', 'op': 'B', 'requires': ['a']},
    ]
    instance_id = inst.create_instance(redis, 'wf', plan)

    _run_until_idle(worker, redis)

    assert order == ['A', 'B']
    assert inst.get_instance(redis, instance_id)['status'] == 'completed'


def test_signal_before_wait_step_reached(redis):
    class ExtractMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Extract'

            def run(self, **kwargs):
                self.success = True

    class FinalizeMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Finalize'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [ExtractMod, FinalizeMod])
    plan = [
        {'step_id': 'extract', 'op': 'Extract', 'requires': None},
        {'step_id': 'ack', 'kind': 'wait', 'event': 'vendor_ack', 'requires': ['extract']},
        {'step_id': 'finalize', 'op': 'Finalize', 'requires': ['ack']},
    ]
    instance_id = inst.create_instance(redis, 'wf', plan)

    # Signal arrives immediately, before the leader has ever ticked.
    inst.signal_instance(redis, instance_id, 'vendor_ack', {'ok': True})

    _run_until_idle(worker, redis)

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'completed'
    assert next(s for s in doc['steps'] if s['step_id'] == 'ack')['result'] == {'ok': True}


def test_signal_after_wait_step_reached(redis):
    class ExtractMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Extract'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [ExtractMod])
    plan = [
        {'step_id': 'extract', 'op': 'Extract', 'requires': None},
        {'step_id': 'ack', 'kind': 'wait', 'event': 'vendor_ack', 'requires': ['extract']},
    ]
    instance_id = inst.create_instance(redis, 'wf', plan)

    worker.run()
    worker.sweep_active_workflow_instances()
    doc = inst.get_instance(redis, instance_id)
    assert next(s for s in doc['steps'] if s['step_id'] == 'ack')['status'] == 'waiting_for:vendor_ack'

    inst.signal_instance(redis, instance_id, 'vendor_ack', {'ok': True})
    worker.sweep_active_workflow_instances()

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'completed'


def test_concurrency_gated_step_is_deferred_until_slot_frees(redis):
    class SlowMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'SlowOp'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [SlowMod])
    plan = lambda: [{'step_id': 'op', 'op': 'SlowOp', 'requires': None,
                      'concurrency_key': 'shared', 'concurrency_limit': 1}]

    id1 = inst.create_instance(redis, 'wf', plan())
    id2 = inst.create_instance(redis, 'wf', plan())

    worker.sweep_active_workflow_instances()

    statuses = {
        id1: next(s for s in inst.get_instance(redis, id1)['steps'])['status'],
        id2: next(s for s in inst.get_instance(redis, id2)['steps'])['status'],
    }
    # Exactly one of the two got dispatched this tick -- the other stays
    # untouched (status None) until a slot frees up.
    dispatched = [v for v in statuses.values() if v == 'dispatched']
    deferred = [v for v in statuses.values() if v is None]
    assert len(dispatched) == 1
    assert len(deferred) == 1


def test_dlq_after_max_retries_fails_the_step_not_the_whole_run(redis):
    class AlwaysFailMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'AlwaysFail'

            def run(self, **kwargs):
                raise RuntimeError('boom')

    from ..nuts.worker import RetryPolicy
    worker = _worker(redis, [AlwaysFailMod], retry_policy=RetryPolicy(base=0.01, cap=0.02, max_attempts=1))
    plan = [{'step_id': 'fail', 'op': 'AlwaysFail', 'requires': None, 'max_attempts': 1}]
    instance_id = inst.create_instance(redis, 'wf', plan)

    _run_until_idle(worker, redis, ticks=6)

    doc = inst.get_instance(redis, instance_id)
    assert doc['status'] == 'failed'
    assert next(s for s in doc['steps'])['status'] == 'failed'
    assert redis.xlen(dispatch.DLQ_STREAM) == 1


def test_yaml_workflow_directory_is_sugar_over_create_instance(redis):
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, 'pipeline.yaml'), 'w') as f:
            yaml.dump({
                'workflow': {
                    'name': 'test-pipeline',
                    'schedule': '0/2 * * ? * * *',
                    'jobs': [
                        {'name': 'TestA', 'requires': None},
                        {'name': 'TestB', 'requires': ['TestA']},
                    ],
                }
            }, f)

        class TestAMod:
            class Job(NutsJob):
                def __init__(self):
                    super().__init__()
                    self.name = 'TestA'

                def run(self, **kwargs):
                    self.success = True

        class TestBMod:
            class Job(NutsJob):
                def __init__(self):
                    super().__init__()
                    self.name = 'TestB'

                def run(self, **kwargs):
                    self.success = True

        worker = _worker(redis, [TestAMod, TestBMod], workflow_directory=tmpdir)
        assert [t.name for t in worker.workflow_templates] == ['test-pipeline']

        # Force the template due right now instead of waiting on the clock.
        redis.zrem(worker.scheduled_workflow_queue, 'test-pipeline')
        redis.zadd(worker.scheduled_workflow_queue, {'test-pipeline': time.time() - 1})
        worker.drain_due_workflow_cron()

        active = redis.smembers(inst.ACTIVE_SET)
        assert len(active) == 1

        _run_until_idle(worker, redis)

        instance_id = list(active)[0].decode()
        doc = inst.get_instance(redis, instance_id)
        assert doc['status'] == 'completed'
        assert doc['workflow_name'] == 'test-pipeline'
    finally:
        shutil.rmtree(tmpdir)


def test_invalid_workflow_template_is_skipped(redis):
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, 'invalid.yaml'), 'w') as f:
            yaml.dump({
                'workflow': {
                    'name': 'invalid-workflow',
                    'schedule': '0/10 * * ? * * *',
                    'jobs': [
                        {'name': 'job1', 'requires': ['job2']},
                        {'name': 'job2', 'requires': ['job1']},
                    ],
                }
            }, f)

        class NoopMod:
            class Job(NutsJob):
                def __init__(self):
                    super().__init__()
                    self.name = 'job1'

                def run(self, **kwargs):
                    self.success = True

        worker = _worker(redis, [NoopMod], workflow_directory=tmpdir)
        assert not any(t.name == 'invalid-workflow' for t in worker.workflow_templates)
    finally:
        shutil.rmtree(tmpdir)


def test_worker_without_workflow_directory_still_works(redis):
    class NoopMod:
        class Job(NutsJob):
            def __init__(self):
                super().__init__()
                self.name = 'Noop'

            def run(self, **kwargs):
                self.success = True

    worker = _worker(redis, [NoopMod])
    assert worker.workflow_templates == []
    assert worker.is_leader
    worker.run()  # must not crash
