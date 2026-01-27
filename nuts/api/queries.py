"""Redis query helpers for the NUTS API."""

import json
from datetime import datetime, timezone
from typing import Optional
from redis import Redis

from .models import (
    PendingJob,
    RunningJob,
    CompletedJob,
    ScheduledJob,
    WorkflowStatus,
    WorkflowJobStatus,
    ScheduledWorkflow,
)

# Redis key constants (matching worker.py)
SCHEDULED_QUEUE = "nuts|jobs|scheduled"
RUNNING_QUEUE = "nuts|jobs|running"
PENDING_QUEUE = "nuts|jobs|pending"
COMPLETED_QUEUE = "nuts|jobs|completed"
SCHEDULED_WORKFLOW_QUEUE = "nuts|workflows|scheduled"
RUNNING_WORKFLOW_QUEUE = "nuts|workflows|running"
CANCEL_QUEUE = "nuts|jobs|cancel"


def get_pending_jobs(redis: Redis) -> list[PendingJob]:
    """Get all jobs in the pending queue."""
    jobs = []
    members = redis.smembers(PENDING_QUEUE)
    for member in members:
        if isinstance(member, bytes):
            member = member.decode()
        data = json.loads(member)
        name = data[0]
        params = data[1] if len(data) > 1 else []
        # Handle workflow job names
        if "|" in name and "workflow" in name:
            # Format: workflow-{name}|{job_name}
            name = name.split("|")[1]
        jobs.append(PendingJob(name=name, params=params))
    return jobs


def get_running_jobs(redis: Redis) -> list[RunningJob]:
    """Get all currently running jobs."""
    jobs = []
    running = redis.hgetall(RUNNING_QUEUE)
    for key, value in running.items():
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(value, bytes):
            value = value.decode()

        # Key format: {worker_id}|{job_name}
        parts = key.split("|")
        worker_id = parts[0]
        job_name = parts[1] if len(parts) > 1 else key

        data = json.loads(value)
        started_at = datetime.fromisoformat(data.get("timestamp", datetime.now(timezone.utc).isoformat()))
        params = data.get("args", [])

        jobs.append(RunningJob(
            name=job_name,
            worker_id=worker_id,
            started_at=started_at,
            params=params
        ))
    return jobs


def get_completed_jobs(redis: Redis) -> list[CompletedJob]:
    """Get all completed jobs."""
    jobs = []
    completed = redis.hgetall(COMPLETED_QUEUE)
    for key, value in completed.items():
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(value, bytes):
            value = value.decode()

        data = json.loads(value)
        workflow_name = None
        job_name = key

        # Check if this is a workflow job
        if "|" in key:
            parts = key.split("|")
            workflow_name = parts[0].replace("workflow-", "")
            job_name = parts[1]

        jobs.append(CompletedJob(
            name=job_name,
            success=data.get("success", False),
            error=data.get("error"),
            workflow_name=workflow_name
        ))
    return jobs


def get_scheduled_jobs(redis: Redis) -> list[ScheduledJob]:
    """Get all scheduled jobs with their next run times."""
    jobs = []
    scheduled = redis.zrange(SCHEDULED_QUEUE, 0, -1, withscores=True)
    for name, timestamp in scheduled:
        if isinstance(name, bytes):
            name = name.decode()
        next_run = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        jobs.append(ScheduledJob(name=name, next_run=next_run))
    return jobs


def get_scheduled_workflows(redis: Redis) -> list[ScheduledWorkflow]:
    """Get all scheduled workflows with their next run times."""
    workflows = []
    scheduled = redis.zrange(SCHEDULED_WORKFLOW_QUEUE, 0, -1, withscores=True)
    for name, timestamp in scheduled:
        if isinstance(name, bytes):
            name = name.decode()
        next_run = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        workflows.append(ScheduledWorkflow(name=name, next_run=next_run))
    return workflows


