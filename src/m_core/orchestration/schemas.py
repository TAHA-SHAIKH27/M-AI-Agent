"""Orchestration schemas for M AI Assistant."""
import msgspec
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


class TaskExecutionStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNING = "replanning"
    AWAITING_APPROVAL = "awaiting_approval"


class SubtaskExecutionStatus(str, Enum):
    """Subtask execution status."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class CheckpointType(str, Enum):
    """Checkpoint type."""
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    PRE_EXECUTION = "pre_execution"
    POST_EXECUTION = "post_execution"
    ERROR = "error"


class RecoveryAction(str, Enum):
    """Recovery action type."""
    RETRY = "retry"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    FALLBACK_TOOL = "fallback_tool"
    FALLBACK_AGENT = "fallback_agent"
    FALLBACK_PROVIDER = "fallback_provider"
    REPLAN = "replan"
    SKIP = "skip"
    CANCEL = "cancel"
    ESCALATE = "escalate"


class EvaluationResult(str, Enum):
    """Result evaluation outcome."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    NEEDS_REVIEW = "needs_review"


class TaskExecutionRecord(msgspec.Struct):
    """Record of a task execution."""
    task_id: str
    execution_plan_id: str
    routing_plan_id: Optional[str] = None
    status: TaskExecutionStatus = TaskExecutionStatus.PENDING
    created_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    parent_task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    subtask_records: List["SubtaskExecutionRecord"] = msgspec.field(default_factory=list)
    result: Optional[Any] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 3
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class SubtaskExecutionRecord(msgspec.Struct):
    """Record of a subtask execution."""
    subtask_id: str
    task_id: str
    status: SubtaskExecutionStatus = SubtaskExecutionStatus.PENDING
    assigned_target_type: Optional[str] = None  # agent, tool, provider
    assigned_target_id: Optional[str] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 2
    dependencies: List[str] = msgspec.field(default_factory=list)
    execution_time_ms: float = 0.0
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class Checkpoint(msgspec.Struct, frozen=True):
    """Execution checkpoint for recovery."""
    checkpoint_id: str
    task_id: str
    checkpoint_type: CheckpointType = CheckpointType.AUTOMATIC
    created_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    execution_state: Dict[str, Any] = msgspec.field(default_factory=dict)
    subtask_states: Dict[str, Dict[str, Any]] = msgspec.field(default_factory=dict)
    completed_subtasks: List[str] = msgspec.field(default_factory=list)
    failed_subtasks: List[str] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class RecoveryPlan(msgspec.Struct, frozen=True):
    """Plan for recovering from failure."""
    task_id: str
    failed_subtask_id: Optional[str] = None
    action: RecoveryAction = RecoveryAction.RETRY
    reason: str = ""
    max_attempts: int = 3
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    fallback_target: Optional[Dict[str, Any]] = None
    new_execution_plan_id: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class SubtaskEvaluation(msgspec.Struct, frozen=True):
    """Evaluation of a subtask result."""
    subtask_id: str
    result: EvaluationResult = EvaluationResult.SUCCESS
    score: float = 1.0  # 0.0 to 1.0
    criteria_met: List[str] = msgspec.field(default_factory=list)
    criteria_failed: List[str] = msgspec.field(default_factory=list)
    missing_outputs: List[str] = msgspec.field(default_factory=list)
    issues: List[str] = msgspec.field(default_factory=list)
    recommendations: List[str] = msgspec.field(default_factory=list)
    confidence: float = 1.0


class TaskEvaluation(msgspec.Struct, frozen=True):
    """Evaluation of overall task execution."""
    task_id: str
    overall_result: EvaluationResult = EvaluationResult.SUCCESS
    subtask_evaluations: List[SubtaskEvaluation] = msgspec.field(default_factory=list)
    completed_subtasks: int = 0
    failed_subtasks: int = 0
    skipped_subtasks: int = 0
    total_subtasks: int = 0
    execution_time_ms: float = 0.0
    summary: str = ""
    issues: List[str] = msgspec.field(default_factory=list)
    recommendations: List[str] = msgspec.field(default_factory=list)
    consolidated_result: Optional[Any] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class SchedulerConfig(msgspec.Struct, frozen=True):
    """Scheduler configuration."""
    max_concurrency: int = 3
    default_timeout_seconds: float = 60.0
    enable_parallel: bool = True
    priority_boost_for_retries: bool = True
    dependency_check_interval_ms: int = 100


__all__ = [
    "TaskExecutionStatus",
    "SubtaskExecutionStatus",
    "CheckpointType",
    "RecoveryAction",
    "EvaluationResult",
    "TaskExecutionRecord",
    "SubtaskExecutionRecord",
    "Checkpoint",
    "RecoveryPlan",
    "SubtaskEvaluation",
    "TaskEvaluation",
    "SchedulerConfig",
]