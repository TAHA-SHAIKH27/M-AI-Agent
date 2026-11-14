"""Closed-loop Autonomous Task Executor integrating Nemotron reasoning, NVIDIA Vision, DesktopAutomation, and BrowserAutomation."""
import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from ..core.events import event_bus
from ..desktop import DesktopAutomation, DesktopObservation, desktop_automation
from ..memory import memory_manager
from ..memory.base import MemorySearchQuery, MemoryTier
from ..model_providers import VisionProviderType, get_vision_factory, provider_factory
from ..model_providers.schemas import ChatMessage, CompletionRequest, MessageRole
from ..planning.schemas import ApprovalStatus, Subtask
from ..security import AuditEventType, audit_logger, consent_manager
from ..tools import tool_registry
from ..utils.logging import get_logger
from ..web.browser_automation import BrowserAutomation

logger = get_logger(__name__)


AUTONOMOUS_PLANNER_SYSTEM_PROMPT = """You are M, an autonomous Windows desktop and browser task execution engine.
You observe live desktop/window/browser state and select ONE concrete next action per turn until the user's goal is completely achieved and verified.

Rules:
1. NEVER invent or hardcode static screen coordinates. Always describe targets semantically (e.g., {"description": "Save button", "text": "Save", "control_type": "Button", "automation_id": "..."}). Only include coordinates if directly observed in the live UIA control list for this turn.
2. Prefer fast structural/direct actions when reliable (`uia_interact`, `launch_app`, `focus_window`, `window_control`, `hotkey`, `browser`, `tool`), and use visual UI targeting (`click`, `double_click`, `type`) when structural controls are unavailable.
3. Verify outcomes carefully. Do NOT return `complete` until the observed screen, window state, file existence, or verification confirms the goal is genuinely finished.
4. If the previous action failed or the screen stayed unchanged, adapt: focus the right window, dismiss blocking dialogs, switch from UIA to visual targeting, or try a keyboard shortcut.
5. Set `"requires_approval": true` when an action deletes/overwrites files, runs shell/PowerShell/system admin commands, modifies system settings, or performs irreversible external submissions.

Return ONLY valid JSON matching this schema:
{
  "thought": "Brief analysis of current observation vs goal",
  "step_goal": "Short human-readable label for this step",
  "action_type": "uia_interact|click|double_click|right_click|type|hotkey|press_key|scroll|drag|launch_app|focus_window|window_control|browser|tool|wait|verify|complete|fail",
  "target": {
    "description": "Semantic description of target control or element",
    "text": "Exact visible label/text if known",
    "control_type": "Button|Edit|Document|MenuItem|...",
    "automation_id": "",
    "window_title": ""
  },
  "parameters": {
    "text": "",
    "keys": [],
    "key": "",
    "app_name": "",
    "window_title": "",
    "window_action": "focus|maximize|minimize|restore|close",
    "direction": "down|up",
    "scroll_amount": 3,
    "to_target": {},
    "tool_name": "",
    "tool_arguments": {},
    "browser_action": "navigate|click|type|press_key|scroll|extract|screenshot",
    "url": "",
    "seconds": 1.0
  },
  "expected_verification": {
    "type": "screen_changed|window_exists|title_contains|text_present|element_exists|file_exists|visual_check|none",
    "value": ""
  },
  "requires_approval": false,
  "risk_reason": "",
  "final_summary": ""
}"""


@dataclass
class AutonomousStepResult:
    """Result returned for each executed autonomous step."""
    step_index: int
    action_type: str
    step_goal: str
    success: bool
    verified: bool
    verification_note: str = ""
    error: Optional[str] = None
    observation_summary: Dict[str, Any] = field(default_factory=dict)


