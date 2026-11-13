"""Tests for task event replay after a WebSocket/API reconnect."""
import uuid

import pytest

from m_core.api.rest.dependencies import APIClientInfo
from m_core.api.rest.routes.tasks import get_task_events
from m_core.core.events import event_bus
from m_core.orchestration.schemas import TaskExecutionRecord, TaskExecutionStatus
from m_core.orchestration.state_manager import state_manager


@pytest.mark.asyncio
async def test_task_event_replay_returns_task_events_and_supports_cursor():
    task_id = f"event-replay-{uuid.uuid4().hex}"
    await state_manager.save_execution_record(TaskExecutionRecord(
        task_id=task_id,
        execution_plan_id="event-replay-plan",
        status=TaskExecutionStatus.COMPLETED,
    ))
    await event_bus.publish_sync(
        "task.created", "test", {"task_id": task_id, "message": "created"}
    )
    first = event_bus.get_history(limit=1)[-1]
    await event_bus.publish_sync(
        "task.completed", "test", {"task_id": task_id, "status": "completed"}
    )

    client = APIClientInfo(host="127.0.0.1")
    replay = await get_task_events(
        task_id, after_event_id=None, client=client, _=None
    )
    assert [event.event_type for event in replay.events] == [
        "task.created",
        "task.completed",
    ]
    assert replay.latest_event_id == replay.events[-1].event_id

    resumed = await get_task_events(
        task_id, after_event_id=first.event_id, client=client, _=None
    )
    assert [event.event_type for event in resumed.events] == ["task.completed"]
