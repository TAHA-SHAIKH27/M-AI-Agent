"""Regression tests for the enhanced direct/visual control router."""
import pytest

from m_core.planning.router_enhanced import (
    ControlMethod,
    ControlMethodSelector,
    EnhancedIntelligentRouter,
)
from m_core.planning.schemas import (
    ExecutionPlan,
    Subtask,
    TaskAnalysis,
    TaskIntent,
    TaskType,
)
from m_core.security import ConsentScope, consent_manager


@pytest.mark.asyncio
async def test_selector_reads_tool_registry_manifests_without_attribute_error():
    consent_manager.grant("screen:capture", scope=ConsentScope.GLOBAL, ttl_hours=1)
    selector = ControlMethodSelector()
    await selector.initialize()

    assert selector._check_tool_available("screen_capture") is True


@pytest.mark.asyncio
async def test_enhanced_router_uses_visual_method_and_preserves_assigned_tool():
    consent_manager.grant("screen:capture", scope=ConsentScope.GLOBAL, ttl_hours=1)
    subtask = Subtask(
        id="capture-1",
        name="Capture screenshot",
        description="Capture a screenshot of the current screen",
        task_type=TaskType.TOOL_USE,
        assigned_tool="screen_capture",
    )
    analysis = TaskAnalysis(
        intent=TaskIntent(primary="capture screenshot"),
        task_type=TaskType.TOOL_USE,
        needs_tools=True,
    )
    plan = ExecutionPlan(
        task_id="phase6-router-test",
        original_request="Capture a screenshot",
        analysis=analysis,
        subtasks=[subtask],
    )

    enhanced = EnhancedIntelligentRouter()
    routing = await enhanced.create_routing_plan(plan)

    assert routing.metadata["enhanced_routing"] is True
    assert routing.decisions[0].target_id == "screen_capture"
    assert routing.decisions[0].metadata["control_method"] == ControlMethod.VISUAL.value
