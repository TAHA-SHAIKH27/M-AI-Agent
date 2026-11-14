"""Shared REST API response serializers."""
from .schemas import TaskInfo, TaskStatus
from m_core.orchestration.schemas import TaskExecutionRecord, TaskExecutionStatus

TASK_EXECUTION_STATUS_MAP = {
    TaskExecutionStatus.PENDING: TaskStatus.PENDING,
    TaskExecutionStatus.QUEUED: TaskStatus.QUEUED,
    TaskExecutionStatus.RUNNING: TaskStatus.RUNNING,
    TaskExecutionStatus.WAITING: TaskStatus.WAITING,
    TaskExecutionStatus.PAUSED: TaskStatus.PAUSED,
    TaskExecutionStatus.COMPLETED: TaskStatus.COMPLETED,
    TaskExecutionStatus.FAILED: TaskStatus.FAILED,
    TaskExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
    TaskExecutionStatus.REPLANNING: TaskStatus.REPLANNING,
    TaskExecutionStatus.AWAITING_APPROVAL: TaskStatus.AWAITING_APPROVAL,
}


def execution_record_to_task_info(
    execution_record: TaskExecutionRecord,
    default_status: TaskStatus = TaskStatus.PENDING,
) -> TaskInfo:
    """Convert an orchestration TaskExecutionRecord into an API TaskInfo model."""
    subtasks = [
        {
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
        }
        for sr in execution_record.subtask_records
    ]

    return TaskInfo(
        task_id=execution_record.task_id,
        status=TASK_EXECUTION_STATUS_MAP.get(execution_record.status, default_status),
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

