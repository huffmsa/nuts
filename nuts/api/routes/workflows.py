"""Workflow API endpoints."""

from fastapi import APIRouter, HTTPException, Request

from ..models import (
    WorkflowStatus,
    ScheduledWorkflow,
    WorkflowRescheduleRequest,
    SuccessResponse,
)
from .. import queries

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("", response_model=list[WorkflowStatus | ScheduledWorkflow])
async def list_workflows(request: Request) -> list[WorkflowStatus | ScheduledWorkflow]:
    """List all workflows (both scheduled and running)."""
    scheduled = queries.get_scheduled_workflows(request.app.state.redis)
    running = queries.get_running_workflows(request.app.state.redis)

    # Combine, preferring running status if a workflow appears in both
    running_names = {w.name for w in running}
    result: list[WorkflowStatus | ScheduledWorkflow] = list(running)
    for w in scheduled:
        if w.name not in running_names:
            result.append(w)

    return result


@router.get("/scheduled", response_model=list[ScheduledWorkflow])
async def list_scheduled_workflows(request: Request) -> list[ScheduledWorkflow]:
    """List all scheduled workflows with their next run times."""
    return queries.get_scheduled_workflows(request.app.state.redis)


@router.get("/running", response_model=list[WorkflowStatus])
async def list_running_workflows(request: Request) -> list[WorkflowStatus]:
    """List all running workflows with their current state."""
    return queries.get_running_workflows(request.app.state.redis)


@router.get("/{name}", response_model=WorkflowStatus)
async def get_workflow(request: Request, name: str) -> WorkflowStatus:
    """Get details for a specific workflow."""
    workflow = queries.get_workflow(request.app.state.redis, name)
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    return workflow


@router.post("/{name}/trigger", response_model=SuccessResponse)
async def trigger_workflow(request: Request, name: str) -> SuccessResponse:
    """Trigger a workflow to run immediately.

    This sets the workflow's scheduled time to now, causing it to be
    picked up on the next leader worker cycle.
    """
    queries.trigger_workflow(request.app.state.redis, name)
    return SuccessResponse(message=f"Workflow '{name}' triggered for immediate execution")


@router.delete("/{name}/scheduled", response_model=SuccessResponse)
async def cancel_scheduled_workflow(request: Request, name: str) -> SuccessResponse:
    """Remove a workflow from the scheduled queue."""
    removed = queries.cancel_scheduled_workflow(request.app.state.redis, name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found in scheduled queue")
    return SuccessResponse(message=f"Workflow '{name}' removed from scheduled queue")


@router.post("/{name}/reschedule", response_model=SuccessResponse)
async def reschedule_workflow(
    request: Request, name: str, schedule: WorkflowRescheduleRequest
) -> SuccessResponse:
    """Reschedule a workflow for a specific time."""
    queries.reschedule_workflow(request.app.state.redis, name, schedule.run_at)
    return SuccessResponse(message=f"Workflow '{name}' rescheduled for {schedule.run_at.isoformat()}")
