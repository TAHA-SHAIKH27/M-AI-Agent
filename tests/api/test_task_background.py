"""Tests that task creation is asynchronous and queued cancellation is safe."""
import pytest
from fastapi import BackgroundTasks

from m_core.api.rest.dependencies import APIClientInfo
from m_core.api.rest.routes.chat import create_task
from m_core.api.rest.schemas import ChatRequest, TaskStatus
from m_core.orchestration import orchestrator
from m_core.orchestration.schemas import TaskExecutionStatus
from m_core.orchestration.state_manager import state_manager


@pytest.mark.asyncio
async def test_create_task_returns_queued_record_and_schedules_background_execution():
    background = BackgroundTasks()
    response = await create_task(
        ChatRequest(message="Get system information"),
        background,
        client=APIClientInfo(host="127.0.0.1"),
        _=None,
        conversation_id="background-task-test",
    )

    assert response.status == TaskStatus.QUEUED
    assert response.task_id
    assert len(background.tasks) == 1
    scheduled = background.tasks[0]
    assert scheduled.func == orchestrator.execute_task
    assert scheduled.kwargs["task_id"] == response.task_id

    # Cancellation before the worker starts must persist and prevent execution.
    assert await orchestrator.cancel_task(response.task_id) is True
    record = await state_manager.get_execution_record(response.task_id)
    assert record is not None
    assert record.status is TaskExecutionStatus.CANCELLED
