"""Model provider schemas using msgspec for fast serialization."""
import msgspec
from typing import Optional, List, Dict, Any
from enum import Enum
from dataclasses import dataclass


# Common tool schemas (also available in m_core.common_schemas)
class ToolRiskLevel(str, Enum):
    """Risk level of a tool."""
    LOW = "low"           # Read-only, no side effects
    MEDIUM = "medium"     # May modify state, requires consent
    HIGH = "high"         # Significant side effects, explicit approval required
    CRITICAL = "critical" # System-level operations, explicit approval + confirmation


class ToolCategory(str, Enum):
    """Tool category for organization."""
    SYSTEM = "system"
    FILE = "file"
    WEB = "web"
    MEMORY = "memory"
    APP = "app"
    AUTOMATION = "automation"
    CUSTOM = "custom"
    VISUAL = "visual"
    WINDOWS = "windows"
    VOICE = "voice"


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
    FUNCTION_CALL = "function_call"


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


class ChatMessage(msgspec.Struct, frozen=True):
    role: MessageRole
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None


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
    # Provider-specific extra parameters
    extra_body: Optional[Dict[str, Any]] = None


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
    provider: str = ""


class StreamChunk(msgspec.Struct, frozen=True):
    id: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[FinishReason] = None
    is_final: bool = False
    provider: str = ""


class EmbeddingRequest(msgspec.Struct, frozen=True):
    model: Optional[str] = None
    input: List[str] = msgspec.field(default_factory=list)
    encoding_format: str = "float"


class EmbeddingData(msgspec.Struct, frozen=True):
    object: str = "embedding"
    embedding: List[float] = msgspec.field(default_factory=list)
    index: int = 0


class EmbeddingResponse(msgspec.Struct, frozen=True):
    object: str = "list"
    data: List[EmbeddingData] = msgspec.field(default_factory=list)
    model: str = ""
    usage: Optional[CompletionUsage] = None


class ProviderHealth(msgspec.Struct, frozen=True):
    healthy: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    model_count: int = 0