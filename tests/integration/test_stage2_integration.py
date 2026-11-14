"""Stage 2 Mocked REST API, WebSocket & Lifespan Integration Tests.

Covers:
- Mocked REST API integration (tasks listing, stats, get, approve, pause, resume, cancel, checkpoints).
- Mocked WebSocket integration (connection ack, command dispatch, event broadcast).
- Pause, resume, approval, denial, and cancellation workflows across REST and WS.
- Task serialization (execution_record_to_task_info) and status transitions.
- WebSocket reconnect and duplicate-event checks (idempotent register_event_handlers).
- FastAPI lifespan startup and shutdown (clean resource cleanup).
- Background task handling and graceful resource cleanup.
"""
import os
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from m_core.api.rest.schemas import (
    ApprovalStatus,
    TaskApproveRequest,
    TaskBoardStats,
    TaskInfo,
    TaskListResponse,
    TaskStatus,
    WSMessageType,
)
from m_core.api.rest.serializers import execution_record_to_task_info
from m_core.api.rest.server import app, lifespan
from m_core.api.websocket.server import connection_manager, register_event_handlers
from m_core.config import settings
from m_core.core.events import Event, event_bus
from m_core.desktop import desktop_automation
from m_core.model_providers import close_vision_factory, provider_factory
from m_core.orchestration import autonomous_executor, orchestrator, scheduler, state_manager
from m_core.orchestration.schemas import (
    Checkpoint,
    CheckpointType,
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
    TaskExecutionStatus,
)
from m_core.planning.schemas import ExecutionPlan, Subtask
from m_core.security import Capability, consent_manager
from tests.api.mock_provider import MockProvider


