"""Regression tests for tool enable/disable enforcement."""
import pytest

from m_core.tools.registry import ToolRegistry
from m_core.core.exceptions import ToolNotFoundError


class FakeTool:
    manifest = {
        "name": "sample",
        "display_name": "Sample",
        "description": "test tool",
        "category": "test",
        "risk_level": "low",
        "required_permissions": [],
        "optional_permissions": [],
        "requires_approval": False,
    }

    def get_manifest(self):
        return self.manifest


def test_disabled_tool_cannot_be_resolved():
    registry = ToolRegistry()
    registry.register("sample", FakeTool, FakeTool.manifest, enabled=False)

    with pytest.raises(ToolNotFoundError):
        registry.get_tool("sample")


def test_disabling_cached_tool_blocks_future_resolution():
    registry = ToolRegistry()
    registry.register("sample", FakeTool, FakeTool.manifest)
    cached = registry.get_tool("sample")

    assert isinstance(cached, FakeTool)
    assert registry.disable("sample") is True
    with pytest.raises(ToolNotFoundError):
        registry.get_tool("sample")


def test_reenabling_cached_tool_restores_resolution():
    registry = ToolRegistry()
    registry.register("sample", FakeTool, FakeTool.manifest)
    cached = registry.get_tool("sample")

    assert registry.disable("sample") is True
    assert registry.enable("sample") is True
    assert registry.get_tool("sample") is cached
