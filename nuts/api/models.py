"""Pydantic models for API request/response schemas."""

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class PendingJob(BaseModel):
    """A job waiting in the pending queue."""
    name: str
    params: list[Any] = Field(default_factory=list)


class RunningJob(BaseModel):
    """A currently executing job."""
    name: str
    worker_id: str
    started_at: datetime
    params: list[Any] = Field(default_factory=list)


class CompletedJob(BaseModel):
    """A completed job with result information."""
    name: str
    success: bool
    error: Optional[str] = None
    workflow_name: Optional[str] = None


class ScheduledJob(BaseModel):
    """A job scheduled for future execution."""
    name: str
    next_run: datetime


class WorkflowJobStatus(BaseModel):
    """Status of a job within a workflow."""
    name: str
    status: Optional[str] = None
    requires: Optional[list[str]] = None
    error: Optional[str] = None


class WorkflowStatus(BaseModel):
    """Current status of a workflow."""
    name: str
    schedule: str
    status: Optional[str] = None
    error: Optional[str] = None
    jobs: list[WorkflowJobStatus] = Field(default_factory=list)
    next_run: Optional[datetime] = None


class ScheduledWorkflow(BaseModel):
    """A workflow scheduled for future execution."""
    name: str
    next_run: datetime


class JobEnqueueRequest(BaseModel):
    """Request to enqueue a new job."""
    name: str
    params: list[Any] = Field(default_factory=list)


class JobScheduleRequest(BaseModel):
    """Request to schedule a job for a specific time."""
    name: str
    run_at: datetime
    params: list[Any] = Field(default_factory=list)


class WorkflowTriggerRequest(BaseModel):
    """Request to trigger a workflow immediately."""
    pass


class WorkflowRescheduleRequest(BaseModel):
    """Request to reschedule a workflow."""
    run_at: datetime


class SuccessResponse(BaseModel):
    """Generic success response."""
    success: bool = True
    message: str


class ErrorResponse(BaseModel):
    """Error response."""
    success: bool = False
    error: str


class JobListResponse(BaseModel):
    """Response containing a list of jobs."""
    jobs: list[PendingJob | RunningJob | CompletedJob | ScheduledJob]
    count: int


class WorkflowListResponse(BaseModel):
    """Response containing a list of workflows."""
    workflows: list[WorkflowStatus | ScheduledWorkflow]
    count: int