@pytest.fixture(autouse=True)
def stage2_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ensure complete isolation for Stage 2 tests: temporary data dir and mocked providers/desktop."""
    isolated_data = tmp_path / "stage2_data"
    isolated_data.mkdir(parents=True, exist_ok=True)
    (isolated_data / "logs").mkdir(exist_ok=True)
    (isolated_data / "orchestration").mkdir(exist_ok=True)

    orig_data_dir = settings.data_dir
    orig_testing = settings.testing
    settings.data_dir = isolated_data
    settings.testing = True

    # Grant required test capabilities
    for cap in [
        Capability.MEMORY_READ,
        Capability.MEMORY_WRITE,
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.FILE_LIST,
        Capability.SYSTEM_INFO,
    ]:
        consent_manager.grant(cap)

    # Use offline MockProvider
    mock = MockProvider(response_content="Mock response for stage 2 integration")
    orig_providers = list(provider_factory._providers)
    provider_factory.replace_providers([mock])

    try:
        yield isolated_data
    finally:
        settings.data_dir = orig_data_dir
        settings.testing = orig_testing
        provider_factory.replace_providers(orig_providers)


@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ==============================================================================
# 1. Mocked REST API Integration & Workflow Tests
# ==============================================================================

class TestRestApiWorkflows:
    """REST API task workflows: pause, resume, approval, denial, cancellation, serialization."""

    @pytest.mark.asyncio
    async def test_task_serialization_and_status_transitions(self):
        """Verify TaskInfo serialization from TaskExecutionRecord with all status variations."""
        # 1. Create a running execution record with subtasks
        sub1 = SubtaskExecutionRecord(
            subtask_id="st-1",
            task_id="task-ser-1",
            status=SubtaskExecutionStatus.COMPLETED,
            result={"content": "File saved"},
            completed_at=int(time.time() * 1000) - 5000,
        )
        sub2 = SubtaskExecutionRecord(
            subtask_id="st-2",
            task_id="task-ser-1",
            status=SubtaskExecutionStatus.RUNNING,
            started_at=int(time.time() * 1000) - 4000,
        )
        rec = TaskExecutionRecord(
            task_id="task-ser-1",
            execution_plan_id="plan-ser-1",
            status=TaskExecutionStatus.RUNNING,
            created_at=int(time.time() * 1000) - 10000,
            started_at=int(time.time() * 1000) - 9000,
            subtask_records=[sub1, sub2],
        )

        task_info = execution_record_to_task_info(rec)
        assert isinstance(task_info, TaskInfo)
        assert task_info.task_id == "task-ser-1"
        assert task_info.status == TaskStatus.RUNNING
        assert len(task_info.subtasks) == 2
        assert task_info.subtasks[0]["status"] == "completed"
        assert task_info.subtasks[1]["status"] == "running"

        # 2. Status transition to paused
        rec.status = TaskExecutionStatus.PAUSED
        task_info_paused = execution_record_to_task_info(rec)
        assert task_info_paused.status == TaskStatus.PAUSED

        # 3. Status transition to awaiting approval
        rec.status = TaskExecutionStatus.AWAITING_APPROVAL
        task_info_awaiting = execution_record_to_task_info(rec)
        assert task_info_awaiting.status == TaskStatus.AWAITING_APPROVAL

        # 4. Status transition to completed
        rec.status = TaskExecutionStatus.COMPLETED
        rec.completed_at = int(time.time() * 1000)
        task_info_completed = execution_record_to_task_info(rec)
        assert task_info_completed.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_rest_tasks_listing_and_stats(self, async_client: AsyncClient):
        """Verify GET /api/v1/tasks and /api/v1/tasks/stats endpoints."""
        # Initialize state_manager
        await state_manager.initialize()

        rec1 = TaskExecutionRecord(task_id="t-list-1", execution_plan_id="p-1", status=TaskExecutionStatus.COMPLETED)
        rec2 = TaskExecutionRecord(task_id="t-list-2", execution_plan_id="p-2", status=TaskExecutionStatus.FAILED)
        rec3 = TaskExecutionRecord(task_id="t-list-3", execution_plan_id="p-3", status=TaskExecutionStatus.RUNNING)
        await state_manager.save_execution_record(rec1)
        await state_manager.save_execution_record(rec2)
        await state_manager.save_execution_record(rec3)

        # GET /api/v1/tasks
        resp = await async_client.get("/api/v1/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert data["total"] >= 3

        # Filter by status
        resp_filtered = await async_client.get("/api/v1/tasks?status=completed")
        assert resp_filtered.status_code == 200
        filtered_data = resp_filtered.json()
        assert all(t["status"] == "completed" for t in filtered_data["tasks"])

        # GET /api/v1/tasks/stats
        stats_resp = await async_client.get("/api/v1/tasks/stats")
        assert stats_resp.status_code == 200
        stats = stats_resp.json()
        assert stats["total_tasks"] >= 3
        assert stats["completed"] >= 1
        assert stats["failed"] >= 1

    @pytest.mark.asyncio
    async def test_rest_pause_resume_and_checkpoints_workflow(self, async_client: AsyncClient, monkeypatch):
        """Verify POST /tasks/{id}/pause, GET checkpoints, and POST resume workflows."""
        await orchestrator.initialize()

        # Seed an active task in orchestrator
        rec = TaskExecutionRecord(task_id="t-pause-api", execution_plan_id="p-pause", status=TaskExecutionStatus.RUNNING)
        mock_ctx = MagicMock()
        mock_ctx.task_id = "t-pause-api"
        mock_ctx.execution_record = rec
        mock_ctx.subtask_records = {}
        orchestrator._active_tasks["t-pause-api"] = mock_ctx

        # Mock pause_task behavior
        async def fake_pause(tid):
            rec.status = TaskExecutionStatus.PAUSED
            # create a checkpoint
            cp = Checkpoint(
                checkpoint_id="cp-pause-1",
                task_id=tid,
                checkpoint_type=CheckpointType.MANUAL,
                created_at=int(time.time() * 1000),
                execution_state={"status": "paused"},
                subtask_states={},
                completed_subtasks=["sub-0"],
                failed_subtasks=[],
                metadata={},
            )
            await state_manager.save_checkpoint(cp)
            return True

        monkeypatch.setattr(orchestrator, "pause_task", fake_pause)
        monkeypatch.setattr(orchestrator, "get_task_status", AsyncMock(return_value=rec))

        # 1. Pause task
        pause_resp = await async_client.post("/api/v1/tasks/t-pause-api/pause")
        assert pause_resp.status_code == 200
        assert pause_resp.json()["status"] == "paused"

        # 2. List checkpoints
        cp_resp = await async_client.get("/api/v1/tasks/t-pause-api/checkpoints")
        assert cp_resp.status_code == 200
        cps = cp_resp.json()["checkpoints"]
        assert len(cps) >= 1
        assert cps[0]["checkpoint_id"] == "cp-pause-1"
        assert cps[0]["checkpoint_type"] == "manual"

        # 3. Resume task (from checkpoint)
        async def fake_resume(cid):
            rec.status = TaskExecutionStatus.RUNNING
            return rec

        monkeypatch.setattr(orchestrator, "resume_from_checkpoint", fake_resume)
        resume_resp = await async_client.post("/api/v1/tasks/t-pause-api/resume?checkpoint_id=cp-pause-1")
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "running"

    @pytest.mark.asyncio
    async def test_rest_approval_and_denial_workflow(self, async_client: AsyncClient, monkeypatch):
        """Verify POST /tasks/{id}/approve handling for approval and denial."""
        rec = TaskExecutionRecord(
            task_id="t-appr-api",
            execution_plan_id="p-appr",
            status=TaskExecutionStatus.AWAITING_APPROVAL,
        )
        monkeypatch.setattr(orchestrator, "get_task_status", AsyncMock(return_value=rec))

        # 1. Deny approval -> task marks failed
        async def fake_approve(task_id, subtask_id=None, approved=True, reason=None):
            if not approved:
                rec.status = TaskExecutionStatus.FAILED
                return True
            rec.status = TaskExecutionStatus.RUNNING
            return True

        monkeypatch.setattr(orchestrator, "approve_subtask", fake_approve)

        deny_resp = await async_client.post(
            "/api/v1/tasks/t-appr-api/approve",
            json={"approved": False, "reason": "User rejected dangerous operation"},
        )
        assert deny_resp.status_code == 200
        assert deny_resp.json()["status"] == "failed"

        # 2. Approve -> task marks running
        rec.status = TaskExecutionStatus.AWAITING_APPROVAL
        approve_resp = await async_client.post(
            "/api/v1/tasks/t-appr-api/approve",
            json={"approved": True},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "running"

    @pytest.mark.asyncio
    async def test_rest_cancel_task_workflow(self, async_client: AsyncClient, monkeypatch):
        """Verify POST /tasks/{id}/cancel cancels active task."""
        rec = TaskExecutionRecord(task_id="t-can-api", execution_plan_id="p-can", status=TaskExecutionStatus.RUNNING)

        async def fake_cancel(tid):
            rec.status = TaskExecutionStatus.CANCELLED
            return True

        monkeypatch.setattr(orchestrator, "cancel_task", fake_cancel)
        monkeypatch.setattr(orchestrator, "get_task_status", AsyncMock(return_value=rec))

        cancel_resp = await async_client.post("/api/v1/tasks/t-can-api/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"


# ==============================================================================
# 2. Mocked WebSocket Integration Tests
# ==============================================================================

class TestWebSocketWorkflows:
    """WebSocket connection, command dispatch, event broadcast, reconnect & deduplication."""

    @pytest.mark.asyncio
    async def test_websocket_connection_ack_and_reconnect(self):
        """Verify connection ACK, client ID subscription, and clean reconnect."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws?client_id=ws-test-client-1") as ws1:
                ack1 = ws1.receive_json()
                assert ack1["type"] == "connection_ack"
                assert ack1["payload"]["client_id"] == "ws-test-client-1"

            # Reconnect with same client_id (verifies connection_manager disconnect & reconnect)
            with client.websocket_connect("/api/v1/ws?client_id=ws-test-client-1") as ws2:
                ack2 = ws2.receive_json()
                assert ack2["type"] == "connection_ack"
                assert ack2["payload"]["client_id"] == "ws-test-client-1"

    @pytest.mark.asyncio
    async def test_websocket_idempotent_event_handlers_registration(self):
        """Verify register_event_handlers is idempotent and does not register duplicate subscribers."""
        from m_core.api.websocket import server as ws_server

        # Calling multiple times should not raise and should not duplicate event bus listeners
        initial_handlers = dict(event_bus._subscribers)
        await register_event_handlers()
        await register_event_handlers()
        await register_event_handlers()

        # The count of subscribers for task events must remain constant
        assert ws_server._handlers_registered is True
        for event_name in ["task.step", "task.paused", "task.resumed", "task.completed"]:
            handlers = event_bus._subscribers.get(event_name, [])
            # Assert no duplicate identical handlers
            assert len(handlers) == len(set(handlers))

    @pytest.mark.asyncio
    async def test_websocket_inbound_commands_and_broadcasts(self, monkeypatch):
        """Test pause, resume, approve, deny, cancel, and task event broadcasts over WebSocket."""
        # Setup task subscription
        task_id = "ws-task-flow-1"

        pause_mock = AsyncMock(return_value=True)
        resume_mock = AsyncMock(return_value=MagicMock())
        approve_mock = AsyncMock(return_value=True)
        cancel_mock = AsyncMock(return_value=True)

        monkeypatch.setattr(orchestrator, "pause_task", pause_mock)
        monkeypatch.setattr(orchestrator, "resume_from_checkpoint", resume_mock)
        monkeypatch.setattr(orchestrator, "approve_subtask", approve_mock)
        monkeypatch.setattr(orchestrator, "cancel_task", cancel_mock)

        with TestClient(app) as client:
            with client.websocket_connect(f"/api/v1/ws?client_id=ws-tester&task_id={task_id}") as ws:
                ack = ws.receive_json()
                assert ack["type"] == "connection_ack"

                # 1. Ping -> pong
                ws.send_json({"type": "ping", "payload": {}})
                pong = ws.receive_json()
                assert pong["type"] == "pong"

                # 2. Pause task -> task_paused
                ws.send_json({"type": "pause_task", "payload": {"task_id": task_id}})
                paused_msg = ws.receive_json()
                assert paused_msg["type"] == "task_paused"

                # 3. Resume task -> task_resumed
                ws.send_json({"type": "resume_task", "payload": {"task_id": task_id, "checkpoint_id": "cp-1"}})
                resumed_msg = ws.receive_json()
                assert resumed_msg["type"] == "task_resumed"

                # 4. Approve action -> chat_response
                ws.send_json({"type": "approve_action", "payload": {"task_id": task_id, "subtask_id": "st-1", "approved": True}})
                appr_msg = ws.receive_json()
                assert appr_msg["type"] == "chat_response"

                # 5. Deny action -> chat_response
                ws.send_json({"type": "deny_action", "payload": {"task_id": task_id, "subtask_id": "st-1"}})
                deny_msg = ws.receive_json()
                assert deny_msg["type"] == "chat_response"

                # 6. Cancel task -> task_cancelled
                ws.send_json({"type": "cancel_task", "payload": {"task_id": task_id}})
                can_msg = ws.receive_json()
                assert can_msg["type"] == "task_cancelled"

        pause_mock.assert_awaited()
        approve_mock.assert_awaited()
        cancel_mock.assert_awaited()


