"""Planning schemas for M AI Assistant."""
import msgspec
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime


class TaskType(str, Enum):
    """Type of task."""
    SIMPLE = "simple"           # Direct answer, no tools needed
    TOOL_USE = "tool_use"       # Requires tool execution
    MULTI_STEP = "multi_step"   # Multiple subtasks with dependencies
    COMPLEX = "complex"         # Complex planning required


class TaskComplexity(str, Enum):
    """Task complexity level."""
    TRIVIAL = "trivial"         # Single turn, no tools
    LOW = "low"                 # 1-2 tools, no dependencies
    MEDIUM = "medium"           # Multiple tools, some dependencies
    HIGH = "high"               # Many tools, complex dependencies
    CRITICAL = "critical"       # Requires careful planning, human oversight


class TaskStatus(str, Enum):
    """Task execution status."""
    PENDING = "pending"
    ANALYZING = "analyzing"
    PLANNED = "planned"
    ROUTED = "routed"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REPLANNING = "replanning"
    AWAITING_APPROVAL = "awaiting_approval"


class SubtaskStatus(str, Enum):
    """Subtask execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ExecutionStrategy(str, Enum):
    """Execution strategy for task."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    DEPENDENCY_AWARE = "dependency_aware"


class RoutingTargetType(str, Enum):
    """Type of routing target."""
    AGENT = "agent"
    TOOL = "tool"
    MODEL_PROVIDER = "model_provider"


class ApprovalStatus(str, Enum):
    """Approval status for consequential actions."""
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


class TaskIntent(msgspec.Struct, frozen=True):
    """Identified user intent."""
    primary: str
    secondary: List[str] = msgspec.field(default_factory=list)
    confidence: float = 1.0
    reasoning: str = ""


class TaskEntity(msgspec.Struct, frozen=True):
    """Extracted entity from user request."""
    name: str
    type: str
    value: Any
    confidence: float = 1.0
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class TaskRequirement(msgspec.Struct, frozen=True):
    """Identified requirement for task completion."""
    capability: str
    description: str
    required: bool = True
    alternatives: List[str] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class TaskAnalysis(msgspec.Struct, frozen=True):
    """Complete task analysis result."""
    intent: TaskIntent
    entities: List[TaskEntity] = msgspec.field(default_factory=list)
    requirements: List[TaskRequirement] = msgspec.field(default_factory=list)
    task_type: TaskType = TaskType.SIMPLE
    complexity: TaskComplexity = TaskComplexity.TRIVIAL
    needs_memory: bool = False
    needs_tools: bool = False
    estimated_subtasks: int = 1
    estimated_duration_seconds: float = 5.0
    risk_level: str = "low"
    requires_approval: bool = False
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class Subtask(msgspec.Struct, frozen=True):
    """A single subtask in a plan."""
    id: str
    name: str
    description: str
    task_type: TaskType = TaskType.TOOL_USE
    assigned_agent: Optional[str] = None
    assigned_tool: Optional[str] = None
    dependencies: List[str] = msgspec.field(default_factory=list)  # Subtask IDs
    priority: int = 0  # Higher = more important
    estimated_duration_seconds: float = 10.0
    max_retries: int = 2
    timeout_seconds: float = 60.0
    requires_approval: bool = False
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUIRED
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class ExecutionPlan(msgspec.Struct, frozen=True):
    """Complete execution plan."""
    task_id: str
    original_request: str
    analysis: TaskAnalysis
    subtasks: List[Subtask] = msgspec.field(default_factory=list)
    strategy: ExecutionStrategy = ExecutionStrategy.DEPENDENCY_AWARE
    max_concurrency: int = 3
    total_estimated_duration: float = 0.0
    created_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class RoutingDecision(msgspec.Struct, frozen=True):
    """Routing decision for a subtask."""
    subtask_id: str
    target_type: RoutingTargetType
    target_id: str  # Agent ID, Tool ID, or Provider ID
    confidence: float = 1.0
    reasoning: str = ""
    fallback_targets: List[Dict[str, Any]] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class RoutingPlan(msgspec.Struct, frozen=True):
    """Complete routing plan for an execution plan."""
    execution_plan_id: str
    decisions: List[RoutingDecision] = msgspec.field(default_factory=list)
    model_provider: Optional[str] = None
    fallback_provider: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class TaskContext(msgspec.Struct, frozen=True):
    """Context for task execution."""
    task_id: str
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None
    session_data: Dict[str, Any] = msgspec.field(default_factory=dict)
    memory_references: List[str] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


# Re-export for convenience
__all__ = [
    "TaskType",
    "TaskComplexity",
    "TaskStatus",
    "SubtaskStatus",
    "ExecutionStrategy",
    "RoutingTargetType",
    "ApprovalStatus",
    "TaskIntent",
    "TaskEntity",
    "TaskRequirement",
    "TaskAnalysis",
    "Subtask",
    "ExecutionPlan",
    "RoutingDecision",
    "RoutingPlan",
    "TaskContext",
]