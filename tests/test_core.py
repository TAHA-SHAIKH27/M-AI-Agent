"""Tests for core module."""
import pytest
import asyncio
from m_core.core import (
    EventBus, Event, Registry, LifecycleManager, LifecyclePhase,
    MError, ConfigurationError, ProviderError, PermissionError, ToolExecutionError,
    ValidationError,
)


class TestEventBus:
    """Test event bus."""
    
    @pytest.fixture
    def event_bus(self):
        return EventBus()
    
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self, event_bus):
        received = []
        
        async def handler(event):
            received.append(event)
        
        await event_bus.subscribe("test.event", handler)
        await event_bus.publish(Event(type="test.event", source="test", payload={"data": "value"}))
        
        assert len(received) == 1
        assert received[0].type == "test.event"
        assert received[0].payload["data"] == "value"
    
    @pytest.mark.asyncio
    async def test_wildcard_subscription(self, event_bus):
        received = []
        
        async def handler(event):
            received.append(event)
        
        await event_bus.subscribe_all(handler)
        await event_bus.publish(Event(type="any.event", source="test"))
        
        assert len(received) == 1
    
    @pytest.mark.asyncio
    async def test_unsubscribe(self, event_bus):
        received = []
        
        async def handler(event):
            received.append(event)
        
        await event_bus.subscribe("test.event", handler)
        await event_bus.unsubscribe("test.event", handler)
        await event_bus.publish(Event(type="test.event", source="test"))
        
        assert len(received) == 0
    
    @pytest.mark.asyncio
    async def test_history(self, event_bus):
        await event_bus.publish(Event(type="test.event", source="test", payload={"i": 1}))
        await event_bus.publish(Event(type="test.event", source="test", payload={"i": 2}))
        
        history = event_bus.get_history("test.event", limit=10)
        assert len(history) == 2
        assert history[0].payload["i"] == 1
        assert history[1].payload["i"] == 2


class TestRegistry:
    """Test component registry."""
    
    def test_register_and_get(self):
        registry = Registry("test")
        registry.register("component1", component="value1")
        
        assert registry.get("component1") == "value1"
        assert registry.list() == ["component1"]
    
    def test_register_factory(self):
        registry = Registry("test")
        registry.register("factory1", factory=lambda: "created")
        
        assert registry.get("factory1") == "created"
        # Second call should return cached
        assert registry.get("factory1") == "created"
    
    def test_overwrite(self):
        registry = Registry("test")
        registry.register("comp", component="v1")
        registry.register("comp", component="v2", overwrite=True)
        
        assert registry.get("comp") == "v2"
    
    def test_unregister(self):
        registry = Registry("test")
        registry.register("comp", component="value")
        registry.unregister("comp")
        
        assert "comp" not in registry.list()
    
    def test_enabled_disabled(self):
        registry = Registry("test")
        registry.register("comp", component="value", enabled=False)
        
        with pytest.raises(ValueError):
            registry.get("comp")
        
        registry.set_enabled("comp", True)
        assert registry.get("comp") == "value"
    
    def test_metadata(self):
        registry = Registry("test")
        registry.register("comp", component="value", metadata={"version": "1.0"})
        
        assert registry.get_metadata("comp") == {"version": "1.0"}


class TestLifecycleManager:
    """Test lifecycle management."""
    
    @pytest.mark.asyncio
    async def test_startup_shutdown(self):
        lifecycle = LifecycleManager()
        startup_called = False
        shutdown_called = False
        
        async def on_startup():
            nonlocal startup_called
            startup_called = True
        
        async def on_shutdown():
            nonlocal shutdown_called
            shutdown_called = True
        
        lifecycle.add_startup_hook("test", on_startup)
        lifecycle.add_shutdown_hook("test", on_shutdown)
        
        await lifecycle.startup()
        assert startup_called is True
        assert lifecycle.phase == LifecyclePhase.STARTED
        
        await lifecycle.shutdown()
        assert shutdown_called is True
        assert lifecycle.phase == LifecyclePhase.STOPPED
    
    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        lifecycle = LifecycleManager()
        order = []
        
        async def low_priority():
            order.append("low")
        
        async def high_priority():
            order.append("high")
        
        lifecycle.add_startup_hook("low", low_priority, priority=0)
        lifecycle.add_startup_hook("high", high_priority, priority=10)
        
        await lifecycle.startup()
        assert order == ["high", "low"]
    
    @pytest.mark.asyncio
    async def test_lifespan_context(self):
        lifecycle = LifecycleManager()
        startup_called = False
        shutdown_called = False
        
        async def on_startup():
            nonlocal startup_called
            startup_called = True
        
        async def on_shutdown():
            nonlocal shutdown_called
            shutdown_called = True
        
        lifecycle.add_startup_hook("test", on_startup)
        lifecycle.add_shutdown_hook("test", on_shutdown)
        
        async with lifecycle.lifespan():
            assert startup_called is True
            assert lifecycle.phase == LifecyclePhase.STARTED
        
        assert shutdown_called is True
        assert lifecycle.phase == LifecyclePhase.STOPPED


class TestExceptions:
    """Test custom exceptions."""
    
    def test_m_error(self):
        err = MError("Test error", code="TEST_CODE", details={"key": "value"})
        assert str(err) == "[TEST_CODE] Test error"
        assert err.code == "TEST_CODE"
        assert err.details["key"] == "value"
    
    def test_provider_error(self):
        err = ProviderError("Provider failed", provider="nvidia")
        assert err.provider == "nvidia"
        assert err.details["provider"] == "nvidia"
    
    def test_permission_error(self):
        err = PermissionError("Access denied", capability="file:write")
        assert err.capability == "file:write"
        assert err.details["capability"] == "file:write"
    
    def test_tool_execution_error(self):
        err = ToolExecutionError("Tool failed", tool="file_ops")
        assert err.tool == "file_ops"
        assert err.details["tool"] == "file_ops"
    
    def test_validation_error(self):
        err = ValidationError("Invalid value", field="temperature", value=5.0)
        assert err.field == "temperature"
        assert err.value == 5.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])