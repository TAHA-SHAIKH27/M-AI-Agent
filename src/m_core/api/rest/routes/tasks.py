"""Task board API routes for M AI Assistant."""
import uuid
import structlog
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from m_core.orchestration import orchestrator
from m_core.orchestration.schemas import TaskExecutionStatus, SubtaskExecutionStatus
from m_core.orchestration.state_manager import state_manager
from m_core.core.events import event_bus
from m_core.security import consent_manager
from m_core.utils.logging import get_logger

from ..schemas import (
    TaskInfo, TaskStatus, TaskListResponse, TaskEvent, TaskEventStream,
    TaskFilter, TaskBoardStats, ErrorResponse,
)
from ..dependencies import (
    get_client_info, validate_localhost, APIClientInfo,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "",
    response_model=TaskListResponse,
    summary="List tasks",
    description="Get a paginated list of tasks with optional filtering",
)
async def list_tasks(
    status: Optional[str] = Query(None, description="Filter by task status"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of tasks to return"),
    offset: int = Query(0, ge=0, description="Number of tasks to skip"),
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskListResponse:
    """List tasks with optional filtering and pagination."""
    try:
        # Convert status filter
        status_filter = None
        if status:
            try:
                status_filter = TaskExecutionStatus(status)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"error": f"Invalid status: {status}", "error_code": "INVALID_STATUS"},
                )
        
        records = await state_manager.list_executions(status=status_filter, limit=limit + offset)
        
        # Apply offset
        records = records[offset:offset + limit]
        
        tasks = []
        for record in records:
            subtasks = []
            for sr in record.subtask_records:
                subtasks.append({
                    "subtask_id": sr.subtask_id,
                    "status": sr.status.value,
                    "assigned_target_type": sr.assigned_target_type,
                    "assigned_target_id": sr.assigned_target_id,
                    "started_at": sr.started_at,
                    "completed_at": sr.completed_at,
                    "result": sr.result,
                    "error": sr.error,
                    "retry_count": sr.retry_count,
                    "dependencies": sr.dependencies,
                    "execution_time_ms": sr.execution_time_ms,
                })
            
            status_map = {
                TaskExecutionStatus.PENDING: "pending",
                TaskExecutionStatus.QUEUED: "queued",
                TaskExecutionStatus.RUNNING: "running",
                TaskExecutionStatus.WAITING: "waiting",
                TaskExecutionStatus.COMPLETED: "completed",
                TaskExecutionStatus.FAILED: "failed",
                TaskExecutionStatus.CANCELLED: "cancelled",
                TaskExecutionStatus.REPLANNING: "replanning",
                TaskExecutionStatus.AWAITING_APPROVAL: "awaiting_approval",
            }
            
            tasks.append(TaskInfo(
                task_id=record.task_id,
                status=status_map.get(record.status, "pending"),
                conversation_id=record.conversation_id,
                created_at=record.created_at,
                started_at=record.started_at,
                completed_at=record.completed_at,
                error=record.error,
                error_code=record.error_code,
                subtasks=subtasks,
                result=record.result,
                metadata=record.metadata,
            ))
        
        # Get total count for pagination
        all_records = await state_manager.list_executions(status=status_filter, limit=10000)
        total = len(all_records)
        
        return TaskListResponse(
            tasks=tasks,
            total=total,
            limit=limit,
            offset=offset,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_tasks_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to list tasks",
                "error_code": "TASK_LIST_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/stats",
    response_model=TaskBoardStats,
    summary="Get task board statistics",
    description="Get aggregate statistics for the task board dashboard",
)
async def get_task_stats(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskBoardStats:
    """Get aggregate statistics for the task board."""
    try:
        all_records = await state_manager.list_executions(limit=10000)
        
        total = len(all_records)
        by_status = {}
        for record in all_records:
            status = record.status.value
            by_status[status] = by_status.get(status, 0) + 1
        
        completed = sum(1 for r in all_records if r.status.value == "completed")
        failed = sum(1 for r in all_records if r.status.value == "failed")
        running = sum(1 for r in all_records if r.status.value in ("running", "waiting", "queued"))
        pending = sum(1 for r in all_records if r.status.value in ("pending", "queued"))
        
        return TaskBoardStats(
            total_tasks=total,
            completed=completed,
            failed=failed,
            running=running,
            pending=pending,
            by_status=by_status,
        )
        
    except Exception as e:
        logger.error("get_task_stats_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get task statistics",
                "error_code": "TASK_STATS_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/{task_id}",
    response_model=TaskInfo,
    summary="Get task details",
    description="Get detailed information about a specific task including all subtasks",
)
async def get_task(
    task_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Get detailed information about a specific task."""
    try:
        execution_record = await orchestrator.get_task_status(task_id)
        
        if not execution_record:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        subtasks = []
        for sr in execution_record.subtask_records:
            subtasks.append({
                "subtask_id": sr.subtask_id,
                "status": sr.status.value,
                "assigned_target_type": sr.assigned_target_type,
                "assigned_target_id": sr.assigned_target_id,
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "result": sr.result,
                "error": sr.error,
                "retry_count": sr.retry_count,
                "dependencies": sr.dependencies,
                "execution_time_ms": sr.execution_time_ms,
            })
        
        status_map = {
            TaskExecutionStatus.PENDING: "pending",
            TaskExecutionStatus.QUEUED: "queued",
            TaskExecutionStatus.RUNNING: "running",
            TaskExecutionStatus.WAITING: "waiting",
            TaskExecutionStatus.COMPLETED: "completed",
            TaskExecutionStatus.FAILED: "failed",
            TaskExecutionStatus.CANCELLED: "cancelled",
            TaskExecutionStatus.REPLANNING: "replanning",
            TaskExecutionStatus.AWAITING_APPROVAL: "awaiting_approval",
        }
        
        return TaskInfo(
            task_id=execution_record.task_id,
            status=status_map.get(execution_record.status, "pending"),
            conversation_id=execution_record.conversation_id,
            created_at=execution_record.created_at,
            started_at=execution_record.started_at,
            completed_at=execution_record.completed_at,
            error=execution_record.error,
            error_code=execution_record.error_code,
            subtasks=subtasks,
            result=execution_record.result,
            metadata=execution_record.metadata,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get task",
                "error_code": "TASK_GET_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/{task_id}/events",
    response_model=TaskEventStream,
    summary="Get task event stream",
    description="Get the event stream for a task (for replay/reconnection)",
)
async def get_task_events(
    task_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskEventStream:
    """Get the event stream for a task."""
    # This would require event persistence - for now return empty
    # In a full implementation, events would be persisted alongside task state
    try:
        record = await state_manager.get_execution_record(task_id)
        if not record:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Task '{task_id}' not found", "error_code": "TASK_NOT_FOUND"},
            )
        
        return TaskEventStream(
            task_id=task_id,
            events=[],  # Would be populated from event store
            latest_event_id=None,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_task_events_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get task events",
                "error_code": "TASK_EVENTS_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/{task_id}/cancel",
    response_model=TaskInfo,
    summary="Cancel a task",
    description="Cancel a running or pending task",
)
async def cancel_task(
    task_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Cancel a running or pending task."""
    try:
        success = await orchestrator.cancel_task(task_id)
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found or not cancellable",
                    "error_code": "TASK_NOT_CANCELLABLE",
                },
            )
        
        return await get_task(task_id, client)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cancel_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to cancel task",
                "error_code": "TASK_CANCEL_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/{task_id}/approve",
    summary="Approve or reject a task approval request",
    description="Submit user approval or rejection for a task awaiting approval",
)
async def approve_task(
    task_id: str,
    subtask_id: Optional[str] = None,
    approved: bool = True,
    reason: Optional[str] = None,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Approve or deny a task action."""
    from ..schemas import TaskApproveRequest
    
    request = TaskApproveRequest(
        subtask_id=subtask_id,
        approved=approved,
        reason=reason,
    )
    
    try:
        success = await orchestrator.approve_subtask(
            task_id=task_id,
            subtask_id=request.subtask_id,
            approved=request.approved,
            reason=request.reason,
        )
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found or not awaiting approval",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        return await get_task(task_id)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("approve_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to process task approval",
                "error_code": "TASK_APPROVAL_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/{task_id}/checkpoints",
    summary="List task checkpoints",
    description="Get all checkpoints for a task (for recovery/resume)",
)
async def list_checkpoints(
    task_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> Dict[str, Any]:
    """List all checkpoints for a task."""
    try:
        checkpoints = await state_manager.get_task_checkpoints(task_id)
        
        return {
            "task_id": task_id,
            "checkpoints": [
                {
                    "checkpoint_id": cp.checkpoint_id,
                    "checkpoint_type": cp.checkpoint_type.value,
                    "created_at": cp.created_at,
                    "completed_subtasks": cp.completed_subtasks,
                    "failed_subtasks": cp.failed_subtasks,
                    "metadata": cp.metadata,
                }
                for cp in checkpoints
            ],
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_checkpoints_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to list checkpoints",
                "error_code": "CHECKPOINT_LIST_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/{task_id}/resume",
    summary="Resume task from checkpoint",
    description="Resume task execution from a specific checkpoint",
)
async def resume_from_checkpoint(
    task_id: str,
    checkpoint_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Resume task execution from a checkpoint."""
    try:
        record = await orchestrator.resume_from_checkpoint(checkpoint_id)
        
        if not record or record.task_id != task_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Checkpoint '{checkpoint_id}' not found for task '{task_id}'",
                    "error_code": "CHECKPOINT_NOT_FOUND",
                },
            )
        
        return await get_task(task_id)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("resume_from_checkpoint_failed", task_id=task_id, checkpoint_id=checkpoint_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to resume from checkpoint",
                "error_code": "RESUME_FAILED",
                "details": {"message": str(e)},
            },
        )