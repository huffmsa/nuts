"""
Workflow management for NUTS job scheduler.

This module provides DAG-based workflow functionality allowing multiple jobs
to be orchestrated with dependencies, parallel execution, and error handling.

Classes:
    WorkflowJob: Represents a job within a workflow with status and dependencies
    NutsWorkflow: Manages workflow execution, validation, and state
"""
from typing import Union
from .job import NutsJob


class WorkflowJob(NutsJob):
    """
    Represents a job within a workflow with dependency tracking.

    Extends NutsJob with workflow-specific attributes for managing
    execution status, dependencies, and errors.

    Attributes:
        status: Current execution status ('pending', 'completed', 'failed', or None)
        requires: List of job names that must complete before this job can run
        error: Error message if job failed, None otherwise
    """
    status: Union[str, None]
    requires: Union[list[str], None]
    error: Union[str, None]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.status = None
        self.requires = kwargs.get('requires', None)
        self.error = None


class NutsWorkflow:
    """
    Manages DAG-based workflow execution with dependency resolution.

    NutsWorkflow orchestrates multiple jobs with dependencies, ensuring
    jobs execute in the correct order, handling failures, and managing
    workflow state across executions.

    Attributes:
        name: Unique workflow identifier
        schedule: Cron expression for workflow scheduling
        status: Workflow status ('active', 'completed', 'failed', or None)
        error: Error message if workflow failed, None otherwise
        jobs: List of WorkflowJob instances in this workflow

    Example:
        >>> workflow = NutsWorkflow(
        ...     name='data-pipeline',
        ...     schedule='0 0 2 ? * * *',
        ...     jobs=[
        ...         {'name': 'ExtractData', 'requires': None},
        ...         {'name': 'TransformData', 'requires': ['ExtractData']}
        ...     ]
        ... )
        >>> workflow.run()  # Returns 'ExtractData'
        >>> workflow.update('ExtractData', 'completed')
        >>> workflow.run()  # Returns 'TransformData'
    """
    name: str
    schedule: str
    status: Union[str, None]
    error: Union[str, None]
    jobs: list[WorkflowJob]

    def __init__(self, **kwargs):
        """
        Initialize the NutsWorkflow class.
        """
        self.name = kwargs.get('name', None)
        self.schedule = kwargs.get('schedule', None)
        self.status = None
        self.error = None
        self.jobs = []
        for job in kwargs.get('jobs', []):
            j = WorkflowJob(**job)
            self.jobs.append(j)

    def check_requirements(self, job: WorkflowJob) -> bool:
        """
        Check if all dependencies for a job have completed.

        Args:
            job: WorkflowJob to check requirements for

        Returns:
            True if all required jobs have completed, False otherwise
        """
        if job.requires is None:
            return True

        for req in job.requires:
            for j in self.jobs:
                if j.name == req and j.status != 'completed':
                    return False

        return True

    def run(self) -> Union[str, bool]:
        """
        Determine the next job to execute in the workflow.

        Finds the first job that has not been started and whose
        dependencies have all completed.

        Returns:
            str: Name of the next job to execute
            False: If no jobs are eligible to run (either all scheduled or waiting on dependencies)
        """
        next_job = None
        for job in self.jobs:
            if job.status is None and self.check_requirements(job):
                next_job = job
                break
        if next_job is None:
            return False
        return next_job.name

    def update(self, job_name: str, status: str, error: str = None):
        """
        Update the execution status of a job in the workflow.

        If status is 'failed', marks the entire workflow as failed and
        stops further execution.

        Args:
            job_name: Name of the job to update
            status: New status ('pending', 'completed', or 'failed')
            error: Optional error message if status is 'failed'
        """
        for job in self.jobs:
            if job.name == job_name:
                job.status = status
                if job.status == 'completed':
                    job.success = True
                elif job.status == 'failed':
                    job.success = False
                    job.error = error
                    # Mark entire workflow as failed
                    self.status = 'failed'
                    self.error = f'Job {job_name} failed: {error}'

    def completed(self) -> bool:
        """
        Check if all jobs in the workflow have completed successfully.

        Returns:
            True if all jobs have status 'completed', False otherwise
        """
        completed = []
        for job in self.jobs:
            if job.status == 'completed':
                completed.append(job.name)

        if len(completed) == len(self.jobs):
            return True

        return False

    def has_failures(self) -> bool:
        """Check if any job in the workflow has failed."""
        for job in self.jobs:
            if job.status == 'failed':
                return True
        return False

    def reset(self):
        """
        Reset the workflow for next execution.
        """
        for job in self.jobs:
            job.status = None
            job.success = None
            job.error = None

        self.status = None
        self.error = None

    def validate(self) -> tuple[bool, str]:
        """
        Validate workflow configuration for common errors.
        Returns (is_valid, error_message)
        """
        return validate_dag([(j.name, j.requires) for j in self.jobs])

    def to_plan(self) -> list[dict]:
        """
        Convert this template's jobs into a plan usable by
        nuts.instance.create_instance() -- this is what makes YAML
        workflow_directory loading "sugar" over dynamic instance creation:
        the leader spawns a fresh instance from this plan on each due
        cron occurrence instead of mutating a shared singleton.
        """
        return [
            {'step_id': j.name, 'op': j.name, 'kind': 'job', 'requires': j.requires}
            for j in self.jobs
        ]


def validate_dag(nodes: list[tuple[str, Union[list[str], None]]]) -> tuple[bool, str]:
    """
    Structural validation for any (name, requires) DAG -- cycle detection,
    missing-dependency checks, and orphan-node detection. Pure data
    validation with no dependency on any live workflow/instance state, so
    it's shared by NutsWorkflow.validate() (static YAML templates) and
    nuts.instance.create_instance() (per-instance dynamic plans).
    Returns (is_valid, error_message).
    """
    by_name = {name: requires for name, requires in nodes}

    visited = set()

    def has_cycle(name, path):
        if name in path:
            return True
        if name in visited:
            return False

        visited.add(name)
        path.add(name)

        requires = by_name.get(name)
        if not requires:
            path.remove(name)
            return False

        for req in requires:
            if has_cycle(req, path):
                return True

        path.remove(name)
        return False

    for name, _ in nodes:
        if has_cycle(name, set()):
            return False, f"Circular dependency detected involving job: {name}"

    # Check that all required jobs exist
    names = set(by_name.keys())
    for name, requires in nodes:
        if requires:
            for req in requires:
                if req not in names:
                    return False, f"Job {name} requires non-existent job: {req}"

    # Check for orphaned jobs (no path from root to job)
    root_jobs = [name for name, requires in nodes if not requires]
    if not root_jobs:
        return False, "Workflow has no root jobs (all jobs have requirements)"

    return True, ""
