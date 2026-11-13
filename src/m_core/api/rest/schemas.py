"""REST API schemas for M AI Assistant - Pydantic models for FastAPI compatibility."""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


class HealthStatus(str, Enum):
    """Health check status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ChatRole(str, Enum):
    """Chat message role."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class TaskStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNING = "replanning"
    AWAITING_APPROVAL = "awaiting_approval"


class ApprovalStatus(str, Enum):
    """Approval status for consequential actions."""
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    EXPIRED = "expired"


class WSMessageType(str, Enum):
    """WebSocket message types."""
    # Client -> Server
    CHAT = "chat"
    CANCEL_TASK = "cancel_task"
    APPROVE_ACTION = "approve_action"
    DENY_ACTION = "deny_action"
    PING = "ping"
    
    # Server -> Client
    CHAT_RESPONSE = "chat_response"
    CHAT_STREAM_CHUNK = "chat_stream_chunk"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_AWAITING_APPROVAL = "task_awaiting_approval"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    ERROR = "error"
    PONG = "pong"
    CONNECTION_ACK = "connection_ack"


class ChatMessage(BaseModel):
    """Chat message."""
    id: str = Field(default_factory=lambda: str(__import__('uuid').uuid4())[:8])
    role: ChatRole
    content: str
    name: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp() * 1000))

    model_config = ConfigDict(use_enum_values=True)


class ChatRequest(BaseModel):
    """Chat request payload."""
    message: str = Field(min_length=1)
    conversation_id: Optional[str] = None
    conversation_history: List[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    allowed_tools: List[str] = Field(default_factory=list)
    blocked_tools: List[str] = Field(default_factory=list)
    max_tool_calls: Optional[int] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class ChatResponse(BaseModel):
    """Chat response payload."""
    content: str
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Optional[Dict[str, int]] = None
    conversation_id: Optional[str] = None
    turn_count: int = 0
    tool_calls_made: int = 0
    task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class TaskInfo(BaseModel):
    """Task information."""
    task_id: str
    status: TaskStatus
    created_at: int
    conversation_id: Optional[str] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    subtasks: List[Dict[str, Any]] = Field(default_factory=list)
    result: Optional[Any] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class TaskCancelRequest(BaseModel):
    """Task cancellation request."""
    reason: str = "User requested cancellation"

    model_config = ConfigDict(use_enum_values=True)


class TaskApproveRequest(BaseModel):
    """Task approval/denial request."""
    approved: bool
    subtask_id: Optional[str] = None
    reason: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class MemorySearchRequest(BaseModel):
    """Memory search request."""
    query: str
    tiers: Optional[List[str]] = None
    limit: int = 10
    threshold: float = 0.7
    filters: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class MemoryEntryResponse(BaseModel):
    """Memory entry in API response."""
    id: str
    tier: str
    content: str
    created_at: int
    updated_at: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    expires_at: Optional[int] = None

    model_config = ConfigDict(use_enum_values=True)


class MemorySearchResponse(BaseModel):
    """Memory search response."""
    entries: List[MemoryEntryResponse] = Field(default_factory=list)
    total_count: int = 0
    query_time_ms: float = 0.0

    model_config = ConfigDict(use_enum_values=True)


class ToolInfo(BaseModel):
    """Tool information for API."""
    name: str
    display_name: str
    description: str
    category: str
    risk_level: str
    required_permissions: List[str] = Field(default_factory=list)
    optional_permissions: List[str] = Field(default_factory=list)
    requires_approval: bool = False
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    model_config = ConfigDict(use_enum_values=True)


class ToolsResponse(BaseModel):
    """Tools list response."""
    tools: List[ToolInfo] = Field(default_factory=list)

    model_config = ConfigDict(use_enum_values=True)


class SafeSettingsResponse(BaseModel):
    """Safe configuration settings for API."""
    app_name: str
    app_version: str
    debug: bool
    api_host: str
    api_port: int
    nvidia_model: str
    ollama_model: str
    openai_model: str
    anthropic_model: str
    available_providers: List[str] = Field(default_factory=list)

    model_config = ConfigDict(use_enum_values=True)


class HealthResponse(BaseModel):
    """Health check response."""
    status: HealthStatus
    timestamp: int
    version: str
    components: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    uptime_seconds: float = 0.0

    model_config = ConfigDict(use_enum_values=True)


class ErrorResponse(BaseModel):
    """Error response."""
    error: str
    error_code: str
    details: Optional[Dict[str, Any]] = None
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp() * 1000))

    model_config = ConfigDict(use_enum_values=True)


