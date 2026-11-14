"""Stage 1A mocked unit tests: ToolRegistry/Approvals, Visual Offsets, and Blueprints."""
import json
import sqlite3
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from m_core.common_schemas import ToolExecutionContext, ToolResult
from m_core.core.exceptions import ToolNotFoundError
from m_core.desktop.automation import DesktopAutomation, DesktopObservation
from m_core.desktop.blueprint import (
    ApplicationBlueprint,
    BlueprintAction,
    BlueprintElement,
    BlueprintManager,
    BlueprintStatus,
    BlueprintStore,
    ElementType,
)
from m_core.model_providers.vision_provider import VisionProviderType
from m_core.security import ConsentScope, consent_manager
from m_core.tools.base import BaseTool
from m_core.tools.builtins import visual_automation as vis_mod
from m_core.tools.registry import ToolRegistry


class StrictApprovalTool(BaseTool):
    manifest = {
        "name": "strict_approval_test_tool",
        "display_name": "Strict Approval Test Tool",
        "description": "Strict schema tool requiring approval",
        "category": "test",
        "risk_level": "high",
        "input_schema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"echo": {"type": "string"}},
        },
        "required_permissions": ["test:write"],
        "requires_approval": True,
        "timeout_seconds": 5,
        "max_retries": 0,
    }

    async def _execute_impl(self, context: ToolExecutionContext) -> ToolResult:
        return ToolResult(success=True, result={"echo": context.arguments["value"]})


class TestToolApprovalsAndRegistry:
    """Verify strict schema validation, fail-closed approval gates, and ToolRegistry sync/async contracts."""

    async def test_strict_additional_properties_allows_internal_approval_fields_only(self):
        tool = StrictApprovalTool()
        consent_manager.grant("test:write", scope=ConsentScope.SESSION)

        # Denied when neither approved nor approval_token is passed
        ctx_unapproved = ToolExecutionContext(
            tool_name=tool.name,
            arguments={"value": "hello"},
            consent_manager=consent_manager,
            correlation_id="corr-1",
        )
        res_denied = await tool.execute(ctx_unapproved)
        assert res_denied.success is False
        assert res_denied.error_code == "APPROVAL_REQUIRED"

        # Unknown extra field still rejected by additionalProperties: False
        ctx_extra = ToolExecutionContext(
            tool_name=tool.name,
            arguments={"value": "hello", "approved": True, "rogue_field": 123},
            consent_manager=consent_manager,
            correlation_id="corr-2",
        )
        res_invalid = await tool.execute(ctx_extra)
        assert res_invalid.success is False
        assert res_invalid.error_code == "VALIDATION_ERROR"

        # Stripped internal approval flags pass strict validation and execute
        ctx_approved = ToolExecutionContext(
            tool_name=tool.name,
            arguments={"value": "ok", "approved": True, "approval_token": "tok-1"},
            consent_manager=consent_manager,
            correlation_id="corr-3",
        )
        res_ok = await tool.execute(ctx_approved)
        assert res_ok.success is True
        assert res_ok.result == {"echo": "ok"}

    async def test_missing_consent_fails_closed(self):
        tool = StrictApprovalTool()
        ctx = ToolExecutionContext(
            tool_name=tool.name,
            arguments={"value": "ok", "approved": True},
            consent_manager=consent_manager,
            correlation_id="corr-4",
        )
        res = await tool.execute(ctx)
        assert res.success is False
        assert res.error_code == "CONSENT_REQUIRED"

    async def test_registry_sync_lookup_and_invoke_with_approval_token(self):
        reg = ToolRegistry()
        reg.register_tool_class(StrictApprovalTool)
        # Synchronous lookup contract (must NOT return coroutine)
        inst = reg.get_tool("strict_approval_test_tool")
        assert isinstance(inst, StrictApprovalTool)
        with pytest.raises(ToolNotFoundError):
            reg.get_tool("does_not_exist")

        consent_manager.grant("test:write", scope=ConsentScope.SESSION)
        res = await reg.invoke("strict_approval_test_tool", {"value": "via-token"}, approval_token="tok-xyz")
        assert res.success is True
        assert res.result == {"echo": "via-token"}


