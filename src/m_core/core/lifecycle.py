"""Application lifecycle management."""
import asyncio
import inspect
import logging
from typing import List, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager

from ..utils.logging import get_logger

logger = get_logger(__name__)


class LifecyclePhase(str, Enum):
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class LifecycleHook:
    """Lifecycle hook with priority."""
    name: str
    callback: Callable
    phase: LifecyclePhase
    priority: int = 0  # Higher priority runs first


class LifecycleManager:
    """Manages application startup and shutdown."""
    
    def __init__(self):
        self._hooks: List[LifecycleHook] = []
        self._phase = LifecyclePhase.STOPPED
        self._started = False
        self._shutdown_event = asyncio.Event()
    
    @property
    def phase(self) -> LifecyclePhase:
        return self._phase
    
    @property
    def is_started(self) -> bool:
        return self._started
    
    def add_startup_hook(self, name: str, callback: Callable, priority: int = 0) -> None:
        """Add a startup hook."""
        self._hooks.append(LifecycleHook(
            name=name,
            callback=callback,
            phase=LifecyclePhase.STARTING,
            priority=priority,
        ))
        logger.debug(f"Added startup hook: {name} (priority={priority})")
    
    def add_shutdown_hook(self, name: str, callback: Callable, priority: int = 0) -> None:
        """Add a shutdown hook."""
        self._hooks.append(LifecycleHook(
            name=name,
            callback=callback,
            phase=LifecyclePhase.STOPPING,
            priority=priority,
        ))
        logger.debug(f"Added shutdown hook: {name} (priority={priority})")
    
    async def startup(self) -> None:
        """Run all startup hooks."""
        if self._started:
            logger.warning("Lifecycle already started")
            return
        
        self._phase = LifecyclePhase.STARTING
        logger.info("Starting application lifecycle...")
        
        # Sort by priority (highest first)
        startup_hooks = sorted(
            [h for h in self._hooks if h.phase == LifecyclePhase.STARTING],
            key=lambda h: -h.priority
        )
        
        for hook in startup_hooks:
            try:
                logger.debug(f"Running startup hook: {hook.name}")
                if inspect.iscoroutinefunction(hook.callback):
                    await hook.callback()
                else:
                    hook.callback()
                logger.debug(f"Startup hook completed: {hook.name}")
            except Exception as e:
                self._phase = LifecyclePhase.ERROR
                logger.error(f"Startup hook '{hook.name}' failed: {e}")
                raise
        
        self._phase = LifecyclePhase.STARTED
        self._started = True
        logger.info("Application startup complete")
    
    async def shutdown(self) -> None:
        """Run all shutdown hooks."""
        if not self._started:
            logger.warning("Lifecycle not started")
            return
        
        self._phase = LifecyclePhase.STOPPING
        logger.info("Shutting down application...")
        
        # Sort by priority (highest first)
        shutdown_hooks = sorted(
            [h for h in self._hooks if h.phase == LifecyclePhase.STOPPING],
            key=lambda h: -h.priority
        )
        
        errors = []
        for hook in shutdown_hooks:
            try:
                logger.debug(f"Running shutdown hook: {hook.name}")
                if inspect.iscoroutinefunction(hook.callback):
                    await hook.callback()
                else:
                    hook.callback()
                logger.debug(f"Shutdown hook completed: {hook.name}")
            except Exception as e:
                logger.error(f"Shutdown hook '{hook.name}' failed: {e}")
                errors.append((hook.name, e))
        
        self._phase = LifecyclePhase.STOPPED
        self._started = False
        self._shutdown_event.set()
        
        if errors:
            logger.warning(f"Shutdown completed with {len(errors)} errors")
        else:
            logger.info("Application shutdown complete")
    
    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()
    
    @asynccontextmanager
    async def lifespan(self):
        """Async context manager for lifespan."""
        await self.startup()
        try:
            yield self
        finally:
            await self.shutdown()


# Global lifecycle manager
lifecycle = LifecycleManager()