# ==============================================================================
# 3. FastAPI Lifespan Startup, Shutdown & Resource Cleanup
# ==============================================================================

class TestFastApiLifespanAndResourceCleanup:
    """Verify lifespan startup hooks, orchestrator initialization, and clean shutdown."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_and_shutdown_cleanly(self):
        """Verify lifespan context manager executes all initialization and cleanup steps."""
        sched_shutdown_mock = AsyncMock()
        auto_close_mock = AsyncMock()
        desk_close_mock = AsyncMock()
        prov_close_mock = AsyncMock()
        vis_close_mock = AsyncMock()

        with patch.object(scheduler, "shutdown", sched_shutdown_mock), \
             patch.object(autonomous_executor, "close", auto_close_mock), \
             patch.object(desktop_automation, "close", desk_close_mock), \
             patch.object(provider_factory, "close", prov_close_mock), \
             patch("m_core.model_providers.close_vision_factory", vis_close_mock):

            async with lifespan(app):
                # Verify orchestrator is initialized during startup
                assert orchestrator._initialized is True

            # Verify all resources are cleanly shut down on exit
            sched_shutdown_mock.assert_awaited_once()
            auto_close_mock.assert_awaited_once()
            desk_close_mock.assert_awaited_once()
            prov_close_mock.assert_awaited_once()
            vis_close_mock.assert_awaited_once()

