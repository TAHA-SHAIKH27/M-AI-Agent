"""Tests for pausing and resuming orchestration safely."""
import asyncio

import pytest

from m_core.orchestration import orchestrator
from m_core.orchestration.orchestrator import OrchestrationContext
from m_core.orchestration.scheduler import ScheduledTask, TaskScheduler
from m_core.orchestration.schemas import (
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
    TaskExecutionStatus,
)
from m_core.planning.schemas import (
    ExecutionPlan,
    RoutingDecision,
    RoutingPlan,
    RoutingTargetType,
    Subtask,
    TaskAnalysis,
    TaskContext,
    TaskIntent,
)


@pytest.mark.asyncio
async def test_orchestrator_pause_and_resume_updates_state_and_gate():
    await orchestrator.initialize()
    task_id = "pause-resume-regression"
    record = TaskExecutionRecord(
        task_id=task_id,
        execution_plan_id="pause-resume-plan",
        status=TaskExecutionStatus.RUNNING,
    )
    plan = ExecutionPlan(
        task_id="pause-resume-plan",
        original_request="pause test",
        analysis=TaskAnalysis(intent=TaskIntent(primary="pause test")),
    )
    gate = asyncio.Event()
    gate.set()
    context = OrchestrationContext(
        task_id=task_id,
        execution_plan=plan,
        routing_plan=RoutingPlan(execution_plan_id=plan.task_id),
        task_context=TaskContext(task_id=task_id),
        execution_record=record,
        subtask_records={},
        checkpoints=[],
        pause_event=gate,
    )
    orchestrator._active_tasks[task_id] = context
    try:
        assert await orchestrator.pause_task(task_id) is True
        assert record.status is TaskExecutionStatus.PAUSED
        assert gate.is_set() is False
        checkpoint = context.checkpoints[-1]

        resumed_record = await orchestrator.resume_from_checkpoint(checkpoint.checkpoint_id)
        assert resumed_record is record
        assert record.status is TaskExecutionStatus.RUNNING
        assert gate.is_set() is True

        assert await orchestrator.pause_task(task_id) is True
        assert await orchestrator.resume_task(task_id) is True
        assert record.status is TaskExecutionStatus.RUNNING
        assert gate.is_set() is True
    finally:
        orchestrator._active_tasks.pop(task_id, None)


@pytest.mark.asyncio
async def test_scheduler_waits_at_subtask_boundary_while_paused(monkeypatch):
    scheduler = TaskScheduler()
    subtask = Subtask(id="paused-step", name="paused step", description="test")
    decision = RoutingDecision(
        subtask_id=subtask.id,
        target_type=RoutingTargetType.AGENT,
        target_id="test-agent",
    )
    record = SubtaskExecutionRecord(
        subtask_id=subtask.id,
        task_id="paused-task",
        status=SubtaskExecutionStatus.PENDING,
    )
    scheduled = {subtask.id: ScheduledTask(subtask, decision, record)}
    pause_event = asyncio.Event()
    started = asyncio.Event()

    async def fake_execute(item, task_context, correlation_id, progress_callback):
        started.set()
        item.record.status = SubtaskExecutionStatus.COMPLETED
        return {"success": True}

    monkeypatch.setattr(scheduler, "_execute_subtask", fake_execute)
    execution = asyncio.create_task(
        scheduler._execute_sequential(
            scheduled, None, None, "pause-correlation", None, None, pause_event
        )
    )
    await asyncio.sleep(0)
    assert not started.is_set()
    pause_event.set()
    await execution
    assert started.is_set()



@pytest.mark.asyncio
async def test_cancel_task_wakes_pause_and_approval_gates():
    await orchestrator.initialize()
    task_id = "cancel-gate-regression"
    record = TaskExecutionRecord(
        task_id=task_id,
        execution_plan_id="cancel-gate-plan",
        status=TaskExecutionStatus.RUNNING,
    )
    plan = ExecutionPlan(
        task_id="cancel-gate-plan",
        original_request="cancel test",
        analysis=TaskAnalysis(intent=TaskIntent(primary="cancel test")),
    )
    pause_gate = asyncio.Event()
    approval_gate = asyncio.Event()
    pause_gate.clear()
    context = OrchestrationContext(
        task_id=task_id,
        execution_plan=plan,
        routing_plan=RoutingPlan(execution_plan_id=plan.task_id),
        task_context=TaskContext(task_id=task_id),
        execution_record=record,
        subtask_records={},
        checkpoints=[],
        pause_event=pause_gate,
        approval_event=approval_gate,
    )
    context.pause_event.clear()
    orchestrator._active_tasks[task_id] = context
    try:
        assert await orchestrator.cancel_task(task_id) is True
        assert context.cancelled is True
        assert record.status is TaskExecutionStatus.CANCELLED
        assert pause_gate.is_set() is True
        assert approval_gate.is_set() is True
    finally:
        orchestrator._active_tasks.pop(task_id, None)