class AutonomousTaskExecutor:
    """
    Executes multi-step, cross-application Windows and browser tasks in a closed loop:
    Observe (Adaptive capture + UIA + Browser DOM) -> Reason (Nemotron + Vision) ->
    Resolve target dynamically -> Enforce approval/security -> Execute -> Verify -> Recover.
    """

    HIGH_RISK_TOOLS = {
        "file_delete",
        "file_write",
        "powershell",
        "system_command",
        "shortcut_create",
    }
    CRITICAL_TOOLS = {"file_delete", "powershell", "system_command"}

    def __init__(self, desktop: Optional[DesktopAutomation] = None):
        self._desktop = desktop or desktop_automation
        self._browser: Optional[BrowserAutomation] = None
        self._vision_factory = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize desktop, vision, and model dependencies lazily."""
        if self._initialized:
            return
        await self._desktop.initialize()
        if getattr(self._desktop, "_vision_factory", None) is not None:
            self._vision_factory = self._desktop._vision_factory
        else:
            try:
                self._vision_factory = await get_vision_factory()
            except Exception as e:
                logger.warning("Vision factory unavailable during autonomous init: %s", e)
                self._vision_factory = None
        self._initialized = True

    async def _ensure_browser(self) -> BrowserAutomation:
        """Lazily initialize BrowserAutomation only when a web step needs it."""
        if self._browser is None:
            self._browser = BrowserAutomation()
            await self._browser.initialize()
        return self._browser

    async def close(self) -> None:
        """Release browser resources if opened."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning("Error closing BrowserAutomation: %s", e)
            self._browser = None

    async def execute_subtask(
        self,
        subtask: Subtask,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        pause_waiter: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Execute a goal-oriented autonomous subtask across Windows/browser UIs."""
        await self.initialize()

        meta = dict(subtask.metadata or {})
        goal = str(meta.get("goal") or subtask.description or subtask.name)
        execution_mode = str(meta.get("execution_mode") or "autonomous_desktop")
        target_app = meta.get("target_app")
        max_steps = int(meta.get("max_steps", 25))
        recovery_hint = str(meta.get("recovery_hint") or "")

        # Resume state (if continuing from checkpoint / approval / pause)
        resume_state = dict(meta.get("autonomous_state") or {})
        step_index = int(resume_state.get("step_index", 0))
        history: List[Dict[str, Any]] = list(resume_state.get("history", []))
        approved_digests: Set[str] = set(meta.get("approved_actions", []) or resume_state.get("approved_digests", []))
        subtask_approved = (subtask.approval_status == ApprovalStatus.GRANTED) or (not subtask.requires_approval)

        # Fetch procedural memory hints safely
        memory_hints = await self._load_memory_hints(goal)
        if recovery_hint:
            memory_hints.append(f"Recovery hint from previous failure: {recovery_hint}")

        # Respect explicit denial upfront
        if subtask.requires_approval and subtask.approval_status == ApprovalStatus.DENIED:
            return {
                "success": False,
                "error": "Subtask approval denied",
                "steps_executed": len(history),
                "history": history,
            }

        consecutive_failures = 0
        stuck_streak = 0
        last_action_sig = ""
        current_window_hint: Optional[str] = resume_state.get("current_window_hint") or (
            str(target_app) if target_app else None
        )
        capture_mode = "adaptive"

        obs: Optional[DesktopObservation] = None

        while step_index < max_steps:
            # Cooperative pause and cancellation gates
            if pause_waiter is not None:
                await pause_waiter()
            if cancellation_check and cancellation_check():
                checkpoint_state = self._build_resume_state(
                    step_index, history, approved_digests, current_window_hint
                )
                return {
                    "success": False,
                    "cancelled": True,
                    "error": "Task cancelled by user",
                    "checkpoint_state": checkpoint_state,
                    "history": history,
                }

            # 1. Observe desktop / active browser
            obs = await self._desktop.observe_desktop(
                window_title=current_window_hint,
                capture_mode=capture_mode,
                include_uia=True,
                include_screenshot=True,
            )
            capture_mode = "adaptive"

            browser_state = await self._get_browser_state_if_active(execution_mode)

            # Visual enrichment on first step, after failure/stuckness, or when UIA controls are sparse
            vision_summary = None
            need_vision_analysis = (
                obs.screenshot_base64 is not None
                and self._vision_factory is not None
                and (
                    step_index == 0
                    or consecutive_failures > 0
                    or stuck_streak >= 1
                    or len(obs.uia_controls) < 3
                )
            )
            if need_vision_analysis:
                vision_summary = await self._analyze_screen_context(
                    obs.screenshot_base64, goal, history
                )

            # 2. Reason with Nemotron for next concrete action
            decision = await self._decide_next_action(
                goal=goal,
                execution_mode=execution_mode,
                observation=obs,
                vision_summary=vision_summary,
                browser_state=browser_state,
                history=history,
                memory_hints=memory_hints,
                step_index=step_index,
                max_steps=max_steps,
                stuck_streak=stuck_streak,
            )

            action_type = str(decision.get("action_type") or "fail").lower().strip()
            step_goal = str(decision.get("step_goal") or action_type)
            target = decision.get("target") or {}
            params = decision.get("parameters") or {}
            expected_ver = decision.get("expected_verification") or {}

            # 3. Terminal completion / explicit failure decisions
            if action_type == "complete":
                verified_done, note = await self._verify_goal_completion(
                    goal, decision, obs, expected_ver
                )
                if not verified_done and step_index + 1 < max_steps and stuck_streak < 2:
                    history.append({
                        "step": step_index + 1,
                        "action_type": "verify_completion",
                        "step_goal": "Confirm goal completion",
                        "success": False,
                        "verified": False,
                        "note": note,
                    })
                    stuck_streak += 1
                    capture_mode = "full"
                    step_index += 1
                    continue

                summary = str(decision.get("final_summary") or note or f"Completed: {goal}")
                await self._record_completion_memory(
                    task_context, goal, summary, history, obs
                )
                return {
                    "success": True,
                    "result": {
                        "content": summary,
                        "steps_executed": step_index + 1,
                        "history": history,
                        "verified": verified_done,
                        "active_window": (obs.active_window or {}).get("title") if obs and obs.active_window else None,
                    },
                }

            if action_type == "fail":
                reason = str(decision.get("final_summary") or decision.get("thought") or "Autonomous planner reported task unachievable")
                return {
                    "success": False,
                    "error": reason,
                    "steps_executed": step_index,
                    "history": history,
                    "checkpoint_state": self._build_resume_state(
                        step_index, history, approved_digests, current_window_hint
                    ),
                }

            # 4. Security & approval gating
            action_digest = self._compute_action_digest(action_type, target, params)
            needs_gate, risk_level, risk_reason = self._assess_step_risk(
                action_type=action_type,
                target=target,
                params=params,
                model_flagged=bool(decision.get("requires_approval", False)),
                model_risk_reason=str(decision.get("risk_reason") or ""),
                subtask_approved=subtask_approved,
                approved_digests=approved_digests,
                action_digest=action_digest,
            )
            if needs_gate:
                checkpoint_state = self._build_resume_state(
                    step_index, history, approved_digests, current_window_hint,
                    pending_action={
                        "action_digest": action_digest,
                        "action_type": action_type,
                        "step_goal": step_goal,
                        "target": target,
                        "parameters": params,
                        "risk_level": risk_level,
                        "risk_reason": risk_reason,
                    },
                )
                return {
                    "success": False,
                    "awaiting_approval": True,
                    "risk_level": risk_level,
                    "approval_reason": risk_reason,
                    "pending_action": checkpoint_state["pending_action"],
                    "checkpoint_state": checkpoint_state,
                    "error": f"Approval required: {risk_reason}",
                }

            # Emit step started event
            await self._emit_step_event(
                task_context=task_context,
                subtask_id=subtask.id,
                step_index=step_index + 1,
                max_steps=max_steps,
                action_type=action_type,
                step_goal=step_goal,
                phase="executing",
                correlation_id=correlation_id,
                progress_callback=progress_callback,
            )

            # 5. Execute step dynamically (UIA -> Blueprint -> OCR -> Vision / Browser / Tool)
            exec_res = await self._execute_action(
                action_type=action_type,
                target=target,
                params=params,
                observation=obs,
                current_window_hint=current_window_hint,
                correlation_id=correlation_id,
                force_vision_grounding=(stuck_streak >= 1),
            )

            if exec_res.get("window_hint"):
                current_window_hint = exec_res["window_hint"]

            # 6. Post-action settle and adaptive verification
            await asyncio.sleep(0.5 if action_type != "wait" else 0.0)
            post_obs = await self._desktop.observe_desktop(
                window_title=current_window_hint,
                capture_mode="adaptive",
                include_uia=True,
                include_screenshot=True,
            )
            verified, verification_note = await self._verify_step_outcome(
                action_type=action_type,
                exec_res=exec_res,
                pre_obs=obs,
                post_obs=post_obs,
                expected_ver=expected_ver,
            )

            step_ok = bool(exec_res.get("success", False))
            step_record = {
                "step": step_index + 1,
                "action_type": action_type,
                "step_goal": step_goal,
                "target": target,
                "method": exec_res.get("method"),
                "success": step_ok,
                "verified": verified,
                "note": verification_note,
                "error": exec_res.get("error"),
                "active_window": (post_obs.active_window or {}).get("title") if post_obs.active_window else None,
            }
            history.append(step_record)

            # Learn UI control in BlueprintManager on verified UI successes
            if step_ok and verified and exec_res.get("resolved_control") and post_obs.active_window:
                await self._learn_blueprint_step(
                    post_obs.active_window, action_type, target, exec_res["resolved_control"]
                )

            await self._emit_step_event(
                task_context=task_context,
                subtask_id=subtask.id,
                step_index=step_index + 1,
                max_steps=max_steps,
                action_type=action_type,
                step_goal=step_goal,
                phase="verified" if (step_ok and verified) else "recovering",
                correlation_id=correlation_id,
                progress_callback=progress_callback,
                extra={"success": step_ok, "verified": verified, "note": verification_note},
            )

            # 7. Recovery & stuck detection
            action_sig = f"{action_type}:{action_digest}"
            if not step_ok:
                consecutive_failures += 1
                capture_mode = "full"
                if current_window_hint:
                    try:
                        await self._desktop.focus_window(current_window_hint)
                    except Exception:
                        pass
            elif not post_obs.screen_changed and action_type not in ("wait", "verify", "tool"):
                consecutive_failures = 0
                if action_sig == last_action_sig:
                    stuck_streak += 1
                else:
                    stuck_streak = max(stuck_streak, 1)
                capture_mode = "full"
            else:
                consecutive_failures = 0
                stuck_streak = 0

            last_action_sig = action_sig

            if consecutive_failures >= 4 or stuck_streak >= 4:
                return {
                    "success": False,
                    "error": f"Autonomous execution stalled after {step_index + 1} steps: {exec_res.get('error') or verification_note}",
                    "steps_executed": step_index + 1,
                    "history": history,
                    "checkpoint_state": self._build_resume_state(
                        step_index + 1, history, approved_digests, current_window_hint
                    ),
                }

            step_index += 1

        return {
            "success": False,
            "error": f"Reached maximum autonomous step budget ({max_steps})",
            "steps_executed": step_index,
            "history": history,
            "checkpoint_state": self._build_resume_state(
                step_index, history, approved_digests, current_window_hint
            ),
        }

    # ------------------------------------------------------------------
    # Reasoning & Observation Helpers
    # ------------------------------------------------------------------

    async def _analyze_screen_context(
        self,
        screenshot_b64: str,
        goal: str,
        history: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Ask NVIDIA Vision to summarize relevant on-screen context when UIA is sparse or recovering."""
        if not self._vision_factory:
            return None
        try:
            recent = "; ".join(f"{h.get('action_type')}({h.get('step_goal')})" for h in history[-4:])
            prompt = (
                f"Task goal: {goal}. Recent steps: {recent or 'none'}. "
                "Describe visible windows, dialogs, error popups, active text/inputs, and key interactive controls clearly in <=120 words."
            )
            res = await self._vision_factory.analyze_screen(screenshot_b64, prompt)
            desc = getattr(res, "description", None) if res is not None else None
            if desc and not desc.startswith(("Analysis failed:", "Error:")):
                return desc[:1200]
        except Exception as e:
            logger.debug("Vision screen analysis skipped: %s", e)
        return None

    async def _decide_next_action(
        self,
        goal: str,
        execution_mode: str,
        observation: DesktopObservation,
        vision_summary: Optional[str],
        browser_state: Optional[Dict[str, Any]],
        history: List[Dict[str, Any]],
        memory_hints: List[str],
        step_index: int,
        max_steps: int,
        stuck_streak: int,
    ) -> Dict[str, Any]:
        """Use NVIDIA Nemotron reasoning model to pick the next step."""
        active_win = observation.active_window or {}
        windows_summary = [
            {"title": (w.get("title") or "")[:80], "process": w.get("process_name")}
            for w in observation.windows[:12]
        ]
        controls_summary = [
            {
                "type": c.get("type"),
                "title": c.get("title"),
                "auto_id": c.get("auto_id"),
                "enabled": c.get("enabled", True),
            }
            for c in observation.uia_controls[:30]
        ]
        bp_hints = self._get_blueprint_hints(active_win.get("process_name"))
        available_tools = [
            t["name"] for t in tool_registry.list_tools()
            if all(consent_manager.is_granted(p) for p in t.get("required_permissions", []))
        ][:30]

        state_payload = {
            "goal": goal,
            "execution_mode": execution_mode,
            "step": f"{step_index + 1}/{max_steps}",
            "stuck_streak": stuck_streak,
            "capture_mode": observation.capture_mode,
            "screen_changed": observation.screen_changed,
            "active_window": {
                "title": active_win.get("title"),
                "process": active_win.get("process_name"),
            },
            "visible_windows": windows_summary,
            "uia_controls": controls_summary,
            "known_blueprint_controls": bp_hints,
            "browser_state": browser_state,
            "vision_summary": vision_summary,
            "memory_hints": memory_hints[:4],
            "available_tools": available_tools,
            "recent_history": history[-6:],
        }

        req = CompletionRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=AUTONOMOUS_PLANNER_SYSTEM_PROMPT),
                ChatMessage(
                    role=MessageRole.USER,
                    content=f"Current state:\n{json.dumps(state_payload, ensure_ascii=False)}",
                ),
            ],
            temperature=0.15,
            max_tokens=900,
            stream=False,
        )

        try:
            response = await provider_factory.complete_with_fallback(req)
            parsed = self._parse_json_object(response.content or "")
            if parsed and "action_type" in parsed:
                return parsed
        except Exception as e:
            logger.warning("Nemotron step decision failed, falling back to Vision get_next_action: %s", e)

        # Vision fallback if primary text reasoning unavailable
        if self._vision_factory and observation.screenshot_base64:
            try:
                prev = [f"{h.get('action_type')}: {h.get('step_goal')}" for h in history[-5:]]
                va = await self._vision_factory.get_next_action(
                    observation.screenshot_base64,
                    goal,
                    previous_actions=prev,
                )
                params_dict = dict(va.params) if isinstance(va.params, dict) else {}
                target_x = params_dict.get("x", getattr(va, "x", None))
                target_y = params_dict.get("y", getattr(va, "y", None))
                text_val = str(params_dict.get("text") or getattr(va, "text", "") or "")
                key_val = str(params_dict.get("key") or getattr(va, "key", "") or "")
                return {
                    "thought": va.reasoning,
                    "step_goal": f"{va.action} {va.target or ''}".strip(),
                    "action_type": "complete" if va.is_complete else (va.action or "wait"),
                    "target": {"description": va.target or "", "x": target_x, "y": target_y},
                    "parameters": {"text": text_val, "key": key_val},
                    "expected_verification": {"type": "screen_changed", "value": va.expected_result or ""},
                    "requires_approval": False,
                    "final_summary": va.reasoning if va.is_complete else "",
                }
            except Exception as e:
                logger.warning("Vision fallback action failed: %s", e)

        return {
            "action_type": "fail",
            "final_summary": "Neither Nemotron reasoning nor Vision provider produced an actionable step.",
        }

    # ------------------------------------------------------------------
    # Dynamic Target Resolution & Execution
    # ------------------------------------------------------------------

    async def _execute_action(
        self,
        action_type: str,
        target: Dict[str, Any],
        params: Dict[str, Any],
        observation: DesktopObservation,
        current_window_hint: Optional[str],
        correlation_id: str,
        force_vision_grounding: bool = False,
    ) -> Dict[str, Any]:
        """Dispatch single step cleanly across UIA, Vision/OCR desktop input, browser, or tools."""
        win_hint = target.get("window_title") or params.get("window_title") or current_window_hint
        if not win_hint and observation.active_window:
            win_hint = observation.active_window.get("title")

        if action_type == "wait":
            secs = min(10.0, max(0.2, float(params.get("seconds", 1.0))))
            await asyncio.sleep(secs)
            return {"success": True, "method": "wait"}

        if action_type == "verify":
            return {"success": True, "method": "verify"}

        if action_type == "launch_app":
            app_name = str(params.get("app_name") or target.get("text") or target.get("description") or "").strip()
            if not app_name:
                return {"success": False, "error": "launch_app missing app_name"}
            res = await self._desktop.launch_application(app_name, args=str(params.get("args") or ""), wait_for_window=True)
            details = res.details if isinstance(res.details, dict) else {}
            win_dict = details.get("window") or {}
            win_title = win_dict.get("title") if isinstance(win_dict, dict) else None
            return {
                "success": res.success,
                "error": res.error,
                "method": "launch_app",
                "window_hint": win_title or details.get("process_name") or app_name,
            }

        if action_type in ("focus_window", "window_control"):
            waction = str(params.get("window_action") or ("focus" if action_type == "focus_window" else "focus")).lower()
            ident = params.get("process_name") or win_hint or target.get("window_title") or target.get("description")
            if not ident:
                return {"success": False, "error": "No window/process identifier specified"}
            if waction == "close":
                res = await self._desktop.close_application(ident)
            elif waction == "maximize":
                res = await self._desktop.maximize_window(ident)
            elif waction == "minimize":
                res = await self._desktop.minimize_window(ident)
            elif waction == "restore":
                res = await self._desktop.restore_window(ident)
            else:
                res = await self._desktop.focus_window(ident)
            return {
                "success": res.success,
                "error": res.error,
                "method": f"window:{waction}",
                "window_hint": str(ident) if waction != "close" else current_window_hint,
            }

        if action_type == "hotkey":
            keys = params.get("keys") or []
            if isinstance(keys, str):
                keys = [k.strip() for k in re.split(r"[+\s]+", keys) if k.strip()]
            if not keys:
                return {"success": False, "error": "hotkey requires keys list"}
            res = await self._desktop.send_hotkey(keys)
            return {"success": res.success, "error": res.error, "method": "hotkey"}

        if action_type == "press_key":
            key = str(params.get("key") or "").strip()
            if not key:
                return {"success": False, "error": "press_key requires key"}
            res = await self._desktop.press_key(key)
            return {"success": res.success, "error": res.error, "method": "press_key"}

        if action_type == "uia_interact":
            ident: Dict[str, Any] = {}
            if target.get("automation_id"):
                ident["auto_id"] = target["automation_id"]
            if target.get("text") or target.get("description"):
                ident["title"] = target.get("text") or target.get("description")
            if target.get("control_type"):
                ident["type"] = target["control_type"]
            if win_hint and ident:
                text_to_type = params.get("text")
                if text_to_type is not None and text_to_type != "":
                    res = await self._desktop.ui_type(win_hint, ident, str(text_to_type))
                else:
                    res = await self._desktop.ui_click(win_hint, ident)
                if res.success:
                    return {
                        "success": True,
                        "method": "uia",
                        "resolved_control": {"control_identifier": ident},
                    }
            # Fall through to full dynamic resolution if direct UIA selector missed
            action_type = "type" if params.get("text") else "click"

        if action_type in ("click", "double_click", "right_click", "type"):
            resolved = await self._resolve_dynamic_target(
                target=target,
                observation=observation,
                window_title=win_hint,
                force_vision=force_vision_grounding,
            )

            # Handle typing cleanly (click/focus resolved target first when given)
            if action_type == "type":
                text = str(params.get("text") or "")
                if resolved and resolved.get("method") in ("uia", "blueprint_uia") and resolved.get("control_identifier") and win_hint:
                    ui_res = await self._desktop.ui_type(win_hint, resolved["control_identifier"], text)
                    if ui_res.success:
                        return {
                            "success": True,
                            "method": "uia_type",
                            "resolved_control": resolved,
                        }
                if resolved and resolved.get("x") is not None and resolved.get("y") is not None:
                    await self._desktop.click_at(int(resolved["x"]), int(resolved["y"]))
                    await asyncio.sleep(0.15)
                res = await self._desktop.type_text(text)
                return {
                    "success": res.success,
                    "error": res.error,
                    "method": f"sendinput:{resolved.get('method') if resolved else 'focused'}",
                    "resolved_control": resolved,
                }

            if not resolved or resolved.get("x") is None or resolved.get("y") is None:
                return {
                    "success": False,
                    "error": f"Could not dynamically locate target: {target.get('description') or target.get('text')}",
                }

            # Prefer UIA structural click first if non-forced single left click
            if (
                action_type == "click"
                and not force_vision_grounding
                and resolved.get("method") in ("uia", "blueprint_uia")
                and resolved.get("control_identifier")
                and win_hint
            ):
                ui_res = await self._desktop.ui_click(win_hint, resolved["control_identifier"])
                if ui_res.success:
                    return {
                        "success": True,
                        "method": f"{resolved['method']}_click",
                        "resolved_control": resolved,
                    }

            clicks = 2 if action_type == "double_click" else 1
            button = "right" if action_type == "right_click" else "left"
            res = await self._desktop.click_at(
                int(resolved["x"]), int(resolved["y"]), button=button, clicks=clicks
            )
            return {
                "success": res.success,
                "error": res.error,
                "method": resolved.get("method", "vision"),
                "resolved_control": resolved,
            }

        if action_type == "scroll":
            direction = str(params.get("direction") or "down").lower()
            amount = int(params.get("scroll_amount", 3))
            delta = -abs(amount) if direction == "down" else abs(amount)
            resolved = await self._resolve_dynamic_target(target, observation, win_hint) if (target.get("description") or target.get("text")) else None
            tool_args: Dict[str, Any] = {"action": "scroll", "scroll_amount": delta}
            if resolved and resolved.get("x") is not None:
                tool_args["x"] = int(resolved["x"])
                tool_args["y"] = int(resolved["y"])
            res = await self._desktop._execute_tool("mouse_control", tool_args, ["automation:input"], correlation_id)
            return {"success": res.get("success", False), "error": res.get("error"), "method": "scroll"}

        if action_type == "drag":
            src = await self._resolve_dynamic_target(target, observation, win_hint)
            dst_target = params.get("to_target") or {}
            dst = await self._resolve_dynamic_target(dst_target, observation, win_hint)
            if not src or not dst or src.get("x") is None or dst.get("x") is None:
                return {"success": False, "error": "drag requires resolvable source and destination targets"}
            res = await self._desktop._execute_tool(
                "mouse_control",
                {
                    "action": "drag",
                    "x": int(src["x"]),
                    "y": int(src["y"]),
                    "end_x": int(dst["x"]),
                    "end_y": int(dst["y"]),
                },
                ["automation:input"],
                correlation_id,
            )
            return {"success": res.get("success", False), "error": res.get("error"), "method": "drag"}

        if action_type == "browser":
            return await self._execute_browser_step(target, params)

        if action_type == "tool":
            tool_name = str(params.get("tool_name") or "").strip()
            tool_args = dict(params.get("tool_arguments") or {})
            if not tool_name:
                return {"success": False, "error": "tool action missing tool_name"}
            tool = tool_registry.get_tool(tool_name)
            if not tool:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}
            manifest = getattr(tool, "manifest", None)
            if manifest:
                for perm in getattr(manifest, "required_permissions", []):
                    if not consent_manager.is_granted(perm):
                        return {"success": False, "error": f"Consent required: {perm}"}
                if getattr(manifest, "requires_approval", False):
                    tool_args["approved"] = True
            t_res = await tool_registry.invoke(
                tool_name=tool_name,
                arguments=tool_args,
                correlation_id=correlation_id,
            )
            audit_logger.log_tool(
                tool=tool_name,
                success=t_res.success,
                duration_ms=t_res.execution_time_ms,
                correlation_id=correlation_id,
            )
            return {
                "success": t_res.success,
                "result": t_res.result,
                "error": t_res.error,
                "method": f"tool:{tool_name}",
            }

        return {"success": False, "error": f"Unsupported action_type: {action_type}"}

    async def _resolve_dynamic_target(
        self,
        target: Dict[str, Any],
        observation: DesktopObservation,
        window_title: Optional[str],
        force_vision: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a target semantically using UIA -> Blueprint -> OCR -> Vision (zero hardcoded coordinates)."""
        query = str(
            target.get("text")
            or target.get("description")
            or target.get("automation_id")
            or ""
        ).strip()
        if not query:
            if target.get("x") is not None and target.get("y") is not None:
                return {"method": "observed_coords", "x": int(target["x"]), "y": int(target["y"])}
            return None

        if force_vision and self._vision_factory and observation.screenshot_base64:
            try:
                elems = await self._vision_factory.find_elements(observation.screenshot_base64, query)
                for elem in elems:
                    bbox = elem.get("bbox")
                    if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                        return {
                            "method": "vision",
                            "x": observation.origin_x + int(bbox["left"]) + int(bbox["width"]) // 2,
                            "y": observation.origin_y + int(bbox["top"]) + int(bbox["height"]) // 2,
                            "bbox": bbox,
                        }
            except Exception as e:
                logger.debug("Forced vision lookup failed: %s", e)

        resolved = await self._desktop.resolve_target(
            target_description=query,
            observation=observation,
            window_title=window_title,
        )
        if resolved:
            return resolved

        if target.get("x") is not None and target.get("y") is not None:
            return {"method": "observed_coords", "x": int(target["x"]), "y": int(target["y"])}
        return None

    async def _execute_browser_step(
        self, target: Dict[str, Any], params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a live Playwright browser automation step."""
        browser_auto = await self._ensure_browser()
        baction = str(params.get("browser_action") or "navigate").lower()
        target_desc = str(target.get("text") or target.get("description") or "")

        if baction == "navigate":
            url = str(params.get("url") or "").strip()
            if not url:
                return {"success": False, "error": "browser navigate requires url"}
            res = await browser_auto.navigate_and_explore(url, target_desc or url)
            return {"success": bool(res.get("success", True)), "result": res, "error": res.get("error"), "method": "browser:navigate"}

        if baction == "click":
            res = await browser_auto.find_and_click(target_desc)
            return {"success": bool(res.get("success", False)), "result": res, "error": res.get("error"), "method": "browser:click"}

        if baction == "type":
            res = await browser_auto.type_text(target_desc, str(params.get("text") or ""))
            return {"success": bool(res.get("success", False)), "result": res, "error": res.get("error"), "method": "browser:type"}

        if baction == "press_key":
            res = await browser_auto.browser.press_key(str(params.get("key") or "Enter"))
            return {"success": bool(res.get("success", False)), "result": res, "error": res.get("error"), "method": "browser:key"}

        if baction == "scroll":
            res = await browser_auto.browser.scroll(
                direction=str(params.get("direction") or "down"),
                amount=int(params.get("scroll_amount", 400)),
            )
            return {"success": bool(res.get("success", False)), "result": res, "error": res.get("error"), "method": "browser:scroll"}

        if baction == "extract":
            res = await browser_auto.extract_data(target_desc or "page summary")
            return {"success": bool(res.get("success", True)), "result": res, "error": res.get("error"), "method": "browser:extract"}

        return {"success": False, "error": f"Unsupported browser_action: {baction}"}

    # ------------------------------------------------------------------
    # Verification, Risk Assessment & Helpers
    # ------------------------------------------------------------------

    async def _verify_step_outcome(
        self,
        action_type: str,
        exec_res: Dict[str, Any],
        pre_obs: DesktopObservation,
        post_obs: DesktopObservation,
        expected_ver: Dict[str, Any],
    ) -> tuple[bool, str]:
        """Verify action effect using hybrid structural, filesystem, frame-hash, and vision signals."""
        if not exec_res.get("success"):
            return False, str(exec_res.get("error") or "Execution failed")

        vtype = str(expected_ver.get("type") or "screen_changed").lower()
        vval = str(expected_ver.get("value") or "").strip()

        if vtype in ("none", "wait"):
            return True, "No external state change required"

        if vtype == "window_exists" and vval:
            matched = any(vval.lower() in (w.get("title") or "").lower() for w in post_obs.windows)
            return matched, f"Window '{vval}' {'present' if matched else 'not found'}"

        if vtype == "title_contains" and vval:
            title = (post_obs.active_window or {}).get("title") or ""
            matched = vval.lower() in title.lower()
            return matched, f"Active title='{title}'"

        if vtype == "element_exists" and vval:
            matched = any(
                vval.lower() in f"{c.get('title','')} {c.get('auto_id','')}".lower()
                for c in post_obs.uia_controls
            )
            return matched, f"Element '{vval}' {'visible' if matched else 'missing'}"

        if vtype == "file_exists" and vval:
            exists = os.path.exists(os.path.expandvars(os.path.expanduser(vval)))
            return exists, f"File '{vval}' {'exists' if exists else 'not found'}"

        if vtype == "text_present" and vval:
            ver_res = await self._desktop.verify_text_on_screen(
                vval, window_title=(post_obs.active_window or {}).get("title")
            )
            return ver_res.success, f"OCR text '{vval}' {'found' if ver_res.success else 'missing'}"

        if (vtype == "visual_check" or post_obs.screen_changed) and self._vision_factory:
            if pre_obs.screenshot_base64 and post_obs.screenshot_base64 and vval:
                try:
                    vr = await self._vision_factory.verify_action(
                        before_screenshot=pre_obs.screenshot_base64,
                        after_screenshot=post_obs.screenshot_base64,
                        expected_change=vval,
                    )
                    if vr.get("verified"):
                        return True, str(vr.get("reasoning") or "Vision confirmed change")
                    return False, str(vr.get("reasoning") or "Vision did not confirm expected change")
                except Exception:
                    pass

        if action_type in ("tool", "launch_app", "focus_window"):
            return True, f"{action_type} succeeded"

        if post_obs.screen_changed:
            return True, "Screen visual/window state changed"

        return True, "Action dispatched (no frame hash delta detected)"

    async def _verify_goal_completion(
        self,
        goal: str,
        decision: Dict[str, Any],
        obs: DesktopObservation,
        expected_ver: Dict[str, Any],
    ) -> tuple[bool, str]:
        """Confirm goal completion against filesystem/window/vision evidence."""
        vtype = str(expected_ver.get("type") or "").lower()
        vval = str(expected_ver.get("value") or "").strip()
        if vtype == "file_exists" and vval:
            if not os.path.exists(os.path.expandvars(os.path.expanduser(vval))):
                return False, f"Expected file '{vval}' does not exist yet"

        if self._vision_factory and obs.screenshot_base64 and vtype == "visual_check":
            try:
                ans = await self._vision_factory.analyze_screen(
                    obs.screenshot_base64,
                    f"Has the following goal been visually completed? Goal: {goal}. Condition: {vval or 'visible completion'}. Reply YES or NO and 1-sentence reason.",
                )
                ans_text = (getattr(ans, "description", None) or str(ans) if ans is not None else "").strip()
                if ans_text.upper().startswith("NO"):
                    return False, ans_text[:300]
            except Exception:
                pass
        return True, str(decision.get("final_summary") or "Goal achieved")

    def _assess_step_risk(
        self,
        action_type: str,
        target: Dict[str, Any],
        params: Dict[str, Any],
        model_flagged: bool,
        model_risk_reason: str,
        subtask_approved: bool,
        approved_digests: Set[str],
        action_digest: str,
    ) -> tuple[bool, str, str]:
        """Fail-closed risk evaluation for autonomous steps."""
        if action_digest in approved_digests:
            return False, "low", ""

        # Critical/destructive checks apply EVEN IF overall workflow was approved
        if action_type == "tool":
            tname = str(params.get("tool_name") or "")
            if tname in self.CRITICAL_TOOLS:
                return True, "critical", f"High-risk tool execution '{tname}' requires explicit approval"
            if tname in self.HIGH_RISK_TOOLS and not subtask_approved:
                return True, "high", f"Tool '{tname}' modifies files/system state"

        if action_type == "type":
            txt = str(params.get("text") or "").lower()
            if any(k in txt for k in ("rm -rf", "del /", "format ", "remove-item", "reg delete")):
                return True, "critical", "Potentially destructive command text requires explicit approval"

        if action_type == "hotkey":
            keys_low = "+".join(str(k).lower() for k in (params.get("keys") or []))
            if "alt+f4" in keys_low and not subtask_approved:
                return True, "medium", "Closing active window with Alt+F4 requires approval"

        if model_flagged and not subtask_approved:
            return True, "high", model_risk_reason or f"Step '{action_type}' flagged for user approval"

        if not subtask_approved and action_type not in ("wait", "verify", "focus_window"):
            return True, "medium", f"Action '{action_type}' requires explicit approval"

        return False, "low", ""

    @staticmethod
    def _compute_action_digest(action_type: str, target: Dict[str, Any], params: Dict[str, Any]) -> str:
        """Deterministic digest identifying a specific proposed action for exact approval matching."""
        raw = json.dumps({"a": action_type, "t": target, "p": params}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _build_resume_state(
        step_index: int,
        history: List[Dict[str, Any]],
        approved_digests: Set[str],
        current_window_hint: Optional[str],
        pending_action: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Serialize autonomous execution state for checkpointing/pause/approval resume."""
        state: Dict[str, Any] = {
            "step_index": step_index,
            "history": history[-30:],
            "approved_digests": sorted(approved_digests),
            "current_window_hint": current_window_hint,
        }
        if pending_action:
            state["pending_action"] = pending_action
        return state

    async def _get_browser_state_if_active(self, execution_mode: str) -> Optional[Dict[str, Any]]:
        """Inspect active Playwright page state if browser session is already active or browser mode requested."""
        if self._browser is None and execution_mode != "autonomous_browser":
            return None
        if self._browser is None:
            return {"active": False, "note": "Browser not yet navigated"}
        try:
            page = self._browser.browser.page if self._browser.browser else None
            if not page:
                return {"active": False}
            content = await self._browser.browser.get_page_content()
            return {
                "active": True,
                "url": getattr(page, "url", None),
                "title": (content or {}).get("title") if isinstance(content, dict) else None,
                "text_excerpt": str((content or {}).get("text", ""))[:800] if isinstance(content, dict) else "",
            }
        except Exception:
            return None

    def _get_blueprint_hints(self, process_name: Optional[str]) -> List[Dict[str, Any]]:
        """Read learned BlueprintManager controls for active app."""
        if not process_name or not getattr(self._desktop, "_blueprint_manager", None):
            return []
        try:
            bp = self._desktop._blueprint_manager.store.get_blueprint_for_app(process_name)
            if not bp or not isinstance(bp.elements, dict):
                return []
            return [
                {
                    "name": el.name,
                    "type": el.element_type.value if hasattr(el.element_type, "value") else str(el.element_type),
                    "auto_id": el.selectors.get("auto_id") if isinstance(el.selectors, dict) else None,
                    "title": el.selectors.get("title") if isinstance(el.selectors, dict) else el.name,
                }
                for el in list(bp.elements.values())[:12]
            ]
        except Exception:
            return []

    async def _learn_blueprint_step(
        self,
        active_window: Dict[str, Any],
        action_type: str,
        target: Dict[str, Any],
        resolved: Dict[str, Any],
    ) -> None:
        """Persist successful UI discoveries back into BlueprintManager."""
        bm = getattr(self._desktop, "_blueprint_manager", None)
        proc = active_window.get("process_name")
        title = active_window.get("title")
        if not bm or not proc or not title:
            return
        try:
            bp = await bm.get_or_create_blueprint(
                app_name=proc.replace(".exe", ""),
                process_name=proc,
                window_title=title,
            )
            ident = dict(resolved.get("control_identifier") or {})
            if not ident:
                if target.get("automation_id"):
                    ident["auto_id"] = target["automation_id"]
                if target.get("text") or target.get("description"):
                    ident["title"] = target.get("text") or target.get("description")
                if target.get("control_type"):
                    ident["control_type"] = target["control_type"]
            if not ident:
                return
            await bm.learn_from_interaction(
                blueprint=bp,
                window_title=title,
                action=action_type,
                element_identifier=ident,
                success=True,
                details={"bbox": resolved.get("bbox"), "method": resolved.get("method")},
            )
        except Exception as e:
            logger.debug("Blueprint learn skipped: %s", e)

    async def _load_memory_hints(self, goal: str) -> List[str]:
        """Load relevant procedural/episodic memories if permitted."""
        hints: List[str] = []
        if not consent_manager.is_granted("memory:read"):
            return hints
        try:
            query = MemorySearchQuery(
                query=goal,
                tiers=[MemoryTier.PROCEDURAL, MemoryTier.EPISODIC],
                limit=3,
            )
            result = await memory_manager.search(query)
            for entry in getattr(result, "entries", []) or []:
                content = getattr(entry, "content", None)
                if content:
                    hints.append(str(content)[:250])
        except Exception:
            pass
        return hints

    async def _record_completion_memory(
        self,
        task_context: Any,
        goal: str,
        summary: str,
        history: List[Dict[str, Any]],
        obs: Optional[DesktopObservation],
    ) -> None:
        """Store completed autonomous workflow summary in episodic/procedural memory when permitted."""
        if not consent_manager.is_granted("memory:write"):
            return
        try:
            steps_brief = " -> ".join(h.get("step_goal", h.get("action_type", "")) for h in history[:10])
            await memory_manager.save(
                content=f"Goal: {goal}\nSteps: {steps_brief}\nOutcome: {summary}",
                tier=MemoryTier.EPISODIC,
                metadata={
                    "task_id": getattr(task_context, "task_id", None),
                    "type": "autonomous_execution",
                    "steps": len(history),
                    "window": (obs.active_window or {}).get("title") if obs and obs.active_window else None,
                },
                tags=["autonomous_execution"],
            )
        except Exception as e:
            logger.debug("Memory save skipped: %s", e)

    async def _emit_step_event(
        self,
        task_context: Any,
        subtask_id: str,
        step_index: int,
        max_steps: int,
        action_type: str,
        step_goal: str,
        phase: str,
        correlation_id: str,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]],
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit step-level telemetry on event bus and invoke progress callback."""
        payload = {
            "task_id": getattr(task_context, "task_id", None),
            "subtask_id": subtask_id,
            "step": step_index,
            "max_steps": max_steps,
            "action_type": action_type,
            "step_goal": step_goal,
            "phase": phase,
            **(extra or {}),
        }
        await event_bus.publish_sync(
            "task.step",
            "autonomous_executor",
            payload,
            correlation_id=correlation_id,
        )
        if progress_callback:
            try:
                await progress_callback(subtask_id, payload)
            except Exception as e:
                logger.debug("Step progress callback error: %s", e)

    @staticmethod
    def _parse_json_object(content: str) -> Optional[Dict[str, Any]]:
        """Extract and parse a JSON object cleanly from LLM response text."""
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        text = match.group()
        # 1. Try parsing directly first (preserves valid escaped Windows paths)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        # 2. If direct parse failed, attempt to escape stray unescaped Windows backslashes
        try:
            fixed = re.sub(r'(?<!\\)\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', text)
            data = json.loads(fixed)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        return None


autonomous_executor = AutonomousTaskExecutor()