class WSEnvelope(BaseModel):
    """WebSocket message envelope."""
    type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    correlation_id: Optional[str] = None
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp() * 1000))

    model_config = ConfigDict(use_enum_values=True)


# Request/Response for WebSocket chat
class WSChatPayload(BaseModel):
    """WebSocket chat payload."""
    message: str
    conversation_id: Optional[str] = None
    conversation_history: List[ChatMessage] = Field(default_factory=list)
    stream: bool = True
    allowed_tools: List[str] = Field(default_factory=list)
    blocked_tools: List[str] = Field(default_factory=list)
    max_tool_calls: Optional[int] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class WSChatResponsePayload(BaseModel):
    """WebSocket chat response payload."""
    content: str
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    conversation_id: Optional[str] = None
    turn_count: int = 0
    tool_calls_made: int = 0
    task_id: Optional[str] = None
    is_stream_chunk: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class WSTaskProgressPayload(BaseModel):
    """WebSocket task progress payload."""
    task_id: str
    status: TaskStatus
    subtask_id: Optional[str] = None
    completed: int = 0
    total: int = 0
    message: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class WSTaskCompletedPayload(BaseModel):
    """WebSocket task completed payload."""
    task_id: str
    status: TaskStatus
    result: Optional[Any] = None
    error: Optional[str] = None
    execution_time_ms: float = 0.0

    model_config = ConfigDict(use_enum_values=True)


class WSApprovalRequiredPayload(BaseModel):
    """WebSocket approval required payload."""
    task_id: str
    subtask_id: str
    action: str
    description: str
    risk_level: str
    required_permissions: List[str] = Field(default_factory=list)

    model_config = ConfigDict(use_enum_values=True)


class WSApprovePayload(BaseModel):
    """WebSocket approve/deny payload."""
    task_id: str
    subtask_id: str
    approved: bool
    reason: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class WSToolPayload(BaseModel):
    """WebSocket tool execution payload."""
    task_id: str
    subtask_id: str
    tool_name: str
    status: str  # started, completed, failed
    result: Optional[Any] = None
    error: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


# Task board schemas
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


class TaskEvent(BaseModel):
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
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


class TaskEventStream(BaseModel):
    """Task event stream for streaming/reconnection."""
    task_id: str
    events: List[TaskEvent] = Field(default_factory=list)
    latest_event_id: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class SubtaskInfo(BaseModel):
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
    dependencies: List[str] = Field(default_factory=list)
    execution_time_ms: float = 0.0

    model_config = ConfigDict(use_enum_values=True)


class TaskListResponse(BaseModel):
    """Paginated task list response."""
    tasks: List[TaskInfo]
    total: int
    limit: int
    offset: int

    model_config = ConfigDict(use_enum_values=True)


class TaskBoardStats(BaseModel):
    """Task board statistics for dashboard."""
    total_tasks: int
    completed: int
    failed: int
    running: int
    pending: int
    by_status: Dict[str, int]

    model_config = ConfigDict(use_enum_values=True)


class TaskCancelRequest(BaseModel):
    """Task cancellation request."""
    reason: str = "User requested cancellation"

    model_config = ConfigDict(use_enum_values=True)


class TaskFilter(BaseModel):
    """Task filter for listing."""
    status: Optional[str] = None
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    date_from: Optional[int] = None
    date_to: Optional[int] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskEventFilter(BaseModel):
    """Task event filter for event streaming."""
    task_id: Optional[str] = None
    event_types: Optional[List[str]] = None
    priority: Optional[str] = None
    since_timestamp: Optional[int] = None

    model_config = ConfigDict(use_enum_values=True)


