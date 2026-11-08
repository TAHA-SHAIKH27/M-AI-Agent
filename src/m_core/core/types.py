"""Core types and schemas using msgspec for fast serialization."""
import msgspec
from typing import Optional, List, Dict, Any, Literal
from enum import Enum
from dataclasses import dataclass
from datetime import datetime


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class ProviderType(str, Enum):
    CLOUD = "cloud"
    LOCAL = "local"
    HYBRID = "hybrid"


class CapabilityCategory(str, Enum):
    MODEL = "model"
    FILE = "file"
    APP = "app"
    SYSTEM = "system"
    WEB = "web"
    VOICE = "voice"
    PHONE = "phone"
    SCREEN = "screen"
    AUTOMATION = "automation"
    MEMORY = "memory"
    PLUGIN = "plugin"


# =============================================================================
# MODEL PROVIDER SCHEMAS
# =============================================================================

class ChatMessage(msgspec.Struct, frozen=True):
    role: MessageRole
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None


class ToolFunction(msgspec.Struct, frozen=True):
    name: str
    description: str
    parameters: Dict[str, Any]


class ToolSchema(msgspec.Struct, frozen=True):
    type: str = "function"
    function: ToolFunction = None


class ModelCapability(msgspec.Struct, frozen=True):
    chat: bool = True
    streaming: bool = False
    function_calling: bool = False
    vision: bool = False
    reasoning: bool = False
    max_context_tokens: int = 4096
    supported_languages: List[str] = msgspec.field(default_factory=lambda: ["en"])


class ModelInfo(msgspec.Struct, frozen=True):
    id: str
    name: str
    provider: str
    capabilities: ModelCapability
    context_window: int


class CompletionRequest(msgspec.Struct, frozen=True):
    model: Optional[str] = None
    messages: List[ChatMessage] = msgspec.field(default_factory=list)
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: Optional[int] = None
    stream: bool = False
    tools: Optional[List[ToolSchema]] = None
    tool_choice: Optional[str] = None
    stop: Optional[List[str]] = None
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0


class CompletionUsage(msgspec.Struct, frozen=True):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class CompletionResponse(msgspec.Struct, frozen=True):
    id: str
    model: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[FinishReason] = None
    usage: Optional[CompletionUsage] = None
    latency_ms: float = 0.0


class StreamChunk(msgspec.Struct, frozen=True):
    id: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[FinishReason] = None
    is_final: bool = False


class EmbeddingRequest(msgspec.Struct, frozen=True):
    model: Optional[str] = None
    input: List[str] = msgspec.field(default_factory=list)
    encoding_format: str = "float"


class EmbeddingResponse(msgspec.Struct, frozen=True):
    object: str = "list"
    data: List[Dict[str, Any]] = msgspec.field(default_factory=list)
    model: str = ""
    usage: CompletionUsage = None


class ProviderHealth(msgspec.Struct, frozen=True):
    healthy: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    model_count: int = 0


# =============================================================================
# AGENT SCHEMAS
# =============================================================================

class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    ERROR = "error"
    STOPPED = "stopped"


class AgentManifest(msgspec.Struct, frozen=True):
    id: str
    name: str
    version: str
    description: str
    capabilities: List[str] = msgspec.field(default_factory=list)
    required_permissions: List[str] = msgspec.field(default_factory=list)
    optional_permissions: List[str] = msgspec.field(default_factory=list)
    entry_point: str = ""
    api_version: str = "1.0"
    min_m_version: str = "0.1.0"


class AgentMessage(msgspec.Struct, frozen=True):
    id: str
    type: str  # request, response, stream, error, event
    source_agent: str
    target_agent: str
    correlation_id: str
    payload: Dict[str, Any]
    timestamp: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    ttl: Optional[int] = None


# =============================================================================
# TOOL SCHEMAS
# =============================================================================

class ToolResult(msgspec.Struct, frozen=True):
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    execution_time_ms: float = 0.0


class ToolCall(msgspec.Struct, frozen=True):
    id: str
    name: str
    arguments: Dict[str, Any]


# =============================================================================
# MEMORY SCHEMAS
# =============================================================================

class MemoryTier(str, Enum):
    STM = "stm"           # Short-term memory
    EPISODIC = "episodic" # Task/conversation history
    SEMANTIC = "semantic" # Facts, preferences, knowledge
    PROCEDURAL = "procedural" # Skills, workflows


class MemoryEntry(msgspec.Struct, frozen=True):
    id: str
    tier: MemoryTier
    content: str
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    updated_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    expires_at: Optional[int] = None
    tags: List[str] = msgspec.field(default_factory=list)


class MemorySearchQuery(msgspec.Struct, frozen=True):
    query: str
    tiers: List[MemoryTier] = msgspec.field(default_factory=lambda: [MemoryTier.SEMANTIC, MemoryTier.EPISODIC])
    limit: int = 10
    threshold: float = 0.7
    filters: Dict[str, Any] = msgspec.field(default_factory=dict)


class MemorySearchResult(msgspec.Struct, frozen=True):
    entries: List[MemoryEntry] = msgspec.field(default_factory=list)
    total_count: int = 0
    query_time_ms: float = 0.0


# =============================================================================
# PERMISSION SCHEMAS
# =============================================================================

class ConsentScope(str, Enum):
    GLOBAL = "global"
    SESSION = "session"
    PER_REQUEST = "per-request"


class ConsentRecord(msgspec.Struct, frozen=True):
    capability: str
    granted: bool
    granted_at: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    expires_at: Optional[int] = None
    scope: ConsentScope = ConsentScope.GLOBAL
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)


# =============================================================================
# VOICE SCHEMAS
# =============================================================================

class AudioFormat(msgspec.Struct, frozen=True):
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "int16"


class STTResult(msgspec.Struct, frozen=True):
    text: str
    language: str
    language_probability: float
    duration: float
    segments: List[Dict[str, Any]] = msgspec.field(default_factory=list)


class TTSRequest(msgspec.Struct, frozen=True):
    text: str
    voice: Optional[str] = None
    speed: float = 1.0
    format: AudioFormat = msgspec.field(default_factory=AudioFormat)


# =============================================================================
# PHONE SCHEMAS
# =============================================================================

class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BUSY = "busy"
    NO_ANSWER = "no-answer"
    CANCELED = "canceled"


class CallRecord(msgspec.Struct, frozen=True):
    id: str
    direction: CallDirection
    from_number: str
    to_number: str
    status: CallStatus
    started_at: Optional[int] = None
    ended_at: Optional[int] = None
    duration_seconds: int = 0
    recording_url: Optional[str] = None
    transcription: Optional[str] = None


# =============================================================================
# SYSTEM SCHEMAS
# =============================================================================

class HealthStatus(msgspec.Struct, frozen=True):
    status: str
    version: str
    uptime_seconds: float
    components: Dict[str, Dict[str, Any]] = msgspec.field(default_factory=dict)
    timestamp: int = msgspec.field(default_factory=lambda: int(datetime.now().timestamp() * 1000))


def to_builtins(obj: Any) -> Any:
    """Recursively convert msgspec structs, Enums, and objects to JSON-serializable Python builtins."""
    if obj is None:
        return None
    if isinstance(obj, msgspec.Struct):
        return to_builtins(msgspec.to_builtins(obj))
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_builtins(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_builtins(v) for v in obj]
    return obj