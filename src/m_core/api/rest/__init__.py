"""REST API package for M AI Assistant."""
from .server import app
from .routes import (
    health_router, chat_router, memory_router, tools_router, settings_router,
)
from .middleware import (
    RequestLoggingMiddleware, ErrorHandlingMiddleware, SecurityMiddleware,
)
from .dependencies import (
    get_client_info, validate_localhost, get_orchestrator,
    get_memory_manager, get_tool_registry, get_agent_registry,
    get_provider_factory, get_conversation_id, get_task_id,
    APIClientInfo,
)
from .schemas import (
    HealthStatus, ChatRole, TaskStatus, ApprovalStatus,
    WSMessageType, ChatMessage, ChatRequest, ChatResponse,
    TaskInfo, TaskCancelRequest, MemorySearchRequest, MemorySearchResponse,
    MemoryEntryResponse, ToolInfo, ToolsResponse, SafeSettingsResponse,
    HealthResponse, ErrorResponse, WSEnvelope, WSChatPayload,
    WSChatResponsePayload, WSTaskProgressPayload, WSTaskCompletedPayload,
    WSApprovalRequiredPayload, WSApprovePayload, WSToolPayload,
)

__all__ = [
    "app",
    # Routers
    "health_router", "chat_router", "memory_router", "tools_router", "settings_router",
    # Middleware
    "RequestLoggingMiddleware", "ErrorHandlingMiddleware", "SecurityMiddleware",
    # Dependencies
    "get_client_info", "validate_localhost", "get_orchestrator",
    "get_memory_manager", "get_tool_registry", "get_agent_registry",
    "get_provider_factory", "get_conversation_id", "get_task_id",
    "APIClientInfo",
    # Schemas
    "HealthStatus", "ChatRole", "TaskStatus", "ApprovalStatus",
    "WSMessageType", "ChatMessage", "ChatRequest", "ChatResponse",
    "TaskInfo", "TaskCancelRequest", "MemorySearchRequest", "MemorySearchResponse",
    "MemoryEntryResponse", "ToolInfo", "ToolsResponse", "SafeSettingsResponse",
    "HealthResponse", "ErrorResponse", "WSEnvelope", "WSChatPayload",
    "WSChatResponsePayload", "WSTaskProgressPayload", "WSTaskCompletedPayload",
    "WSApprovalRequiredPayload", "WSApprovePayload", "WSToolPayload",
]