"""Tests for bounded, safety-aware orchestration retries."""
from types import SimpleNamespace

import pytest

from m_core.core.events import event_bus
from m_core.orchestration.scheduler import ScheduledTask, TaskScheduler
from m_core.orchestration.schemas import SubtaskExecutionRecord, SubtaskExecutionStatus
from m_core.planning.schemas import RoutingDecision, RoutingTargetType, Subtask


@pytest.mark.asyncio
async def test_scheduler_retries_transient_agent_failure_and_emits_event(monkeypatch):
    scheduler = TaskScheduler()
    subtask = Subtask(
        id="retry-step",
        name="retry step",
        description="temporary agent failure",
        assigned_agent="test-agent",
        max_retries=1,
        metadata={"retry_safe": True},
    )
    decision = RoutingDecision(
        subtask_id=subtask.id,
        target_type=RoutingTargetType.AGENT,
        target_id="test-agent",
    )
    record = SubtaskExecutionRecord(subtask_id=subtask.id, task_id="retry-task", max_retries=1)
    scheduled = ScheduledTask(subtask=subtask, routing_decision=decision, record=record)
    attempts = 0

    async def flaky_agent(agent_id, item, task_context, correlation_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"success": False, "error": "temporary upstream timeout"}
        return {"success": True, "result": "recovered"}

    monkeypatch.setattr(scheduler, "_execute_agent", flaky_agent)
    result = await scheduler._execute_subtask(
        scheduled, SimpleNamespace(task_id="retry-task"), "retry-correlation", None
    )

    assert result == {"success": True, "result": "recovered"}
    assert attempts == 2
    assert record.retry_count == 1
    assert record.status is SubtaskExecutionStatus.COMPLETED
    retry_events = [
        event for event in event_bus.get_history(event_type="task.retry", limit=100)
        if event.payload.get("task_id") == "retry-task"
        and event.payload.get("subtask_id") == subtask.id
    ]
    assert len(retry_events) == 1


def test_scheduler_does_not_retry_permission_or_validation_failures():
    assert TaskScheduler._is_retryable_error("Consent required: screen:capture") is False
    assert TaskScheduler._is_retryable_error("Invalid argument: path") is False
    assert TaskScheduler._is_retryable_error("temporary network timeout") is True