def get_running_workflows(redis: Redis) -> list[WorkflowStatus]:
    """Get all running workflows with their current state."""
    workflows = []
    running = redis.hgetall(RUNNING_WORKFLOW_QUEUE)
    for name, state in running.items():
        if isinstance(name, bytes):
            name = name.decode()
        if isinstance(state, bytes):
            state = state.decode()

        data = json.loads(state)
        jobs = []
        for job_data in data.get("jobs", []):
            jobs.append(WorkflowJobStatus(
                name=job_data.get("name"),
                status=job_data.get("status"),
                requires=job_data.get("requires"),
                error=job_data.get("error")
            ))

        workflows.append(WorkflowStatus(
            name=data.get("name", name),
            schedule=data.get("schedule", ""),
            status=data.get("status"),
            error=data.get("error"),
            jobs=jobs
        ))
    return workflows


def get_workflow(redis: Redis, name: str) -> Optional[WorkflowStatus]:
    """Get a specific workflow by name."""
    # Check running workflows first
    running = redis.hget(RUNNING_WORKFLOW_QUEUE, name)
    if running:
        if isinstance(running, bytes):
            running = running.decode()
        data = json.loads(running)
        jobs = []
        for job_data in data.get("jobs", []):
            jobs.append(WorkflowJobStatus(
                name=job_data.get("name"),
                status=job_data.get("status"),
                requires=job_data.get("requires"),
                error=job_data.get("error")
            ))
        return WorkflowStatus(
            name=data.get("name", name),
            schedule=data.get("schedule", ""),
            status=data.get("status"),
            error=data.get("error"),
            jobs=jobs
        )

    # Check scheduled workflows
    score = redis.zscore(SCHEDULED_WORKFLOW_QUEUE, name)
    if score is not None:
        next_run = datetime.fromtimestamp(score, tz=timezone.utc)
        return WorkflowStatus(
            name=name,
            schedule="",  # Schedule not stored in sorted set
            status="scheduled",
            next_run=next_run,
            jobs=[]
        )

    return None


def enqueue_job(redis: Redis, name: str, params: list) -> bool:
    """Add a job to the pending queue."""
    redis.sadd(PENDING_QUEUE, json.dumps([name, params]))
    return True


def schedule_job(redis: Redis, name: str, run_at: datetime) -> bool:
    """Schedule a job for a specific time."""
    timestamp = run_at.timestamp()
    redis.zadd(SCHEDULED_QUEUE, {name: timestamp})
    return True


def cancel_pending_job(redis: Redis, name: str, params: list = None) -> bool:
    """Remove a job from the pending queue."""
    if params is None:
        params = []
    removed = redis.srem(PENDING_QUEUE, json.dumps([name, params]))
    return removed > 0


def cancel_scheduled_job(redis: Redis, name: str) -> bool:
    """Remove a job from the scheduled queue."""
    removed = redis.zrem(SCHEDULED_QUEUE, name)
    return removed > 0


def request_job_cancellation(redis: Redis, job_identifier: str) -> bool:
    """Request cancellation of a running job.

    The worker will check this set before executing jobs.
    """
    redis.sadd(CANCEL_QUEUE, job_identifier)
    return True


def check_job_cancelled(redis: Redis, job_identifier: str) -> bool:
    """Check if a job cancellation has been requested."""
    return redis.sismember(CANCEL_QUEUE, job_identifier)


def clear_job_cancellation(redis: Redis, job_identifier: str) -> bool:
    """Clear a job cancellation request."""
    redis.srem(CANCEL_QUEUE, job_identifier)
    return True


def trigger_workflow(redis: Redis, name: str) -> bool:
    """Trigger a workflow to run immediately.

    Moves the workflow from scheduled to immediate execution by setting
    its scheduled time to now.
    """
    now = datetime.now(timezone.utc).timestamp()
    # Remove from scheduled if present
    redis.zrem(SCHEDULED_WORKFLOW_QUEUE, name)
    # Add with current timestamp so it runs immediately
    redis.zadd(SCHEDULED_WORKFLOW_QUEUE, {name: now})
    return True


def cancel_scheduled_workflow(redis: Redis, name: str) -> bool:
    """Remove a workflow from the scheduled queue."""
    removed = redis.zrem(SCHEDULED_WORKFLOW_QUEUE, name)
    return removed > 0


def reschedule_workflow(redis: Redis, name: str, run_at: datetime) -> bool:
    """Reschedule a workflow for a specific time."""
    timestamp = run_at.timestamp()
    redis.zadd(SCHEDULED_WORKFLOW_QUEUE, {name: timestamp})
    return True
