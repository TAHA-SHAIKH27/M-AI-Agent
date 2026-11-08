"""Registry for plugins, tools, agents, and other components."""
import logging
from typing import Dict, List, Optional, Type, TypeVar, Any, Callable
from dataclasses import dataclass, field

from ..utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass
class RegistryEntry:
    """Registry entry with metadata."""
    name: str
    component: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    version: str = "1.0.0"
    enabled: bool = True


class Registry:
    """Generic registry for components."""
    
    def __init__(self, name: str):
        self.name = name
        self._entries: Dict[str, RegistryEntry] = {}
        self._factories: Dict[str, Callable] = {}
    
    def register(
        self,
        name: str,
        component: Any = None,
        *,
        factory: Callable = None,
        metadata: Dict[str, Any] = None,
        version: str = "1.0.0",
        enabled: bool = True,
        overwrite: bool = False,
    ) -> None:
        """Register a component or factory."""
        if name in self._entries and not overwrite:
            raise ValueError(f"{self.name} '{name}' already registered. Use overwrite=True to replace.")
        
        if component is not None:
            self._entries[name] = RegistryEntry(
                name=name,
                component=component,
                metadata=metadata or {},
                version=version,
                enabled=enabled,
            )
            logger.debug(f"Registered {self.name}: {name} (v{version})")
        elif factory is not None:
            self._factories[name] = factory
            logger.debug(f"Registered {self.name} factory: {name}")
        else:
            raise ValueError("Either component or factory must be provided")
    
    def get(self, name: str) -> Any:
        """Get a registered component."""
        if name in self._entries:
            entry = self._entries[name]
            if not entry.enabled:
                raise ValueError(f"{self.name} '{name}' is disabled")
            return entry.component
        
        if name in self._factories:
            component = self._factories[name]()
            self.register(name, component=component)
            return component
        
        raise KeyError(f"{self.name} '{name}' not found")
    
    def get_factory(self, name: str) -> Callable:
        """Get a factory function."""
        if name not in self._factories:
            raise KeyError(f"{self.name} factory '{name}' not found")
        return self._factories[name]
    
    def unregister(self, name: str) -> bool:
        """Unregister a component."""
        if name in self._entries:
            del self._entries[name]
            logger.debug(f"Unregistered {self.name}: {name}")
            return True
        if name in self._factories:
            del self._factories[name]
            logger.debug(f"Unregistered {self.name} factory: {name}")
            return True
        return False
    
    def list(self, enabled_only: bool = True) -> List[str]:
        """List registered names."""
        names = []
        for name, entry in self._entries.items():
            if not enabled_only or entry.enabled:
                names.append(name)
        names.extend(self._factories.keys())
        return sorted(set(names))
    
    def get_metadata(self, name: str) -> Dict[str, Any]:
        """Get metadata for a registered component."""
        if name in self._entries:
            return self._entries[name].metadata
        return {}
    
    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a component."""
        if name in self._entries:
            self._entries[name].enabled = enabled
            logger.debug(f"{'Enabled' if enabled else 'Disabled'} {self.name}: {name}")
            return True
        return False
    
    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()
        self._factories.clear()
        logger.debug(f"Cleared {self.name} registry")


# Global registries
tool_registry = Registry("tools")
agent_registry = Registry("agents")
plugin_registry = Registry("plugins")
model_provider_registry = Registry("model_providers")