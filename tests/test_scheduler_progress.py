"""Ensure every scheduler strategy reports subtask progress."""
import asyncio

import pytest

from m_core.orchestration.scheduler import ScheduledTask, TaskScheduler
from m_core.orchestration.schemas import SubtaskExecutionRecord, SubtaskExecutionStatus
from m_core.planning.schemas import RoutingDecision, RoutingTargetType, Subtask


def _scheduled(subtask_id: str) -> ScheduledTask:
    subtask = Subtask(id=subtask_id, name=subtask_id, description="test step")
    decision = RoutingDecision(
        subtask_id=subtask_id,
        target_type=RoutingTargetType.AGENT,
        target_id="test-agent",
    )
    record = SubtaskExecutionRecord(subtask_id=subtask_id, task_id="task-progress")
    return ScheduledTask(subtask=subtask, routing_decision=decision, record=record)


@pytest.mark.asyncio
async def test_sequential_scheduler_reports_each_completed_subtask(monkeypatch):
    scheduler = TaskScheduler()
    scheduled = {key: _scheduled(key) for key in ("one", "two")}
    reported = []

    async def fake_execute(item, task_context, correlation_id, progress_callback):
        item.record.status = SubtaskExecutionStatus.COMPLETED
        return {"success": True, "result": item.subtask.id}

    async def on_progress(subtask_id, record):
        reported.append((subtask_id, record.status))

    monkeypatch.setattr(scheduler, "_execute_subtask", fake_execute)
    await scheduler._execute_sequential(
        scheduled, None, None, "correlation", on_progress, None
    )

    assert [item[0] for item in reported] == ["one", "two"]
    assert all(status is SubtaskExecutionStatus.COMPLETED for _, status in reported)


@pytest.mark.asyncio
async def test_parallel_scheduler_reports_each_completed_subtask(monkeypatch):
    scheduler = TaskScheduler()
    scheduler._semaphore = asyncio.Semaphore(2)
    scheduled = {key: _scheduled(key) for key in ("one", "two")}
    reported = []

    async def fake_execute(item, task_context, correlation_id, progress_callback):
        item.record.status = SubtaskExecutionStatus.COMPLETED
        return {"success": True, "result": item.subtask.id}

    async def on_progress(subtask_id, record):
        reported.append((subtask_id, record.status))

    monkeypatch.setattr(scheduler, "_execute_subtask", fake_execute)
    await scheduler._execute_parallel(
        scheduled, None, None, "correlation", on_progress, None
    )

    assert {item[0] for item in reported} == {"one", "two"}
    assert all(status is SubtaskExecutionStatus.COMPLETED for _, status in reported)



@pytest.mark.asyncio
async def test_dependency_scheduler_fails_missing_dependencies_instead_of_spinning():
    scheduler = TaskScheduler()
    subtask = Subtask(
        id="blocked",
        name="blocked",
        description="has an unavailable prerequisite",
        dependencies=["missing-prerequisite"],
    )
    decision = RoutingDecision(
        subtask_id=subtask.id,
        target_type=RoutingTargetType.AGENT,
        target_id="test-agent",
    )
    record = SubtaskExecutionRecord(subtask_id=subtask.id, task_id="blocked-task")
    scheduled = {subtask.id: ScheduledTask(subtask, decision, record)}

    results = await scheduler._execute_dependency_aware(
        scheduled, None, None, "blocked-correlation", None, None
    )

    assert results["blocked"]["success"] is False
    assert "dependencies" in results["blocked"]["error"]
    assert record.status is SubtaskExecutionStatus.FAILED
