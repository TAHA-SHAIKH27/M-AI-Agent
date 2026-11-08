"""Core package for M AI Assistant."""
from .types import *
from .events import EventBus, Event, event_bus
from .registry import Registry
from .lifecycle import LifecycleManager, LifecyclePhase, lifecycle
from .exceptions import (
    MError, ConfigurationError, ProviderError, PermissionError, ToolExecutionError,
    ValidationError,
)

__all__ = [
    # Types
    "MessageRole", "FinishReason", "ProviderType", "CapabilityCategory",
    "ChatMessage", "ToolFunction", "ToolSchema", "ModelCapability", "ModelInfo",
    "CompletionRequest", "CompletionUsage", "CompletionResponse", "StreamChunk",
    "EmbeddingRequest", "EmbeddingResponse", "ProviderHealth",
    "AgentStatus", "AgentManifest", "AgentMessage",
    "ToolResult", "ToolCall",
    "MemoryTier", "MemoryEntry", "MemorySearchQuery", "MemorySearchResult",
    "ConsentScope", "ConsentRecord",
    "AudioFormat", "STTResult", "TTSRequest",
    "CallDirection", "CallStatus", "CallRecord",
    "HealthStatus", "to_builtins",
    # Core
    "EventBus", "Event", "event_bus", "Registry", "LifecycleManager", "LifecyclePhase", "lifecycle",
    # Exceptions
    "MError", "ConfigurationError", "ProviderError", "PermissionError", "ToolExecutionError",
    "ValidationError",
]