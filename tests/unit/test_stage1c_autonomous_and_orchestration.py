"""Stage 1C mocked unit tests: Autonomous Loop, Recovery, Scheduler, Orchestrator, Persistence, and Frozen Structs."""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest

from m_core.common_schemas import CompletionResponse, FinishReason
from m_core.core.events import event_bus
from m_core.desktop.automation import DesktopObservation, InteractionMethod, InteractionResult
from m_core.model_providers.factory import provider_factory
from m_core.model_providers.vision_provider import ScreenAnalysis, VisionAction
from m_core.orchestration.autonomous_executor import AutonomousTaskExecutor
from m_core.orchestration.evaluator import ResultEvaluator
from m_core.orchestration.execution_manager import execution_manager
from m_core.orchestration.orchestrator import ExecutionOrchestrator, OrchestrationContext
from m_core.orchestration.recovery import FailureRecoveryManager, RecoveryContext
from m_core.orchestration.scheduler import TaskScheduler
from m_core.orchestration.schemas import (
    Checkpoint,
    CheckpointType,
    RecoveryAction,
    RecoveryPlan,
    SchedulerConfig,
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
    TaskExecutionStatus,
)
from m_core.orchestration.state_manager import ExecutionStateManager
from m_core.planning.schemas import (
    ApprovalStatus,
    ExecutionPlan,
    ExecutionStrategy,
    RoutingDecision,
    RoutingPlan,
    RoutingTargetType,
    Subtask,
    TaskAnalysis,
    TaskContext,
    TaskIntent,
    TaskType,
)


def _obs(
    title: str = "Untitled - Notepad",
    process_name: str = "notepad.exe",
    controls: list | None = None,
    changed: bool = True,
    b64: str | None = "ZmFrZS1zY3JlZW5zaG90",
) -> DesktopObservation:
    return DesktopObservation(
        timestamp=time.time(),
        active_window={"hwnd": 1001, "title": title, "process_name": process_name, "is_visible": True},
        windows=[{"hwnd": 1001, "title": title, "process_name": process_name, "is_visible": True}],
        uia_controls=controls
        or [
            {
                "type": "Edit",
                "title": "Text Editor",
                "auto_id": "editor_1",
                "enabled": True,
                "rect": {"left": 50, "top": 50, "width": 400, "height": 300},
                "center_x": 250,
                "center_y": 200,
            }
        ],
        screenshot_base64=b64,
        capture_mode="window",
        origin_x=0,
        origin_y=0,
        width=800,
        height=600,
        frame_hash="hash-a" if changed else "hash-same",
        screen_changed=changed,
    )


class TestFrozenMsgspecStructures:
    """Ensure frozen msgspec schemas reject direct mutation and support msgspec.structs.replace."""

    def test_frozen_structs_immutable_and_replaceable(self):
        subtask = Subtask(id="s1", name="Step", description="Desc", dependencies=["s0"])
        plan = ExecutionPlan(
            task_id="t1",
            original_request="Run",
            analysis=TaskAnalysis(intent=TaskIntent(primary="test")),
            subtasks=[subtask],
        )
        decision = RoutingDecision(subtask_id="s1", target_type=RoutingTargetType.AGENT, target_id="a1")
        rplan = RoutingPlan(execution_plan_id="t1", decisions=[decision])
        cp = Checkpoint(checkpoint_id="cp1", task_id="t1")
        rec_plan = RecoveryPlan(task_id="t1")
        sc_cfg = SchedulerConfig()

        for obj, field_name, val in [
            (subtask, "dependencies", []),
            (plan, "max_concurrency", 5),
            (decision, "target_id", "a2"),
            (rplan, "model_provider", "other"),
            (cp, "task_id", "t2"),
            (rec_plan, "max_attempts", 9),
            (sc_cfg, "max_concurrency", 10),
        ]:
            with pytest.raises((AttributeError, TypeError)):
                setattr(obj, field_name, val)

        updated = msgspec.structs.replace(subtask, dependencies=[], approval_status=ApprovalStatus.GRANTED)
        assert updated.dependencies == []
        assert updated.approval_status == ApprovalStatus.GRANTED
        assert subtask.dependencies == ["s0"]


