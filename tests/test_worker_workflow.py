import datetime
import json
import tempfile
import os
import shutil
import yaml
from ..nuts.worker import Worker
from ..nuts.workflow import NutsWorkflow
from redis import Redis
from .fixtures.jobs import add_one, scheduled_job

r = Redis()


def create_test_workflow_file():
    """Helper to create temporary workflow file."""
    workflow_content = {
        'workflow': {
            'name': 'test-integration-workflow',
            'schedule': '0/10 * * ? * * *',  # Every 10 seconds
            'jobs': [
                {'name': 'AddOne', 'requires': None},
                {'name': 'ScheduledJob', 'requires': ['AddOne']}
            ]
        }
    }

    tmpdir = tempfile.mkdtemp()
    workflow_file = os.path.join(tmpdir, 'test_workflow.yaml')
    with open(workflow_file, 'w') as f:
        yaml.dump(workflow_content, f)

    return tmpdir, workflow_file


def test_worker_loads_workflows():
    """Test worker properly loads workflows from directory."""
    tmpdir, _ = create_test_workflow_file()

    try:
        r.flushall()
        jobs = [add_one, scheduled_job]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        assert len(worker.workflows) >= 1
        assert any(wf.name == 'test-integration-workflow' for wf in worker.workflows)
    finally:
        shutil.rmtree(tmpdir)


def test_worker_schedules_workflow():
    """Test worker schedules workflows correctly."""
    tmpdir, _ = create_test_workflow_file()

    try:
        r.flushall()
        jobs = [add_one, scheduled_job]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        # Check workflow is scheduled
        scheduled = r.zscan(worker.scheduled_workflow_queue, match='test-integration-workflow')
        assert len(scheduled[1]) > 0
    finally:
        shutil.rmtree(tmpdir)


def test_workflow_job_execution():
    """Test workflow jobs execute in correct order."""
    tmpdir, _ = create_test_workflow_file()

    try:
        r.flushall()
        jobs = [add_one, scheduled_job]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        # Manually trigger workflow
        wf = [w for w in worker.workflows if w.name == 'test-integration-workflow'][0]
        wf.status = 'active'

        worker.run_workflows()

        # Check first job was scheduled
        pending_jobs = r.smembers(worker.pending_queue)
        assert len(pending_jobs) == 1

        # Job name should be workflow-prefixed
        job_data = json.loads(list(pending_jobs)[0])
        assert 'workflow-test-integration-workflow' in job_data[0]
        assert 'AddOne' in job_data[0]
    finally:
        shutil.rmtree(tmpdir)


def test_workflow_failure_stops_execution():
    """Test workflow stops when a job fails."""
    tmpdir, _ = create_test_workflow_file()

    try:
        r.flushall()
        jobs = [add_one, scheduled_job]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        wf = [w for w in worker.workflows if w.name == 'test-integration-workflow'][0]
        wf.status = 'active'

        # Simulate first job failing
        wf.update('AddOne', 'failed', 'Test failure')

        # Verify workflow marked as failed before running
        assert wf.status == 'failed'
        assert wf.has_failures()

        # Run workflows - should detect failure, reset, and reschedule
        worker.run_workflows()

        # After run_workflows, the workflow should be reset and rescheduled
        assert wf.status is None
        assert not wf.has_failures()

        # Second job should NOT be scheduled (because workflow failed before it could run)
        pending_jobs = r.smembers(worker.pending_queue)
        scheduled_job_count = sum(1 for job in pending_jobs if 'ScheduledJob' in str(job))
        assert scheduled_job_count == 0
    finally:
        shutil.rmtree(tmpdir)


def test_worker_without_workflow_directory():
    """Test worker works without workflow_directory (backwards compatibility)."""
    r.flushall()

    jobs = [add_one, scheduled_job]
    # Should not crash
    worker = Worker(redis=r, jobs=jobs)

    assert len(worker.workflows) == 0
    assert worker.is_leader  # Leadership still works

    # Should be able to run normally
    worker.run()


def test_invalid_workflow_skipped():
    """Test that invalid workflows are logged and skipped."""
    # Create workflow with circular dependency
    workflow_content = {
        'workflow': {
            'name': 'invalid-workflow',
            'schedule': '0/10 * * ? * * *',
            'jobs': [
                {'name': 'job1', 'requires': ['job2']},
                {'name': 'job2', 'requires': ['job1']}
            ]
        }
    }

    tmpdir = tempfile.mkdtemp()
    workflow_file = os.path.join(tmpdir, 'invalid_workflow.yaml')
    with open(workflow_file, 'w') as f:
        yaml.dump(workflow_content, f)

    try:
        r.flushall()
        jobs = [add_one]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        # Invalid workflow should not be loaded
        assert not any(wf.name == 'invalid-workflow' for wf in worker.workflows)
    finally:
        shutil.rmtree(tmpdir)


def test_workflow_completion_reschedules():
    """Test that completed workflows are rescheduled."""
    tmpdir, _ = create_test_workflow_file()

    try:
        r.flushall()
        jobs = [add_one, scheduled_job]
        worker = Worker(redis=r, jobs=jobs, workflow_directory=tmpdir)

        wf = [w for w in worker.workflows if w.name == 'test-integration-workflow'][0]
        wf.status = 'active'

        # Mark all jobs as completed
        wf.update('AddOne', 'completed')
        wf.update('ScheduledJob', 'completed')

        assert wf.completed()

        # Run workflows - should reset and reschedule
        worker.run_workflows()

        # Workflow should be reset
        assert wf.status is None
        assert not wf.completed()

        # Should be rescheduled
        scheduled = r.zscan(worker.scheduled_workflow_queue, match='test-integration-workflow')
        assert len(scheduled[1]) > 0
    finally:
        shutil.rmtree(tmpdir)
