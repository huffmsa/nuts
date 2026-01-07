import pytest
from ..nuts.workflow import NutsWorkflow, WorkflowJob


def test_workflow_initialization():
    """Test basic workflow creation."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )
    assert wf.name == 'test-workflow'
    assert len(wf.jobs) == 2
    assert wf.status is None


def test_workflow_run_order():
    """Test workflow executes jobs in correct order."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )

    # First call should return job1
    next_job = wf.run()
    assert next_job == 'job1'

    # Can't run job2 until job1 completes
    next_job = wf.run()
    assert next_job == 'job1'  # Still waiting

    # Mark job1 complete
    wf.update('job1', 'completed')

    # Now job2 should be ready
    next_job = wf.run()
    assert next_job == 'job2'


def test_workflow_completion():
    """Test workflow completion detection."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None}
        ]
    )

    assert not wf.completed()

    wf.update('job1', 'completed')
    assert wf.completed()


def test_workflow_failure_handling():
    """Test workflow handles job failures."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )

    # Mark job1 as failed
    wf.update('job1', 'failed', 'Test error')

    # Workflow should be marked failed
    assert wf.status == 'failed'
    assert wf.error is not None
    assert 'job1' in wf.error

    # Should have failures
    assert wf.has_failures()

    # Job1 should have the error
    assert wf.jobs[0].error == 'Test error'
    assert wf.jobs[0].success is False


def test_workflow_reset():
    """Test workflow reset clears state."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None}
        ]
    )

    wf.update('job1', 'completed')
    assert wf.completed()

    wf.reset()
    assert not wf.completed()
    assert wf.jobs[0].status is None
    assert wf.jobs[0].success is None
    assert wf.status is None


def test_workflow_reset_clears_errors():
    """Test workflow reset clears error state."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None}
        ]
    )

    wf.update('job1', 'failed', 'Test error')
    assert wf.has_failures()
    assert wf.error is not None

    wf.reset()
    assert not wf.has_failures()
    assert wf.error is None
    assert wf.jobs[0].error is None


def test_workflow_parallel_jobs():
    """Test workflow with parallel executable jobs."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': None},  # Can run in parallel
            {'name': 'job3', 'requires': ['job1', 'job2']}
        ]
    )

    # Both job1 and job2 can run (returns first eligible)
    next_job = wf.run()
    assert next_job in ['job1', 'job2']

    wf.update('job1', 'pending')
    next_job = wf.run()
    assert next_job == 'job2'  # Next available

    wf.update('job2', 'pending')
    next_job = wf.run()
    assert next_job is False  # All available jobs running

    # Complete prerequisites
    wf.update('job1', 'completed')
    wf.update('job2', 'completed')

    # Now job3 is ready
    next_job = wf.run()
    assert next_job == 'job3'


def test_workflow_validation_circular_dependency():
    """Test workflow validation catches circular dependencies."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': ['job2']},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )

    is_valid, error = wf.validate()
    assert not is_valid
    assert 'circular' in error.lower()


def test_workflow_validation_missing_dependency():
    """Test workflow validation catches missing dependencies."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': ['nonexistent']}
        ]
    )

    is_valid, error = wf.validate()
    assert not is_valid
    assert 'nonexistent' in error


def test_workflow_validation_no_root_jobs():
    """Test workflow validation catches workflows with no root jobs."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': ['job2']},
            {'name': 'job2', 'requires': ['job1']}  # Circular = no root
        ]
    )

    is_valid, error = wf.validate()
    assert not is_valid


def test_workflow_validation_valid_workflow():
    """Test workflow validation passes for valid workflows."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )

    is_valid, error = wf.validate()
    assert is_valid
    assert error == ""


def test_workflow_check_requirements():
    """Test workflow requirement checking."""
    wf = NutsWorkflow(
        name='test-workflow',
        schedule='0 0 * * * ? *',
        jobs=[
            {'name': 'job1', 'requires': None},
            {'name': 'job2', 'requires': ['job1']}
        ]
    )

    # Job1 has no requirements, should pass
    assert wf.check_requirements(wf.jobs[0])

    # Job2 requires job1 which hasn't completed yet
    assert not wf.check_requirements(wf.jobs[1])

    # Complete job1
    wf.jobs[0].status = 'completed'

    # Now job2's requirements are met
    assert wf.check_requirements(wf.jobs[1])
