"""Tool schemas for M AI Assistant."""
import msgspec
from typing import Optional, List, Dict, Any
from enum import Enum
from ..core.types import ToolSchema as CoreToolSchema, ToolFunction


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


class ToolManifest(msgspec.Struct, frozen=True):
    """Complete tool manifest with all metadata."""
    name: str
    display_name: str
    description: str
    category: ToolCategory
    risk_level: ToolRiskLevel
    input_schema: Dict[str, Any]  # JSON Schema
    output_schema: Dict[str, Any]  # JSON Schema
    required_permissions: List[str] = msgspec.field(default_factory=list)
    optional_permissions: List[str] = msgspec.field(default_factory=list)
    risk_description: str = ""
    requires_approval: bool = False
    timeout_seconds: int = 30
    max_retries: int = 2
    version: str = "1.0.0"
    author: str = "M Core"
    tags: List[str] = msgspec.field(default_factory=list)


class ToolInvocation(msgspec.Struct, frozen=True):
    """A tool invocation request."""
    tool_name: str
    arguments: Dict[str, Any]
    correlation_id: Optional[str] = None
    timeout_seconds: Optional[int] = None
    requires_approval: bool = False
    approval_token: Optional[str] = None


class ToolResult(msgspec.Struct, frozen=True):
    """Tool execution result."""
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: Dict[str, Any] = msgspec.field(default_factory=dict)
    execution_time_ms: float = 0.0
    tool_name: str = ""
    correlation_id: Optional[str] = None


class ToolExecutionContext(msgspec.Struct, frozen=True):
    """Context passed to tool during execution."""
    tool_name: str
    arguments: Dict[str, Any]
    correlation_id: str
    user_id: str = "system"
    permissions_granted: List[str] = msgspec.field(default_factory=list)
    audit_logger: Optional[Any] = None
    consent_manager: Optional[Any] = None
    timeout_seconds: int = 30
    memory_manager: Optional[Any] = None
    model_provider_factory: Optional[Any] = None


# Re-export core types for backward compatibility
ToolSchema = CoreToolSchema
ToolFunction = ToolFunction