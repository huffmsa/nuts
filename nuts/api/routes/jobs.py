"""Job API endpoints."""

from fastapi import APIRouter, HTTPException, Request

from ..models import (
    PendingJob,
    RunningJob,
    CompletedJob,
    ScheduledJob,
    JobEnqueueRequest,
    JobScheduleRequest,
    SuccessResponse,
)
from .. import queries

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/pending", response_model=list[PendingJob])
async def list_pending_jobs(request: Request) -> list[PendingJob]:
    """List all jobs in the pending queue."""
    return queries.get_pending_jobs(request.app.state.redis)


@router.get("/running", response_model=list[RunningJob])
async def list_running_jobs(request: Request) -> list[RunningJob]:
    """List all currently running jobs."""
    return queries.get_running_jobs(request.app.state.redis)


@router.get("/completed", response_model=list[CompletedJob])
async def list_completed_jobs(request: Request) -> list[CompletedJob]:
    """List all completed jobs."""
    return queries.get_completed_jobs(request.app.state.redis)


@router.get("/scheduled", response_model=list[ScheduledJob])
async def list_scheduled_jobs(request: Request) -> list[ScheduledJob]:
    """List all scheduled jobs with their next run times."""
    return queries.get_scheduled_jobs(request.app.state.redis)


@router.post("", response_model=SuccessResponse)
async def enqueue_job(request: Request, job: JobEnqueueRequest) -> SuccessResponse:
    """Enqueue a job for immediate execution."""
    queries.enqueue_job(request.app.state.redis, job.name, job.params)
    return SuccessResponse(message=f"Job '{job.name}' enqueued successfully")


@router.post("/schedule", response_model=SuccessResponse)
async def schedule_job(request: Request, job: JobScheduleRequest) -> SuccessResponse:
    """Schedule a job for a specific time."""
    queries.schedule_job(request.app.state.redis, job.name, job.run_at)
    return SuccessResponse(message=f"Job '{job.name}' scheduled for {job.run_at.isoformat()}")


@router.delete("/pending/{job_name}", response_model=SuccessResponse)
async def cancel_pending_job(request: Request, job_name: str) -> SuccessResponse:
    """Remove a job from the pending queue."""
    removed = queries.cancel_pending_job(request.app.state.redis, job_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found in pending queue")
    return SuccessResponse(message=f"Job '{job_name}' removed from pending queue")


@router.delete("/scheduled/{job_name}", response_model=SuccessResponse)
async def cancel_scheduled_job(request: Request, job_name: str) -> SuccessResponse:
    """Remove a job from the scheduled queue."""
    removed = queries.cancel_scheduled_job(request.app.state.redis, job_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found in scheduled queue")
    return SuccessResponse(message=f"Job '{job_name}' removed from scheduled queue")


@router.post("/running/{job_name}/cancel", response_model=SuccessResponse)
async def request_cancel_running_job(request: Request, job_name: str) -> SuccessResponse:
    """Request cancellation of a running job.

    Note: This sets a cancellation flag that workers check. The job will be
    cancelled when the worker next checks for cancellations, which may not
    be immediate if the job is mid-execution.
    """
    queries.request_job_cancellation(request.app.state.redis, job_name)
    return SuccessResponse(
        message=f"Cancellation requested for job '{job_name}'. "
                "The job will be cancelled when the worker processes the request."
    )
