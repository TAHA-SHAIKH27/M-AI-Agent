"""Agent registry for M AI Assistant."""
import asyncio
import structlog
from typing import Dict, List, Optional, Type, Any, Callable
from dataclasses import dataclass, field

from .schemas import AgentManifest, AgentConfig, AgentState, AgentStatus
from ..core.registry import Registry
from ..core.exceptions import AgentNotFoundError, AgentExecutionError
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
class AgentRegistryEntry:
    """Registry entry for an agent."""
    manifest: Any
    config: Any
    instance: Optional[object] = None
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentRegistry(Registry):
    """Registry for agents with lifecycle management."""
    
    def __init__(self):
        super().__init__("agents")
        self._instances: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
    
    def register(
        self,
        name: str,
        agent_class: Type,
        manifest: Any,
        config: Optional[Any] = None,
        enabled: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Register an agent class with its manifest and config."""
        if name in self._entries and not overwrite:
            raise ValueError(f"Agent '{name}' already registered. Use overwrite=True to replace.")
        
        # Create entry
        entry = AgentRegistryEntry(
            manifest=manifest,
            config=config,
            enabled=enabled,
        )
        
        # Store in registry
        super().register(name, component=manifest, metadata={
            "agent_class": agent_class,
            "manifest": manifest,
            "config": config,
            "enabled": enabled,
        }, overwrite=overwrite)
        
        logger.info(f"Registered agent: {name} (type: {manifest.type})")
    
    def register_agent_class(self, agent_class: Type, config: Optional[Any] = None, overwrite: bool = False) -> None:
        """Register an agent by its class (extracts manifest from class)."""
        if not hasattr(agent_class, 'manifest'):
            raise ValueError("Agent class must have a 'manifest' class attribute")
        
        # Create instance to get manifest
        instance = agent_class()
        manifest = instance.manifest
        
        # Convert dict manifest to object with attribute access
        manifest = _manifest_to_obj(manifest)
        
        self.register(
            name=manifest.id,
            agent_class=agent_class,
            manifest=manifest,
            config=config,
            overwrite=overwrite,
        )
        
        # Cache instance
        self._instances[manifest.id] = instance
    
    def get_agent(self, name: str):
        """Get an agent instance by name."""
        if name in self._instances:
            return self._instances[name]
        
        # Try to get from registry
        entry = self._entries.get(name)
        if entry and entry.enabled:
            agent_class = entry.metadata.get("agent_class")
            config = entry.metadata.get("config")
            
            if agent_class:
                instance = agent_class(config=entry.metadata.get("config"))
                self._instances[name] = instance
                return instance
        
        raise AgentNotFoundError(f"Agent '{name}' not found")
    
    def get_manifest(self, name: str):
        """Get agent manifest by name."""
        if name in self._instances:
            return self._instances[name].manifest
        
        entry = self._entries.get(name)
        if entry:
            return entry.metadata.get("manifest")
        return None
    
    def list_agents(self, enabled_only: bool = True, agent_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all registered agents."""
        agents = []
        for name, entry in self._entries.items():
            if enabled_only and not entry.enabled:
                continue
            
            manifest = entry.metadata.get("manifest")
            if manifest:
                # Handle both enum and string values
                agent_type_val = manifest.type
                if hasattr(agent_type_val, 'value'):
                    agent_type_val = agent_type_val.value
                
                caps = manifest.capabilities
                if caps and hasattr(caps[0], 'value'):
                    caps = [c.value for c in caps]
                
                agent_info = {
                    "id": manifest.id,
                    "name": manifest.name,
                    "type": agent_type_val,
                    "description": manifest.description,
                    "capabilities": caps,
                    "enabled": entry.enabled,
                }
                
                if agent_type and agent_type_val != agent_type:
                    continue
                
                agents.append(agent_info)
        
        return sorted(agents, key=lambda x: x["name"])
    
    def get_agent_state(self, name: str):
        """Get agent state."""
        if name in self._instances:
            return self._instances[name].state
        return None
    
    async def start_agent(self, name: str) -> bool:
        """Start an agent by name."""
        agent = self.get_agent(name)
        if agent:
            await agent.start()
            return True
        return False
    
    async def stop_agent(self, name: str) -> bool:
        """Stop an agent by name."""
        agent = self.get_agent(name)
        if agent:
            await agent.stop()
            return True
        return False
    
    async def execute_agent(
        self,
        agent_name: str,
        request: Any,
    ):
        """Execute a request on a specific agent."""
        agent = self.get_agent(agent_name)
        if not agent:
            raise AgentNotFoundError(f"Agent '{agent_name}' not found")
        return await agent.process_request(request)
    
    def enable(self, name: str) -> bool:
        """Enable an agent."""
        return self.set_enabled(name, True)
    
    def disable(self, name: str) -> bool:
        """Disable an agent."""
        return self.set_enabled(name, False)
    
    def get_agent_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Get detailed agent information."""
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


# Global agent registry instance
agent_registry = AgentRegistry()