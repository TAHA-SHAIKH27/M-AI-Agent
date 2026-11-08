"""Event system for inter-component communication."""
import asyncio
import inspect
import logging
from typing import Callable, Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import uuid

from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Event:
    """Event data structure."""
    type: str
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None


class EventBus:
    """Async event bus for publish/subscribe pattern."""
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._wildcard_subscribers: List[Callable] = []
        self._history: List[Event] = []
        self._max_history = 1000
        self._lock = asyncio.Lock()
    
    async def subscribe(self, event_type: str, handler: Callable) -> None:
        """Subscribe to an event type."""
        async with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(handler)
            logger.debug(f"Subscribed {handler.__name__} to {event_type}")
    
    async def subscribe_all(self, handler: Callable) -> None:
        """Subscribe to all events (wildcard)."""
        async with self._lock:
            self._wildcard_subscribers.append(handler)
            logger.debug(f"Subscribed {handler.__name__} to all events")
    
    async def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """Unsubscribe from an event type."""
        async with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(handler)
                    logger.debug(f"Unsubscribed {handler.__name__} from {event_type}")
                except ValueError:
                    pass
    
    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        # Add to history
        async with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)
            
            # Get subscribers
            subscribers = self._subscribers.get(event.type, []).copy()
            wildcard = self._wildcard_subscribers.copy()
        
        # Call subscribers
        all_handlers = subscribers + wildcard
        if not all_handlers:
            return
        
        # Run all handlers concurrently
        tasks = []
        for handler in all_handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    tasks.append(handler(event))
                else:
                    tasks.append(asyncio.to_thread(handler, event))
            except Exception as e:
                logger.error(f"Error creating task for handler {handler.__name__}: {e}")
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"Handler {all_handlers[i].__name__} failed: {result}")
    
    async def publish_sync(self, event_type: str, source: str, payload: Dict[str, Any] = None, correlation_id: str = None) -> None:
        """Convenience method to publish an event."""
        event = Event(
            type=event_type,
            source=source,
            payload=payload or {},
            correlation_id=correlation_id,
        )
        await self.publish(event)
    
    def get_history(self, event_type: Optional[str] = None, limit: int = 100) -> List[Event]:
        """Get event history."""
        history = self._history
        if event_type:
            history = [e for e in history if e.type == event_type]
        return history[-limit:]
    
    def clear_history(self) -> None:
        """Clear event history."""
        self._history.clear()


# Global event bus instance
event_bus = EventBus()