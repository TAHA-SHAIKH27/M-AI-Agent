"""Agent schemas for M AI Assistant."""
import msgspec
from typing import Optional, List, Dict, Any, Literal
from enum import Enum
from datetime import datetime


class AgentStatus(str, Enum):
    """Agent status."""
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    ERROR = "error"
    STOPPED = "stopped"


class AgentType(str, Enum):
    """Agent type."""
    GENERAL = "general"
    SPECIALIZED = "specialized"
    TOOL = "tool"


class AgentCapability(str, Enum):
    """Agent capability identifiers."""
    CHAT = "chat"
    TOOL_USE = "tool_use"
    REASONING = "reasoning"
    PLANNING = "planning"
    MEMORY_ACCESS = "memory_access"
    FILE_ACCESS = "file_access"
    WEB_ACCESS = "web_access"
    SYSTEM_ACCESS = "system_access"


class AgentManifest(msgspec.Struct, frozen=True):
    """Agent manifest with metadata."""
    id: str
    name: str
    version: str
    description: str
    type: AgentType = AgentType.GENERAL
    capabilities: List[AgentCapability] = msgspec.field(default_factory=list)
    required_permissions: List[str] = msgspec.field(default_factory=list)
    optional_permissions: List[str] = msgspec.field(default_factory=list)
    entry_point: str = ""
    api_version: str = "1.0"
    min_m_version: str = "0.1.0"
    max_tool_calls_per_turn: int = 10
    max_conversation_turns: int = 50
    supports_streaming: bool = False
    supports_tools: bool = True
    system_prompt: str = ""
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class AgentState(msgspec.Struct):
    """Current agent state."""
    status: AgentStatus = AgentStatus.IDLE
    current_task: Optional[str] = None
    conversation_id: Optional[str] = None
    turn_count: int = 0
    tool_calls_this_turn: int = 0
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class AgentConfig(msgspec.Struct, frozen=True):
    """Agent configuration."""
    manifest: AgentManifest
    model_provider: str = "primary"  # "primary" or specific provider ID
    temperature: float = 0.7
    max_tokens: int = 4096
    max_tool_calls_per_turn: int = 10
    max_conversation_turns: int = 50
    enable_streaming: bool = True
    system_prompt_override: Optional[str] = None
    allowed_tools: List[str] = msgspec.field(default_factory=list)  # Empty = all
    blocked_tools: List[str] = msgspec.field(default_factory=list)
    memory_tiers: List[str] = msgspec.field(default_factory=lambda: ["stm", "episodic", "semantic"])
    system_prompt_template: str = "You are M, a helpful AI assistant. You have access to tools and memory. Use them when appropriate."
    tool_call_timeout_seconds: int = 30
    max_tool_retries: int = 2
    system_prompt: str = ""


class AgentMessage(msgspec.Struct, frozen=True):
    """Message between agents or user and agent."""
    id: str
    role: str  # "user", "assistant", "system", "tool"
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = msgspec.field(default_factory=list)
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    timestamp: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))


class AgentRequest(msgspec.Struct, frozen=True):
    """Request to agent."""
    message: str
    conversation_id: Optional[str] = None
    conversation_history: List["AgentMessage"] = msgspec.field(default_factory=list)
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    stream: bool = False
    allowed_tools: List[str] = msgspec.field(default_factory=list)  # Empty = all allowed
    blocked_tools: List[str] = msgspec.field(default_factory=list)
    max_tool_calls: Optional[int] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class AgentResponse(msgspec.Struct, frozen=True):
    """Response from agent."""
    content: str
    tool_calls: List[Dict[str, Any]] = msgspec.field(default_factory=list)
    finish_reason: str = "stop"
    usage: Optional[Dict[str, int]] = None
    conversation_id: Optional[str] = None
    turn_count: int = 0
    tool_calls_made: int = 0
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


class ToolCallRequest(msgspec.Struct, frozen=True):
    """Model-requested tool call."""
    id: str
    name: str
    arguments: Dict[str, Any]
    correlation_id: Optional[str] = None


class ToolCallResult(msgspec.Struct, frozen=True):
    """Tool execution result for model."""
    tool_call_id: str
    name: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_time_ms: float = 0.0


class AgentEvent(msgspec.Struct, frozen=True):
    """Agent lifecycle event."""
    type: str  # "started", "turn_started", "turn_completed", "tool_called", "tool_completed", "completed", "error"
    agent_id: str
    conversation_id: Optional[str] = None
    turn: int = 0
    data: Dict[str, Any] = msgspec.field(default_factory=dict)
    timestamp: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    correlation_id: Optional[str] = None


# Re-export for convenience
__all__ = [
    "AgentStatus",
    "AgentType",
    "AgentCapability",
    "AgentManifest",
    "AgentState",
    "AgentConfig",
    "AgentMessage",
    "AgentRequest",
    "AgentResponse",
    "ToolCallRequest",
    "ToolCallResult",
    "AgentEvent",
    "AgentConfig",
    "AgentManifest",
]