class TestVisualOffsetsAndDesktopObservation:
    """Mock mss/win32gui/tools to test DPI helper, virtual-screen offsets, adaptive capture & resolution."""

    async def test_dpi_aware_idempotent_and_screen_capture_origin_metadata(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(vis_mod, "_DPI_INITIALIZED", False)
        vis_mod._ensure_dpi_aware()
        assert vis_mod._DPI_INITIALIZED is True
        # Second call short-circuits cleanly
        vis_mod._ensure_dpi_aware()

        # Mock virtual monitor with negative multi-monitor left offset
        fake_shot = types.SimpleNamespace(
            size=(4, 3),
            bgra=bytes([0, 0, 255, 255] * 12),
        )

        class FakeMSS:
            monitors = [
                {"left": -1920, "top": 120, "width": 4, "height": 3},
                {"left": 0, "top": 0, "width": 4, "height": 3},
            ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def grab(self, monitor):
                return fake_shot

        monkeypatch.setattr(vis_mod, "MSS_AVAILABLE", True)
        monkeypatch.setattr(vis_mod, "mss", types.SimpleNamespace(mss=FakeMSS))
        monkeypatch.setattr(vis_mod, "_ensure_default_desktop", lambda: None)

        consent_manager.grant("screen:capture", scope=ConsentScope.SESSION)
        tool = vis_mod.ScreenCaptureTool()
        ctx = ToolExecutionContext(
            tool_name="screen_capture",
            arguments={"mode": "full", "save_to_file": False},
            consent_manager=consent_manager,
            correlation_id="corr-5",
        )
        res = await tool.execute(ctx)
        assert res.success is True
        assert res.result["origin_x"] == -1920
        assert res.result["origin_y"] == 120
        assert res.result["width"] == 4
        assert res.result["height"] == 3

    async def test_visual_find_offsets_color_match_by_monitor_origin(self, monkeypatch):
        # 20x20 image with an 8x8 target color block [10, 200, 30] starting at (x=5, y=4)
        rgb = np.zeros((20, 20, 3), dtype=np.uint8)
        rgb[4:12, 5:13] = [10, 200, 30]
        # MSS provides BGRA bytes
        bgra = np.zeros((20, 20, 4), dtype=np.uint8)
        bgra[:, :, 0] = rgb[:, :, 2]
        bgra[:, :, 1] = rgb[:, :, 1]
        bgra[:, :, 2] = rgb[:, :, 0]
        bgra[:, :, 3] = 255

        fake_shot = types.SimpleNamespace(size=(20, 20), bgra=bgra.tobytes())

        class FakeMSS:
            monitors = [{"left": 300, "top": 150, "width": 20, "height": 20}]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def grab(self, monitor):
                return fake_shot

        monkeypatch.setattr(vis_mod, "MSS_AVAILABLE", True)
        monkeypatch.setattr(vis_mod, "OPENCV_AVAILABLE", True)
        monkeypatch.setattr(vis_mod, "mss", types.SimpleNamespace(mss=FakeMSS))
        monkeypatch.setattr(vis_mod, "_ensure_default_desktop", lambda: None)

        consent_manager.grant("screen:find", scope=ConsentScope.SESSION)
        tool = vis_mod.VisualFindTool()
        ctx = ToolExecutionContext(
            tool_name="visual_find",
            arguments={"method": "color", "color": [10, 200, 30]},
            consent_manager=consent_manager,
            correlation_id="corr-6",
        )
        res = await tool.execute(ctx)
        assert res.success is True
        match = res.result["matches"][0]
        assert match["x"] == 305
        assert match["y"] == 154
        assert match["center_x"] == 305 + match["width"] // 2
        assert match["center_y"] == 154 + match["height"] // 2

    async def test_desktop_adaptive_observe_and_dynamic_resolution_order(self, tmp_path: Path):
        da = DesktopAutomation(evidence_dir=tmp_path / "evidence")
        da._initialized = True
        da._vision_factory = MagicMock()

        calls = []

        async def fake_execute_tool(tool_name, args, perms=None):
            calls.append((tool_name, args))
            if tool_name == "window_list":
                return {
                    "success": True,
                    "result": {
                        "windows": [
                            {"hwnd": 101, "title": "Untitled - Notepad", "is_visible": True, "process_name": "notepad.exe"}
                        ]
                    },
                }
            if tool_name == "window_interact":
                return {
                    "success": True,
                    "result": {
                        "controls": [
                            {
                                "type": "Button",
                                "title": "Save",
                                "auto_id": "btn_save",
                                "enabled": True,
                                "visible": True,
                                "rect": {"left": 100, "top": 200, "width": 80, "height": 30},
                            }
                        ]
                    },
                }
            return {"success": False}

        da._execute_tool = fake_execute_tool
        da.capture_screenshot_with_meta = AsyncMock(
            return_value={
                "success": True,
                "base64": "ZmFrZQ==",
                "path": str(tmp_path / "shot.png"),
                "width": 800,
                "height": 600,
                "origin_x": -100,
                "origin_y": 50,
                "mode": "window",
            }
        )

        obs = await da.observe_desktop(window_title="Notepad", capture_mode="adaptive")
        assert obs.capture_mode == "window"
        assert obs.origin_x == -100
        assert obs.origin_y == 50
        assert len(obs.uia_controls) == 1

        # UIA wins over OCR/Vision
        resolved_uia = await da.resolve_target("Save", observation=obs)
        assert resolved_uia["method"] == "uia"
        assert resolved_uia["x"] == 140
        assert resolved_uia["y"] == 215

        # Vision grounding offsets image coords by observation origin_x/origin_y
        empty_obs = DesktopObservation(
            timestamp=1.0,
            active_window={"title": "Custom App"},
            windows=[],
            uia_controls=[],
            screenshot_base64="ZmFrZQ==",
            origin_x=1920,
            origin_y=100,
        )
        da.find_text_on_screen = AsyncMock(return_value=MagicMock(success=False, details={}))
        da._vision_factory.find_elements = AsyncMock(
            return_value=[{"label": "Submit", "bbox": {"left": 10, "top": 20, "width": 40, "height": 20}, "confidence": 0.9}]
        )
        resolved_vis = await da.resolve_target("Submit", observation=empty_obs, provider_type=VisionProviderType.NVIDIA_NIM)
        assert resolved_vis["method"] == "vision"
        assert resolved_vis["x"] == 1920 + 10 + 20
        assert resolved_vis["y"] == 100 + 20 + 10


class TestBlueprintStoreAndManager:
    """Verify SQLite BlueprintStore schema, JSON storage, status transitions, and element lookup."""

    async def test_blueprint_store_non_double_encoded_json_and_stale(self, tmp_path: Path):
        db_file = tmp_path / "bp_test.db"
        store = BlueprintStore(db_path=db_file)
        elem = BlueprintElement(
            element_id="elem_save",
            element_type=ElementType.BUTTON,
            name="Save",
            description="Save document button",
            selectors={"auto_id": "btnSave", "title": "Save", "type": "Button", "control_id": 0},
            bbox={"left": 10, "top": 20, "width": 50, "height": 25},
            verified=True,
        )
        act = BlueprintAction(
            action_id="act_1",
            name="save_doc",
            description="Click Save",
            steps=[{"action": "click", "target": "Save"}],
            applications=["notepad.exe"],
        )
        bp = ApplicationBlueprint(
            blueprint_id="bp-1",
            app_name="notepad",
            process_name="notepad.exe",
            elements={"elem_save": elem},
            actions={"save_doc": act},
        )
        store.save_blueprint(bp)

        # Verify SQLite stores structured JSON (not double-encoded string literals)
        conn = sqlite3.connect(str(db_file))
        row = conn.execute("SELECT selectors, bbox FROM blueprint_elements WHERE element_id='elem_save'").fetchone()
        act_row = conn.execute("SELECT steps, applications FROM blueprint_actions WHERE action_id='act_1'").fetchone()
        conn.close()

        assert isinstance(json.loads(row[0]), dict)
        assert isinstance(json.loads(row[1]), dict)
        assert isinstance(json.loads(act_row[0]), list)
        assert isinstance(json.loads(act_row[1]), list)

        loaded = store.get_blueprint("bp-1")
        assert loaded is not None
        assert loaded.elements["elem_save"].selectors["auto_id"] == "btnSave"
        assert loaded.elements["elem_save"].element_type == ElementType.BUTTON
        assert isinstance(loaded.elements["elem_save"].element_type, ElementType)

        mgr = BlueprintManager(store=store)
        found = await mgr.find_element(loaded, "Notepad", "Save button")
        assert found is not None
        assert found.element_id == "elem_save"
        ident = mgr.get_control_identifier(found)
        assert ident == {"auto_id": "btnSave", "title": "Save", "type": "Button"}

        # Test status transitions & promotion
        bp.success_count = 4
        bp.failure_count = 0
        mgr.promote_blueprint(bp)
        assert bp.status == BlueprintStatus.ACTIVE

        mgr.mark_stale(loaded)
        reloaded = store.get_blueprint("bp-1")
        assert reloaded.status == BlueprintStatus.STALE
        store.close()
