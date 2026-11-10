"""Tool registry for M AI Assistant."""
import asyncio
import uuid
import structlog
from typing import Dict, List, Optional, Type, Any, Callable
from dataclasses import dataclass, field

from m_core.common_schemas import ToolManifest, ToolInvocation, ToolResult, ToolExecutionContext, ToolRiskLevel
from .base import BaseTool
from ..core.registry import Registry
from ..core.exceptions import ToolNotFoundError, ToolExecutionError
from ..security import consent_manager, audit_logger
from ..memory import memory_manager
from ..model_providers import provider_factory
from ..utils.logging import get_logger

logger = get_logger(__name__)


def _manifest_to_obj(manifest: Any) -> Any:
    """Convert dict manifest to object with attribute access."""
    if isinstance(manifest, dict):
        class ManifestObj:
            def __init__(self, data):
                for k, v in data.items():
                    setattr(self, k, v)
        return ManifestObj(manifest)
    return manifest


@dataclass
class ToolRegistryEntry:
    """Registry entry for a tool."""
    manifest: Any
    tool_class: Type
    instance: Optional[object] = None
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


class ToolRegistry(Registry):
    """Registry for tools with permission checking and audit logging."""
    
    def __init__(self):
        super().__init__("tools")
        self._instances: Dict[str, BaseTool] = {}
        self._lock = asyncio.Lock()
    
    def register(
        self,
        name: str,
        tool_class: Type,
        manifest: Any,
        enabled: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Register a tool class with its manifest."""
        if name in self._entries and not overwrite:
            raise ValueError(f"Tool '{name}' already registered. Use overwrite=True to replace.")
        
        # Convert manifest to object with attribute access
        manifest_obj = _manifest_to_obj(manifest)
        
        # Create entry
        entry = ToolRegistryEntry(
            manifest=manifest_obj,
            tool_class=tool_class,
        )
        
        # Store in registry
        super().register(name, component=manifest_obj, metadata={
            "tool_class": tool_class,
            "manifest": manifest_obj,
            "enabled": True,
        }, overwrite=overwrite)
        
        logger.info(f"Registered tool: {name} (risk: {manifest_obj.risk_level})")
    
    def register_tool_class(self, tool_class: Type, overwrite: bool = False) -> None:
        """Register a tool by its class (extracts manifest from class)."""
        if not issubclass(tool_class, BaseTool):
            raise ValueError("Tool class must inherit from BaseTool")
        
        # Create instance to get manifest
        instance = tool_class()
        manifest = instance.get_manifest()
        
        # Convert manifest to object with attribute access
        manifest_obj = _manifest_to_obj(manifest)
        
        self.register(
            name=manifest_obj.name,
            tool_class=tool_class,
            manifest=manifest_obj,
            overwrite=overwrite,
        )
        
        # Cache instance
        self._instances[manifest_obj.name] = instance
    
    def get_tool(self, name: str) -> BaseTool:
        """Get a tool instance by name."""
        if name in self._instances:
            return self._instances[name]
        
        # Try to get from registry
        entry = self._entries.get(name)
        if entry and entry.enabled:
            manifest = entry.metadata.get("manifest")
            tool_class = entry.metadata.get("tool_class")
            
            if tool_class:
                instance = tool_class()
                self._instances[name] = instance
                return instance
        
        raise ToolNotFoundError(f"Tool '{name}' not found")
    
    def get_manifest(self, name: str) -> Optional[Any]:
        """Get tool manifest by name."""
        if name in self._instances:
            return self._instances[name].get_manifest()
        
        entry = self._entries.get(name)
        if entry:
            return entry.metadata.get("manifest")
        return None
    
    def list_tools(self, enabled_only: bool = True, risk_level: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all registered tools."""
        tools = []
        for name, entry in self._entries.items():
            if enabled_only and not entry.enabled:
                continue
            
            manifest = entry.metadata.get("manifest")
            if manifest:
                tool_info = {
                    "name": manifest.name,
                    "display_name": manifest.display_name,
                    "description": manifest.description,
                    "category": manifest.category,
                    "risk_level": manifest.risk_level,
                    "required_permissions": manifest.required_permissions,
                    "optional_permissions": manifest.optional_permissions,
                    "requires_approval": manifest.requires_approval,
                    "risk_level": manifest.risk_level,
                    "enabled": entry.enabled,
                }
                
                if risk_level and manifest.risk_level != risk_level:
                    continue
                
                tools.append(tool_info)
        
        return sorted(tools, key=lambda x: x["name"])
    
    async def execute(
        self,
        invocation: Any,
        context: Optional[Any] = None,
    ) -> Any:
        """Execute a tool with full lifecycle management."""
        # Get tool
        tool = self.get_tool(invocation.tool_name)
        
        # Create execution context
        if context is None:
            from ..memory import memory_manager
            from ..model_providers import provider_factory
            from ..security import consent_manager, audit_logger
            
            context = ToolExecutionContext(
                tool_name=invocation.tool_name,
                arguments=invocation.arguments,
                correlation_id=invocation.correlation_id or str(uuid.uuid4()),
                timeout_seconds=invocation.timeout_seconds or 30,
                memory_manager=memory_manager,
                model_provider_factory=provider_factory,
                consent_manager=consent_manager,
                audit_logger=audit_logger,
            )
        
        # Get tool instance
        tool = self.get_tool(invocation.tool_name)
        
        # Execute
        result = await tool.execute(context)
        return result
    
    async def invoke(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        correlation_id: Optional[str] = None,
        timeout_seconds: int = 30,
        approval_token: Optional[str] = None,
    ) -> Any:
        """Convenience method to invoke a tool."""
        from m_core.common_schemas import ToolInvocation
        
        invocation = ToolInvocation(
            tool_name=tool_name,
            arguments=arguments,
            correlation_id=correlation_id,
            timeout_seconds=timeout_seconds,
        )
        
        return await self.execute(invocation)
    
    def enable(self, name: str) -> bool:
        """Enable a tool."""
        return self.set_enabled(name, True)
    
    def disable(self, name: str) -> bool:
        """Disable a tool."""
        return self.set_enabled(name, False)
    
    def get_tool_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Get detailed tool information."""
        entry = self._entries.get(name)
        if not entry:
            return None
        
        manifest = entry.metadata.get("manifest")
        if not manifest:
            return None
        
        return {
            "manifest": manifest,
            "enabled": entry.enabled,
            "metadata": entry.metadata,
        }


# Global tool registry instance
tool_registry = ToolRegistry()