class NotificationRequest(BaseModel):
    """Notification request for human communication."""
    task_id: str
    level: str  # normal, attention, urgent
    title: str
    message: str
    action_required: bool = False
    approval_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(use_enum_values=True)


# Task event payloads for structured events
class TaskCreatedPayload(BaseModel):
    task_id: str
    conversation_id: Optional[str]
    objective: str

    model_config = ConfigDict(use_enum_values=True)


class TaskStartedPayload(BaseModel):
    task_id: str
    subtask_count: int

    model_config = ConfigDict(use_enum_values=True)


class TaskProgressPayload(BaseModel):
    task_id: str
    completed: int
    total: int
    current_step: Optional[str] = None
    current_step_name: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskStepStartedPayload(BaseModel):
    task_id: str
    subtask_id: str
    subtask_name: str
    assigned_target: Optional[str] = None
    control_method: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskStepCompletedPayload(BaseModel):
    task_id: str
    subtask_id: str
    result: Optional[str] = None
    duration_ms: float = 0

    model_config = ConfigDict(use_enum_values=True)


class TaskStepFailedPayload(BaseModel):
    task_id: str
    subtask_id: str
    error: str
    retry_count: int = 0
    will_retry: bool = False

    model_config = ConfigDict(use_enum_values=True)


class TaskVerificationPayload(BaseModel):
    task_id: str
    subtask_id: str
    verification_type: str
    passed: bool
    details: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskRetryPayload(BaseModel):
    task_id: str
    subtask_id: str
    attempt: int
    max_attempts: int
    backoff_seconds: float

    model_config = ConfigDict(use_enum_values=True)


class TaskReplanningPayload(BaseModel):
    task_id: str
    reason: str
    new_plan_id: str
    replan_count: int

    model_config = ConfigDict(use_enum_values=True)


class TaskApprovalRequiredPayload(BaseModel):
    task_id: str
    subtask_id: str
    action: str
    description: str
    risk_level: str
    approval_url: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskApprovedPayload(BaseModel):
    task_id: str
    subtask_id: str
    approved_by: str
    reason: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskDeniedPayload(BaseModel):
    task_id: str
    subtask_id: str
    denied_by: str
    reason: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskPausedPayload(BaseModel):
    task_id: str
    reason: str

    model_config = ConfigDict(use_enum_values=True)


class TaskResumedPayload(BaseModel):
    task_id: str
    from_checkpoint: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)


class TaskCancelledPayload(BaseModel):
    task_id: str
    reason: str

    model_config = ConfigDict(use_enum_values=True)


class TaskCompletedPayload(BaseModel):
    task_id: str
    result: Optional[Any] = None
    duration_ms: float = 0

    model_config = ConfigDict(use_enum_values=True)


class TaskFailedPayload(BaseModel):
    task_id: str
    error: str
    failed_subtasks: List[str] = Field(default_factory=list)

    model_config = ConfigDict(use_enum_values=True)


class TaskVerificationFailedPayload(BaseModel):
    task_id: str
    subtask_id: str
    expected: str
    actual: str
    verification_type: str

    model_config = ConfigDict(use_enum_values=True)


class TaskRecoveryPayload(BaseModel):
    task_id: str
    recovery_action: str
    failed_subtask_id: str
    reason: str

    model_config = ConfigDict(use_enum_values=True)


__all__ = [
    "HealthStatus",
    "ChatRole",
    "TaskStatus",
    "ApprovalStatus",
    "WSMessageType",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "TaskInfo",
    "TaskCancelRequest",
    "TaskApproveRequest",
    "MemorySearchRequest",
    "MemoryEntryResponse",
    "MemorySearchResponse",
    "ToolInfo",
    "ToolsResponse",
    "SafeSettingsResponse",
    "HealthResponse",
    "ErrorResponse",
    "WSEnvelope",
    "WSChatPayload",
    "WSChatResponsePayload",
    "WSTaskProgressPayload",
    "WSTaskCompletedPayload",
    "WSApprovalRequiredPayload",
    "WSApprovePayload",
    "WSToolPayload",
    # Task board schemas
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
]