class TestAutonomousTaskExecutorMocked:
    """Mocked closed-loop execution, approval gates, stuck recovery, pause/cancel, and vision integration."""

    async def test_observe_reason_act_verify_complete_loop(self, monkeypatch):
        desktop = MagicMock()
        desktop.initialize = AsyncMock(return_value=None)
        desktop._vision_factory = None
        desktop._blueprint_manager = None
        desktop.observe_desktop = AsyncMock(
            side_effect=[
                _obs(changed=True),
                _obs(changed=True),
                _obs(changed=True),
            ]
        )
        desktop.resolve_target = AsyncMock(
            return_value={
                "method": "uia",
                "x": 250,
                "y": 200,
                "control_identifier": {"type": "Edit", "auto_id": "editor_1"},
            }
        )
        desktop.ui_type = AsyncMock(
            return_value=InteractionResult(success=True, method_used=InteractionMethod.UI_AUTOMATION)
        )

        # Avoid slow asyncio.sleep settle wait in unit test
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
        auto_mod = sys.modules["m_core.orchestration.autonomous_executor"]
        monkeypatch.setattr(auto_mod, "get_vision_factory", AsyncMock(return_value=None))

        decisions = iter(
            [
                json.dumps(
                    {
                        "thought": "Type greeting into editor",
                        "step_goal": "Type text",
                        "action_type": "type",
                        "target": {"description": "Text Editor", "automation_id": "editor_1"},
                        "parameters": {"text": "Hello M"},
                        "expected_verification": {"type": "screen_changed", "value": ""},
                    }
                ),
                json.dumps(
                    {
                        "thought": "Text entered cleanly",
                        "step_goal": "Complete",
                        "action_type": "complete",
                        "final_summary": "Typed Hello M successfully",
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            provider_factory,
            "complete_with_fallback",
            AsyncMock(
                side_effect=lambda req: CompletionResponse(
                    id="step",
                    model="nvidia/nemotron-3-ultra-550b-a55b",
                    content=next(decisions),
                    finish_reason=FinishReason.STOP,
                )
            ),
        )

        executor = AutonomousTaskExecutor(desktop=desktop)
        subtask = Subtask(
            id="st-1",
            name="Write note",
            description="Open Notepad editor and write Hello M",
            approval_status=ApprovalStatus.GRANTED,
            metadata={"execution_mode": "autonomous_desktop", "max_steps": 5},
        )
        out = await executor.execute_subtask(
            subtask=subtask,
            task_context=TaskContext(task_id="t-loop"),
            correlation_id="corr-1",
        )
        assert out["success"] is True
        assert out["result"]["content"] == "Typed Hello M successfully"
        assert len(out["result"]["history"]) == 1
        desktop.ui_type.assert_awaited_once()

    async def test_destructive_step_triggers_approval_gate_then_digest_allows_resume(self, monkeypatch):
        desktop = MagicMock()
        desktop.initialize = AsyncMock(return_value=None)
        desktop._vision_factory = None
        desktop._blueprint_manager = None
        desktop.observe_desktop = AsyncMock(return_value=_obs(changed=True))
        desktop.resolve_target = AsyncMock(return_value={"method": "uia", "x": 100, "y": 100})
        desktop.click_at = AsyncMock(return_value=InteractionResult(success=True, method_used=InteractionMethod.MOUSE_KEYBOARD))
        desktop.type_text = AsyncMock(return_value=InteractionResult(success=True, method_used=InteractionMethod.MOUSE_KEYBOARD))

        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))
        auto_mod = sys.modules["m_core.orchestration.autonomous_executor"]
        monkeypatch.setattr(auto_mod, "get_vision_factory", AsyncMock(return_value=None))

        destructive_cmd = json.dumps(
            {
                "action_type": "type",
                "step_goal": "Type dangerous command",
                "target": {"description": "Prompt"},
                "parameters": {"text": "del /f /q C:\\important\\*"},
            }
        )
        complete_cmd = json.dumps(
            {
                "action_type": "complete",
                "step_goal": "Done",
                "final_summary": "Executed approved command",
            }
        )
        responses = iter([destructive_cmd, destructive_cmd, complete_cmd])
        monkeypatch.setattr(
            provider_factory,
            "complete_with_fallback",
            AsyncMock(
                side_effect=lambda req: CompletionResponse(
                    id="step",
                    model="nvidia/nemotron-3-ultra-550b-a55b",
                    content=next(responses),
                    finish_reason=FinishReason.STOP,
                )
            ),
        )

        executor = AutonomousTaskExecutor(desktop=desktop)
        subtask = Subtask(
            id="st-risk",
            name="Shell work",
            description="Run command",
            requires_approval=True,
            approval_status=ApprovalStatus.GRANTED,  # Even outer approval must STILL gate destructive text
            metadata={"execution_mode": "autonomous_desktop"},
        )
        gated = await executor.execute_subtask(subtask, TaskContext(task_id="t-risk"), "corr-2")
        assert gated["success"] is False
        assert gated["awaiting_approval"] is True
        assert gated["risk_level"] == "critical"
        digest = gated["pending_action"]["action_digest"]
        desktop.type_text.assert_not_awaited()

        # Resume with approved action_digest
        resumed_subtask = msgspec.structs.replace(
            subtask,
            metadata={
                **subtask.metadata,
                "approved_actions": [digest],
                "autonomous_state": gated["checkpoint_state"],
            },
        )
        ok = await executor.execute_subtask(resumed_subtask, TaskContext(task_id="t-risk"), "corr-2b")
        assert ok["success"] is True
        desktop.type_text.assert_awaited_once()

    async def test_pause_cancel_and_stuck_detection(self, monkeypatch):
        desktop = MagicMock()
        desktop.initialize = AsyncMock(return_value=None)
        desktop._blueprint_manager = None
        vision_mock = MagicMock()
        vision_mock.analyze_screen = AsyncMock(
            return_value=ScreenAnalysis(
                description="No change on screen",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="notepad",
                model_type="nvidia_nim",
                screenshot_dimensions={"width": 800, "height": 600},
            )
        )
        vision_mock.verify_action = AsyncMock(return_value={"verified": False, "reasoning": "unchanged"})
        desktop._vision_factory = vision_mock
        desktop.observe_desktop = AsyncMock(return_value=_obs(changed=False))
        desktop.resolve_target = AsyncMock(return_value={"method": "uia", "x": 100, "y": 100})
        desktop.click_at = AsyncMock(
            return_value=InteractionResult(success=True, method_used=InteractionMethod.MOUSE_KEYBOARD)
        )
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        # 1) Cooperative cancellation returns checkpoint_state before acting
        executor = AutonomousTaskExecutor(desktop=desktop)
        subtask = Subtask(
            id="st-cancel",
            name="Cancel test",
            description="Click",
            approval_status=ApprovalStatus.GRANTED,
            metadata={"execution_mode": "autonomous_desktop"},
        )
        pause_called = False

        async def _pause_waiter():
            nonlocal pause_called
            pause_called = True

        cancelled_res = await executor.execute_subtask(
            subtask,
            TaskContext(task_id="t-can"),
            "corr-c",
            pause_waiter=_pause_waiter,
            cancellation_check=lambda: True,
        )
        assert pause_called is True
        assert cancelled_res["cancelled"] is True
        assert cancelled_res["checkpoint_state"]["step_index"] == 0

        # 2) Stuck detection triggers full recapture & terminates cleanly after 4 unchanged clicks
        same_click = json.dumps(
            {
                "action_type": "click",
                "step_goal": "Click stuck btn",
                "target": {"description": "OK"},
                "parameters": {},
            }
        )
        monkeypatch.setattr(
            provider_factory,
            "complete_with_fallback",
            AsyncMock(
                return_value=CompletionResponse(
                    id="stk",
                    model="nvidia/nemotron-3-ultra-550b-a55b",
                    content=same_click,
                    finish_reason=FinishReason.STOP,
                )
            ),
        )
        stalled = await executor.execute_subtask(subtask, TaskContext(task_id="t-stk"), "corr-stk")
        assert stalled["success"] is False
        assert "stalled" in stalled["error"].lower()
        assert stalled["steps_executed"] == 4
        # Full capture requested on recovery iterations
        modes = [call.kwargs.get("capture_mode") for call in desktop.observe_desktop.await_args_list]
        assert "full" in modes

    async def test_vision_integration_contracts_in_autonomous_executor(self, monkeypatch):
        """Verify AutonomousTaskExecutor handles real ScreenAnalysis and VisionAction dataclasses cleanly."""
        vision = MagicMock()
        vision.analyze_screen = AsyncMock(
            side_effect=[
                ScreenAnalysis(
                    description="Analysis failed: HTTP 503",
                    elements=[],
                    text_content="",
                    suggested_actions=[],
                    app_type="unknown",
                    model_type="nvidia_nim",
                    screenshot_dimensions={},
                ),
                ScreenAnalysis(
                    description="NO - Save dialog is still open and file is missing.",
                    elements=[],
                    text_content="",
                    suggested_actions=[],
                    app_type="notepad",
                    model_type="nvidia_nim",
                    screenshot_dimensions={"width": 800, "height": 600},
                ),
            ]
        )
        vision.get_next_action = AsyncMock(
            return_value=VisionAction(
                action="click",
                target="Save button",
                params={"x": 320, "y": 480, "text": "", "key": ""},
                reasoning="Click Save",
                confidence=0.9,
            )
        )

        executor = AutonomousTaskExecutor(desktop=MagicMock())
        executor._vision_factory = vision

        # Failed ScreenAnalysis must NOT be treated as valid context
        failed_ctx = await executor._analyze_screen_context("ZmFrZQ==", "Save file", [])
        assert failed_ctx is None

        # Goal verification seeing ScreenAnalysis starting with "NO" must reject completion
        verified_done, note = await executor._verify_goal_completion(
            "Save file",
            {"final_summary": "Done"},
            _obs(),
            {"type": "visual_check", "value": "Dialog closed"},
        )
        assert verified_done is False

        # Vision fallback must unpack coordinates from VisionAction.params cleanly
        monkeypatch.setattr(provider_factory, "complete_with_fallback", AsyncMock(side_effect=RuntimeError("Nemotron down")))
        action = await executor._decide_next_action(
            goal="Click Save",
            execution_mode="autonomous_desktop",
            observation=_obs(),
            vision_summary=None,
            browser_state=None,
            history=[],
            memory_hints=[],
            step_index=0,
            max_steps=5,
            stuck_streak=0,
        )
        assert action["action_type"] == "click"
        assert action["target"]["x"] == 320
        assert action["target"]["y"] == 480

    def test_parse_json_object_windows_paths_and_resilience(self):
        """Verify _parse_json_object handles Windows backslashes, escaped paths, and malformed strings."""
        # Valid escaped Windows path
        valid_escaped = '{"path": "C:\\\\Program Files\\\\App\\\\bin", "cmd": "del /f /q C:\\\\test\\\\*"}'
        parsed1 = AutonomousTaskExecutor._parse_json_object(valid_escaped)
        assert parsed1 is not None
        assert parsed1["path"] == "C:\\Program Files\\App\\bin"
        assert parsed1["cmd"] == "del /f /q C:\\test\\*"

        # Raw single-backslash path from LLM
        raw_backslash = 'Here is the action: {"path": "C:\\important\\data", "action_type": "type"} thanks'
        parsed2 = AutonomousTaskExecutor._parse_json_object(raw_backslash)
        assert parsed2 is not None
        assert parsed2["path"] == "C:\\important\\data"
        assert parsed2["action_type"] == "type"

        # Completely invalid JSON fails closed (returns None)
        assert AutonomousTaskExecutor._parse_json_object("no json here") is None
        assert AutonomousTaskExecutor._parse_json_object("{invalid: json,") is None
        assert AutonomousTaskExecutor._parse_json_object("[1, 2, 3]") is None

    async def test_browser_step_lazy_lifecycle_and_close(self, monkeypatch):
        mock_browser_cls = MagicMock()
        mock_browser_inst = MagicMock()
        mock_browser_inst.initialize = AsyncMock(return_value=None)
        mock_browser_inst.navigate_and_explore = AsyncMock(return_value={"success": True, "url": "http://127.0.0.1:9999"})
        mock_browser_inst.extract_data = AsyncMock(return_value={"success": True, "data": "ok"})
        mock_browser_inst.close = AsyncMock(return_value=None)
        mock_browser_cls.return_value = mock_browser_inst
        auto_mod = sys.modules["m_core.orchestration.autonomous_executor"]
        monkeypatch.setattr(auto_mod, "BrowserAutomation", mock_browser_cls)

        executor = AutonomousTaskExecutor(desktop=MagicMock())
        res_nav = await executor._execute_browser_step({"description": "local page"}, {"browser_action": "navigate", "url": "http://127.0.0.1:9999"})
        assert res_nav["success"] is True
        res_ext = await executor._execute_browser_step({"description": "heading"}, {"browser_action": "extract"})
        assert res_ext["success"] is True

        await executor.close()
        mock_browser_inst.close.assert_awaited_once()
        assert executor._browser is None


class TestPersistenceRecoverySchedulerAndOrchestrator:
    """Verify disk persistence, recovery preservation, dependency-aware scheduler, and orchestrator pause/resume/approval."""

    async def test_state_manager_bundle_roundtrip_and_cleanup(self, tmp_path: Path):
        data_dir = tmp_path / "orch_state"
        sm1 = ExecutionStateManager(data_dir=data_dir)
        await sm1.initialize()

        ep = ExecutionPlan(
            task_id="plan-p1",
            original_request="Test persistence",
            analysis=TaskAnalysis(intent=TaskIntent(primary="test")),
            subtasks=[Subtask(id="sub-1", name="One", description="Do one")],
        )
        rp = RoutingPlan(
            execution_plan_id="plan-p1",
            decisions=[RoutingDecision(subtask_id="sub-1", target_type=RoutingTargetType.AGENT, target_id="general_assistant")],
        )
        tc = TaskContext(task_id="task-fresh", conversation_id="conv-1")
        rec_fresh = TaskExecutionRecord(
            task_id="task-fresh",
            execution_plan_id="plan-p1",
            routing_plan_id="plan-p1",
            status=TaskExecutionStatus.PAUSED,
        )
        sub_rec = SubtaskExecutionRecord(
            subtask_id="sub-1",
            task_id="task-fresh",
            status=SubtaskExecutionStatus.AWAITING_APPROVAL,
            metadata={"autonomous_state": {"step_index": 2}},
        )

        ctx_fresh = OrchestrationContext(
            task_id="task-fresh",
            execution_plan=ep,
            routing_plan=rp,
            task_context=tc,
            execution_record=rec_fresh,
            subtask_records={"sub-1": sub_rec},
            checkpoints=[],
        )
        await sm1.save_execution_state(ctx_fresh)
        await sm1.save_checkpoint(Checkpoint(checkpoint_id="cp-fresh", task_id="task-fresh", checkpoint_type=CheckpointType.MANUAL))

        # Also persist a 40-day old stale record for cleanup_old_records()
        old_ms = int((datetime.now().timestamp() - 40 * 86400) * 1000)
        rec_old = TaskExecutionRecord(
            task_id="task-old",
            execution_plan_id="plan-old",
            created_at=old_ms,
        )
        await sm1.save_execution_record(rec_old)
        await sm1.save_checkpoint(Checkpoint(checkpoint_id="cp-old", task_id="task-old"))

        # Cold reload in fresh ExecutionStateManager instance
        sm2 = ExecutionStateManager(data_dir=data_dir)
        await sm2.initialize()
        loaded_rec = await sm2.get_execution_record("task-fresh")
        loaded_bundle = await sm2.get_plan_bundle("task-fresh")
        assert loaded_rec.status == TaskExecutionStatus.PAUSED
        assert loaded_bundle["execution_plan"].task_id == "plan-p1"
        assert loaded_bundle["routing_plan"].decisions[0].target_id == "general_assistant"
        assert loaded_bundle["task_context"].conversation_id == "conv-1"

        removed = await sm2.cleanup_old_records(max_age_days=30)
        assert removed == 1
        assert await sm2.get_execution_record("task-old") is None
        assert not (data_dir / "checkpoints" / "cp-old.json").exists()
        raw_index = json.loads((data_dir / "executions.json").read_text(encoding="utf-8"))
        assert "" not in raw_index
        assert "task-fresh" in raw_index

    async def test_failure_recovery_preserves_approval_cancel_and_replaces_frozen_targets(self):
        mgr = FailureRecoveryManager()
        await mgr.initialize()

        sub = Subtask(id="s1", name="Step", description="Step", max_retries=1)
        ep = ExecutionPlan(task_id="p1", original_request="r", analysis=TaskAnalysis(intent=TaskIntent(primary="test")), subtasks=[sub])
        rp = RoutingPlan(
            execution_plan_id="p1",
            decisions=[RoutingDecision(subtask_id="s1", target_type=RoutingTargetType.TOOL, target_id="tool_a")],
        )
        exec_rec = TaskExecutionRecord(task_id="t1", execution_plan_id="p1", status=TaskExecutionStatus.AWAITING_APPROVAL)
        sub_rec = SubtaskExecutionRecord(subtask_id="s1", task_id="t1", status=SubtaskExecutionStatus.AWAITING_APPROVAL, error="Need approval")
        rc = RecoveryContext(task_id="t1", execution_record=exec_rec, subtask_records={"s1": sub_rec}, execution_plan=ep, routing_plan=rp, failed_subtask_id="s1")

        # Preserves AWAITING_APPROVAL -> ESCALATE without bumping retry_count
        plan_ap = await mgr.handle_failure(rc)
        assert plan_ap.action == RecoveryAction.ESCALATE
        assert sub_rec.retry_count == 0

        # Preserves CANCELLED -> CANCEL
        exec_rec.status = TaskExecutionStatus.CANCELLED
        sub_rec.status = SubtaskExecutionStatus.CANCELLED
        plan_can = await mgr.handle_failure(rc)
        assert plan_can.action == RecoveryAction.CANCEL

        # Mutates frozen RoutingDecision safely
        mgr._replace_routing_target(rc, "s1", RoutingTargetType.AGENT, "general_assistant")
        assert rc.routing_plan.decisions[0].target_type == RoutingTargetType.AGENT
        assert rc.routing_plan.decisions[0].target_id == "general_assistant"

        # Stores & pops replanned bundle cleanly
        exec_rec.status = TaskExecutionStatus.RUNNING
        sub_rec.status = SubtaskExecutionStatus.FAILED
        sub_rec.retry_count = 5
        replan_rec = await mgr._create_replan_recovery(rc, sub)
        assert replan_rec.action == RecoveryAction.REPLAN
        bundle = mgr.pop_replanned_bundle(replan_rec.new_execution_plan_id)
        assert bundle is not None
        assert mgr.pop_replanned_bundle(replan_rec.new_execution_plan_id) is None

    async def test_scheduler_preseed_dependency_skip_and_pause_waiter(self, monkeypatch):
        sched = TaskScheduler(SchedulerConfig(max_concurrency=2, default_timeout_seconds=10.0))
        await sched.initialize()

        s_done = Subtask(id="s0", name="Done", description="Already done")
        s_fail = Subtask(id="s1", name="Fail", description="Fails", max_retries=0)
        s_dep = Subtask(id="s2", name="Child", description="Depends on s1", dependencies=["s1"])
        ep = ExecutionPlan(
            task_id="plan-sch",
            original_request="Dag test",
            analysis=TaskAnalysis(intent=TaskIntent(primary="test")),
            subtasks=[s_done, s_fail, s_dep],
            strategy=ExecutionStrategy.DEPENDENCY_AWARE,
        )
        rp = RoutingPlan(
            execution_plan_id="plan-sch",
            decisions=[
                RoutingDecision(subtask_id="s0", target_type=RoutingTargetType.AGENT, target_id="general_assistant"),
                RoutingDecision(subtask_id="s1", target_type=RoutingTargetType.AGENT, target_id="general_assistant"),
                RoutingDecision(subtask_id="s2", target_type=RoutingTargetType.AGENT, target_id="general_assistant"),
            ],
        )
        records = {
            "s0": SubtaskExecutionRecord(subtask_id="s0", task_id="t-sch", status=SubtaskExecutionStatus.COMPLETED, result={"content": "cached"}),
            "s1": SubtaskExecutionRecord(subtask_id="s1", task_id="t-sch", status=SubtaskExecutionStatus.PENDING, max_retries=0),
            "s2": SubtaskExecutionRecord(subtask_id="s2", task_id="t-sch", status=SubtaskExecutionStatus.PENDING, dependencies=["s1"]),
        }

        executed = []

        async def fake_exec_subtask(subtask, routing_decision, record, **kwargs):
            executed.append(subtask.id)
            record.status = SubtaskExecutionStatus.FAILED
            record.error = "Boom"
            return {"success": False, "error": "Boom"}

        monkeypatch.setattr(execution_manager, "execute_subtask", fake_exec_subtask)

        pause_gate = asyncio.Event()
        pause_gate.set()

        res = await asyncio.wait_for(
            sched.execute_plan(
                execution_plan=ep,
                routing_plan=rp,
                subtask_records=records,
                task_context=TaskContext(task_id="t-sch"),
                correlation_id="corr-sch",
                pause_waiter=pause_gate.wait,
            ),
            timeout=5.0,
        )
        assert executed == ["s1"]  # s0 skipped execution (pre-seeded); s2 skipped due to s1 failure
        assert res["s0"]["result"] == {"content": "cached"}
        assert records["s2"].status == SubtaskExecutionStatus.SKIPPED
        await sched.shutdown()

    async def test_orchestrator_approval_pause_cold_resume_and_checkpoint_fail_closed(self, tmp_path: Path, monkeypatch):
        sm = ExecutionStateManager(data_dir=tmp_path / "orch")
        sched = TaskScheduler(SchedulerConfig(max_concurrency=1))
        rec = FailureRecoveryManager()
        ev = ResultEvaluator()
        orch = ExecutionOrchestrator(scheduler=sched, state_manager=sm, recovery_manager=rec, evaluator=ev)
        await orch.initialize()

        # 1) Fail-closed upfront approval & denial
        analysis_risky = TaskAnalysis(
            intent=TaskIntent(primary="file_cleanup"),
            task_type=TaskType.TOOL_USE,
            risk_level="critical",
            requires_approval=True,
        )
        plan_risky = ExecutionPlan(
            task_id="p-risky",
            original_request="Delete C:\\bad",
            analysis=analysis_risky,
            subtasks=[
                Subtask(
                    id="st-del",
                    name="Delete",
                    description="Delete file",
                    assigned_tool="file_delete",
                    requires_approval=True,
                    approval_status=ApprovalStatus.PENDING,
                )
            ],
        )
        routing_risky = RoutingPlan(
            execution_plan_id="p-risky",
            decisions=[RoutingDecision(subtask_id="st-del", target_type=RoutingTargetType.TOOL, target_id="file_delete")],
        )

        orch_mod = sys.modules["m_core.orchestration.orchestrator"]
        monkeypatch.setattr(orch_mod.task_understanding, "analyze", AsyncMock(return_value=analysis_risky))
        monkeypatch.setattr(orch_mod.planner, "create_plan", AsyncMock(return_value=plan_risky))
        monkeypatch.setattr(orch_mod.enhanced_router, "create_routing_plan", AsyncMock(return_value=routing_risky))

        exec_spy = AsyncMock(return_value={"success": True, "result": "should not run"})
        monkeypatch.setattr(execution_manager, "execute_subtask", exec_spy)

        rec_wait = await orch.execute_task("Delete C:\\bad")
        assert rec_wait.status == TaskExecutionStatus.AWAITING_APPROVAL
        exec_spy.assert_not_awaited()

        # Checkpoint resume must stay AWAITING_APPROVAL (fail closed) while unapproved
        cps = await sm.get_task_checkpoints(rec_wait.task_id)
        assert len(cps) >= 1
        cp_resume_rec = await orch.resume_from_checkpoint(cps[-1].checkpoint_id)
        assert cp_resume_rec.status == TaskExecutionStatus.AWAITING_APPROVAL
        exec_spy.assert_not_awaited()

        # Explicit denial marks FAILED and never runs tool
        denied = await orch.approve_subtask(rec_wait.task_id, approved=False, reason="Too risky")
        assert denied is True
        after_deny = await orch.get_task_status(rec_wait.task_id)
        assert after_deny.status == TaskExecutionStatus.FAILED
        exec_spy.assert_not_awaited()

        # 2) Pause -> persist MANUAL checkpoint -> cold-reload in fresh orchestrator -> approve pending digest & resume
        ep_pause = ExecutionPlan(
            task_id="p-pause",
            original_request="Run autonomous",
            analysis=TaskAnalysis(intent=TaskIntent(primary="desktop_automation")),
            subtasks=[
                Subtask(
                    id="st-auto",
                    name="Auto step",
                    description="Run",
                    requires_approval=False,
                    approval_status=ApprovalStatus.GRANTED,
                    metadata={"execution_mode": "autonomous_desktop"},
                )
            ],
        )
        rp_pause = RoutingPlan(
            execution_plan_id="p-pause",
            decisions=[RoutingDecision(subtask_id="st-auto", target_type=RoutingTargetType.AUTONOMOUS, target_id="autonomous_desktop")],
        )
        ctx_pause = OrchestrationContext(
            task_id="t-pause",
            execution_plan=ep_pause,
            routing_plan=rp_pause,
            task_context=TaskContext(task_id="t-pause"),
            execution_record=TaskExecutionRecord(task_id="t-pause", execution_plan_id="p-pause", status=TaskExecutionStatus.RUNNING),
            subtask_records={
                "st-auto": SubtaskExecutionRecord(
                    subtask_id="st-auto",
                    task_id="t-pause",
                    status=SubtaskExecutionStatus.AWAITING_APPROVAL,
                    metadata={
                        "autonomous_state": {"step_index": 2, "history": [{"step": 1}], "approved_digests": []},
                        "pending_action": {"action_digest": "deadbeef0123", "action_type": "tool", "step_goal": "Write", "risk_level": "high"},
                    },
                )
            },
            checkpoints=[],
        )
        orch._active_tasks["t-pause"] = ctx_pause
        paused_ok = await orch.pause_task("t-pause")
        assert paused_ok is True
        assert ctx_pause.execution_record.status == TaskExecutionStatus.PAUSED

        # Simulate restart into new ExecutionOrchestrator instance
        sm_cold = ExecutionStateManager(data_dir=tmp_path / "orch")
        sched_cold = TaskScheduler(SchedulerConfig(max_concurrency=1))
        orch_cold = ExecutionOrchestrator(scheduler=sched_cold, state_manager=sm_cold, recovery_manager=FailureRecoveryManager(), evaluator=ResultEvaluator())
        await orch_cold.initialize()

        restored_ctx = await orch_cold._restore_context("t-pause")
        assert restored_ctx is not None
        assert restored_ctx.execution_record.status == TaskExecutionStatus.PAUSED

        # Approve subtask transfers autonomous_state & approved_actions and resumes completion
        captured_subtasks = []

        async def finish_subtask(subtask, routing_decision, record, **kwargs):
            captured_subtasks.append(subtask)
            record.status = SubtaskExecutionStatus.COMPLETED
            record.result = {"content": "resumed and completed"}
            return {"success": True, "result": record.result}

        monkeypatch.setattr(execution_manager, "execute_subtask", finish_subtask)

        approved_ok = await orch_cold.approve_subtask("t-pause", subtask_id="st-auto", approved=True)
        assert approved_ok is True
        # Give background asyncio.create_task(_resume_execution) a moment to settle
        for _ in range(40):
            st = await orch_cold.get_task_status("t-pause")
            if st and st.status == TaskExecutionStatus.COMPLETED:
                break
            await asyncio.sleep(0.05)

        final_status = await orch_cold.get_task_status("t-pause")
        assert final_status.status == TaskExecutionStatus.COMPLETED
        assert len(captured_subtasks) == 1
        assert captured_subtasks[0].metadata["autonomous_state"]["step_index"] == 2
        assert "deadbeef0123" in captured_subtasks[0].metadata["approved_actions"]

        await sched.shutdown()
        await sched_cold.shutdown()

