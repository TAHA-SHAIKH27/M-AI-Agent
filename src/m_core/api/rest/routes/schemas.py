"""Schemas for task board API routes."""
import msgspec
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    """Task status for API responses."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNING = "replanning"
    AWAITING_APPROVAL = "awaiting_approval"


class TaskEventType(str, Enum):
    """Task event types for event streaming."""
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_STEP_STARTED = "task.step_started"
    TASK_STEP_COMPLETED = "task.step_completed"
    TASK_STEP_FAILED = "task.step_failed"
    TASK_VERIFICATION = "task.verification"
    TASK_RETRY = "task.retry"
    TASK_REPLANNING = "task.replanning"
    TASK_AWAITING_APPROVAL = "task.awaiting_approval"
    TASK_APPROVED = "task.approved"
    TASK_DENIED = "task.denied"
    TASK_PAUSED = "task.paused"
    TASK_RESUMED = "task.resumed"
    TASK_CANCELLED = "task.cancelled"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_VERIFICATION_FAILED = "task.verification_failed"
    TASK_RECOVERY = "task.recovery"


class TaskEventPriority(str, Enum):
    """Task event priority/notification level."""
    NORMAL = "normal"       # Level 1 - Normal notification
    ATTENTION = "attention" # Level 2 - Attention required
    URGENT = "urgent"       # Level 3 - Urgent intervention required


class TaskEvent(msgspec.Struct, frozen=True):
    """Task event for real-time streaming."""
    event_id: str
    task_id: str
    event_type: str
    timestamp: int
    priority: str = "normal"
    step_id: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class TaskEventStream(msgspec.Struct, frozen=True):
    """Task event stream for streaming/reconnection."""
    task_id: str
    events: List[TaskEvent] = msgspec.field(default_factory=list)
    latest_event_id: Optional[str] = None


class SubtaskInfo(msgspec.Struct, frozen=True):
    """Subtask information for task board."""
    subtask_id: str
    status: str
    assigned_target_type: Optional[str] = None
    assigned_target_id: Optional[str] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    retry_count: int = 0
    dependencies: List[str] = msgspec.field(default_factory=list)
    execution_time_ms: float = 0.0


class TaskInfo(msgspec.Struct, frozen=True):
    """Task information for API responses."""
    task_id: str
    status: str
    conversation_id: Optional[str] = None
    created_at: int
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    subtasks: List[Dict[str, Any]] = msgspec.field(default_factory=list)
    result: Optional[Any] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class TaskListResponse(msgspec.Struct, frozen=True):
    """Paginated task list response."""
    tasks: List[TaskInfo]
    total: int
    limit: int
    offset: int


class TaskBoardStats(msgspec.Struct, frozen=True):
    """Task board statistics for dashboard."""
    total_tasks: int
    completed: int
    failed: int
    running: int
    pending: int
    by_status: Dict[str, int]


class TaskApproveRequest(msgspec.Struct, frozen=True):
    """Approval request for task approval endpoint."""
    subtask_id: Optional[str] = None
    approved: bool = True
    reason: Optional[str] = None


class TaskCancelRequest(msgspec.Struct, frozen=True):
    """Task cancellation request."""
    reason: Optional[str] = None


class TaskFilter(msgspec.Struct, frozen=True):
    """Task filter for listing."""
    status: Optional[str] = None
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    date_from: Optional[int] = None
    date_to: Optional[int] = None


class TaskEventFilter(msgspec.Struct, frozen=True):
    """Task event filter for event streaming."""
    task_id: Optional[str] = None
    event_types: Optional[List[str]] = None
    priority: Optional[str] = None
    since_timestamp: Optional[int] = None


class NotificationRequest(msgspec.Struct, frozen=True):
    """Notification request for human communication."""
    task_id: str
    level: str  # normal, attention, urgent
    title: str
    message: str
    action_required: bool = False
    approval_url: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


# Task event payloads for structured events
class TaskCreatedPayload(msgspec.Struct, frozen=True):
    task_id: str
    conversation_id: Optional[str]
    objective: str


class TaskStartedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_count: int


class TaskProgressPayload(msgspec.Struct, frozen=True):
    task_id: str
    completed: int
    total: int
    current_step: Optional[str] = None
    current_step_name: Optional[str] = None


class TaskStepStartedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    subtask_name: str
    assigned_target: Optional[str] = None
    control_method: Optional[str] = None


class TaskStepCompletedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    result: Optional[str] = None
    duration_ms: float = 0


class TaskStepFailedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    error: str
    retry_count: int = 0
    will_retry: bool = False


class TaskVerificationPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    verification_type: str
    passed: bool
    details: Optional[str] = None


class TaskRetryPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    attempt: int
    max_attempts: int
    backoff_seconds: float


class TaskReplanningPayload(msgspec.Struct, frozen=True):
    task_id: str
    reason: str
    new_plan_id: str
    replan_count: int


class TaskApprovalRequiredPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    action: str
    description: str
    risk_level: str
    approval_url: Optional[str] = None


class TaskApprovedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    approved_by: str
    reason: Optional[str] = None


class TaskDeniedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    denied_by: str
    reason: Optional[str] = None


class TaskPausedPayload(msgspec.Struct, frozen=True):
    task_id: str
    reason: str


class TaskResumedPayload(msgspec.Struct, frozen=True):
    task_id: str
    from_checkpoint: Optional[str] = None


class TaskCancelledPayload(msgspec.Struct, frozen=True):
    task_id: str
    reason: str


class TaskCompletedPayload(msgspec.Struct, frozen=True):
    task_id: str
    result: Optional[Any] = None
    duration_ms: float = 0


class TaskFailedPayload(msgspec.Struct, frozen=True):
    task_id: str
    error: str
    failed_subtasks: List[str] = msgspec.field(default_factory=list)


class TaskVerificationFailedPayload(msgspec.Struct, frozen=True):
    task_id: str
    subtask_id: str
    expected: str
    actual: str
    verification_type: str


class TaskRecoveryPayload(msgspec.Struct, frozen=True):
    task_id: str
    recovery_action: str
    failed_subtask_id: str
    reason: str


__all__ = [
    "TaskStatus",
    "TaskEventType",
    "TaskEventPriority",
    "TaskEvent",
    "TaskEventStream",
    "SubtaskInfo",
    "TaskInfo",
    "TaskListResponse",
    "TaskBoardStats",
    "TaskApproveRequest",
    "TaskCancelRequest",
    "TaskFilter",
    "TaskEventFilter",
    "NotificationRequest",
    "TaskEventPayload",
    "TaskCreatedPayload",
    "TaskStartedPayload",
    "TaskProgressPayload",
    "TaskStepStartedPayload",
    "TaskStepCompletedPayload",
    "TaskStepFailedPayload",
    "TaskVerificationPayload",
    "TaskRetryPayload",
    "TaskReplanningPayload",
    "TaskApprovalRequiredPayload",
    "TaskApprovedPayload",
    "TaskDeniedPayload",
    "TaskPausedPayload",
    "TaskResumedPayload",
    "TaskCancelledPayload",
    "TaskCompletedPayload",
    "TaskFailedPayload",
    "TaskVerificationFailedPayload",
    "TaskRecoveryPayload",
]