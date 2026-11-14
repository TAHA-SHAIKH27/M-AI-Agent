"""
Universal Desktop Automation Engine for M AI Assistant.

Provides a unified, application-agnostic interface for Windows desktop automation:
- Application lifecycle management (launch, close, list)
- Window management (focus, state, enumeration)
- Input automation (mouse, keyboard, shortcuts)
- UI Automation for accessible controls
- Visual verification (OCR, screenshots, text search)
- State observation and verification
- Screenshot-first visual intelligence (NVIDIA NIM + Gemini)
"""

import asyncio
import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from enum import Enum

from m_core.config import settings
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.model_providers import provider_factory
from m_core.model_providers.vision_provider import (
    get_vision_factory, VisionConfig, VisionAction, ScreenAnalysis,
    VisionProviderType
)
from .blueprint import BlueprintManager

logger = logging.getLogger(__name__)


class ApplicationState(Enum):
    """Application running state."""
    NOT_RUNNING = "not_running"
    STARTING = "starting"
    RUNNING = "running"
    FOCUSED = "focused"
    MINIMIZED = "minimized"
    MAXIMIZED = "maximized"
    NOT_RESPONDING = "not_responding"
    CLOSING = "closing"


class InteractionMethod(Enum):
    """Method used for interaction."""
    UI_AUTOMATION = "ui_automation"
    MOUSE_KEYBOARD = "mouse_keyboard"
    OCR_BASED = "ocr_based"
    SHORTCUT = "shortcut"


@dataclass
class WindowInfo:
    """Information about a window."""
    hwnd: int
    title: str
    class_name: str
    process_id: int
    process_name: str
    rect: Dict[str, int]
    width: int
    height: int
    is_visible: bool
    is_minimized: bool
    is_maximized: bool
    state: ApplicationState = ApplicationState.RUNNING


@dataclass
class ApplicationInfo:
    """Information about an application."""
    name: str
    process_name: str
    executable_path: Optional[str] = None
    process_ids: List[int] = field(default_factory=list)
    windows: List[WindowInfo] = field(default_factory=list)
    state: ApplicationState = ApplicationState.NOT_RUNNING


@dataclass
class InteractionResult:
    """Result of an interaction attempt."""
    success: bool
    method_used: InteractionMethod
    details: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    verification_passed: bool = False
    screenshot_path: Optional[str] = None
    ocr_text: Optional[str] = None


@dataclass
class DesktopObservation:
    """Structured snapshot of desktop state with adaptive capture metadata."""
    timestamp: float
    active_window: Optional[Dict[str, Any]] = None
    windows: List[Dict[str, Any]] = field(default_factory=list)
    uia_controls: List[Dict[str, Any]] = field(default_factory=list)
    screenshot_base64: Optional[str] = None
    screenshot_path: Optional[str] = None
    capture_mode: str = "full"
    origin_x: int = 0
    origin_y: int = 0
    width: int = 0
    height: int = 0
    frame_hash: Optional[str] = None
    screen_changed: bool = True


class DesktopAutomation:
    """
    Universal desktop automation engine for Windows 11.
    
    Provides application-agnostic desktop interaction capabilities.
    """
    
    def __init__(self, user_id: str = "default", evidence_dir: Optional[Path] = None):
        self.user_id = user_id
        self.evidence_dir = evidence_dir or (settings.data_dir / "evidence" / f"desktop_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        self._screenshot_count = 0
        self._initialized = False
        # Visual intelligence & blueprints
        self._vision_factory = None
        self._vision_config = VisionConfig()
        self._blueprint_manager: Optional[BlueprintManager] = None
        # Adaptive capture tracking
        self._last_frame_hash: Optional[str] = None
        self._last_window_signature: Optional[str] = None
        self._unchanged_streak: int = 0
    
    async def initialize(self) -> None:
        """Initialize the automation engine.
        
        Does NOT grant permissions automatically - permissions must be explicitly
        granted by the user through the consent system before sensitive operations.
        """
        if self._initialized:
            return
        
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._initialized = True
        logger.info("DesktopAutomation initialized (permissions require explicit consent)")

        try:
            self._blueprint_manager = BlueprintManager()
        except Exception as e:
            logger.warning(f"Failed to initialize BlueprintManager: {e}")
        
        # Reuse shared vision factory from provider_factory when available
        try:
            self._vision_factory = await provider_factory.get_vision_factory()
            if not self._vision_factory:
                self._vision_factory = await get_vision_factory(self._vision_config)
            if self._vision_factory and self._vision_factory._current_provider:
                logger.info(f"Visual intelligence enabled: {self._vision_factory._current_provider.provider_name}")
            else:
                logger.warning("Visual intelligence disabled: no vision providers available")
        except Exception as e:
            logger.warning(f"Failed to initialize visual intelligence: {e}")
    
    def _get_correlation_id(self, prefix: str = "auto") -> str:
        """Generate a correlation ID for tracking."""
        return f"{prefix}-{int(time.time() * 1000)}"
    
    async def _execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        permissions: List[str],
        correlation_id: Optional[str] = None,
        require_explicit_approval: bool = False,
    ) -> Dict[str, Any]:
        """Execute a tool with proper consent and approval enforcement."""
        if not self._initialized:
            await self.initialize()
        
        cid = correlation_id or self._get_correlation_id(tool_name)
        tool = tool_registry.get_tool(tool_name)
        
        all_granted = all(consent_manager.is_granted(perm) for perm in permissions)
        manifest = tool.manifest
        requires_approval = (
            manifest.get("requires_approval", False)
            if isinstance(manifest, dict)
            else getattr(manifest, "requires_approval", False)
        )
        
        tool_args = dict(arguments)
        has_caller_approval = bool(tool_args.get("approval_token") or tool_args.get("approved"))
        if all_granted and requires_approval and not require_explicit_approval and not has_caller_approval:
            tool_args["approval_token"] = f"auto-{self.user_id}-{cid}"
        
        ctx = ToolExecutionContext(
            tool_name=tool_name,
            arguments=tool_args,
            correlation_id=cid,
            user_id=self.user_id,
            permissions_granted=permissions,
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        return {
            "success": result.success,
            "result": result.result,
            "error": result.error,
            "error_code": result.error_code
        }
    
    # ============================================================
    # Application Lifecycle
    # ============================================================
    
    async def launch_application(self, app_name: str, args: str = "", 
                                wait_for_window: bool = True, timeout: float = 30.0) -> InteractionResult:
        """
        Launch an application and optionally wait for its main window.
        
        Args:
            app_name: Application name (e.g., 'notepad', 'word', 'excel', 'chrome')
            args: Command line arguments
            wait_for_window: Whether to wait for main window to appear
            timeout: Maximum time to wait for window
            
        Returns:
            InteractionResult with process info and window details
        """
        cid = self._get_correlation_id("launch")
        
        # Take pre-launch screenshot
        pre_screenshot = await self._capture_screenshot("pre_launch")
        
        # Launch the application
        result = await self._execute_tool("app_launch", {
            "app_name": app_name,
            "arguments": args,
        }, ["app:launch"], cid)
        
        if not result["success"]:
            return InteractionResult(
                success=False, method_used=InteractionMethod.SHORTCUT,
                error=result.get("error", "Launch failed")
            )
        
        pid = result["result"].get("process_id")
        process_name = result["result"].get("process_name")
        
        # Wait for window if requested
        window_info = None
        if wait_for_window and pid:
            window_info = await self._wait_for_window(pid, process_name, timeout)
        
        # Take post-launch screenshot
        post_screenshot = await self._capture_screenshot("post_launch")
        
        return InteractionResult(
            success=True,
            method_used=InteractionMethod.SHORTCUT,
            details={
                "process_id": pid,
                "process_name": process_name,
                "window": window_info.__dict__ if window_info else None,
                "pre_screenshot": pre_screenshot,
                "post_screenshot": post_screenshot
            }
        )
    
    async def close_application(self, identifier: Union[str, int], 
                               force: bool = False) -> InteractionResult:
        """
        Close an application by process name, window title, or PID.
        
        Args:
            identifier: Process name, window title, or PID
            force: Force close if normal close fails
            
        Returns:
            InteractionResult
        """
        cid = self._get_correlation_id("close")
        
        # Determine identifier type
        if isinstance(identifier, int):
            arguments = {"action": "close", "process_id": identifier}
        elif identifier.endswith(".exe") or "\\" in identifier:
            arguments = {"action": "close", "process_name": identifier}
        else:
            arguments = {"action": "close", "process_name": identifier}
        
        result = await self._execute_tool("app_control", arguments, ["app:control"], cid)
        
        # Verify closure
        await asyncio.sleep(1)
        still_running = await self._is_application_running(identifier)
        
        if still_running and force:
            # Force kill via taskkill
            import subprocess
            if isinstance(identifier, int):
                subprocess.run(["taskkill", "/F", "/PID", str(identifier)], capture_output=True)
            else:
                subprocess.run(["taskkill", "/F", "/IM", identifier], capture_output=True)
            await asyncio.sleep(1)
            still_running = await self._is_application_running(identifier)
        
        return InteractionResult(
            success=not still_running,
            method_used=InteractionMethod.SHORTCUT,
            details={"force_used": force, "was_running": still_running}
        )
    
    async def close_all_instances(self, process_name: str) -> InteractionResult:
        """
        Close all instances of an application by process name.
        
        Args:
            process_name: Process name (e.g., 'notepad.exe')
            
        Returns:
            InteractionResult with details of closed instances
        """
        # Use taskkill to force close all instances
        import subprocess
        result = subprocess.run(["taskkill", "/F", "/IM", process_name], capture_output=True, text=True)
        logger.debug("taskkill %s rc=%s stdout=%s", process_name, result.returncode, result.stdout.strip())
        await asyncio.sleep(1)
        
        # Verify all closed
        tool = tool_registry.get_tool('window_list')
        ctx = ToolExecutionContext(
            tool_name='window_list', arguments={'filter_process': process_name.replace('.exe', '')},
            correlation_id='verify-closed', user_id=self.user_id, permissions_granted=['app:control'],
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        remaining = result.result.get("total", 0) if result.success else 0
        
        # If still windows remain, try to close each individually
        closed_count = 0
        if remaining > 0:
            tool = tool_registry.get_tool('window_list')
            ctx = ToolExecutionContext(
                tool_name='window_list', arguments={'filter_process': process_name.replace('.exe', '')},
                correlation_id='list-windows', user_id=self.user_id, permissions_granted=['app:control'],
                consent_manager=consent_manager
            )
            result = await tool.execute(ctx)
            if result.success:
                for w in result.result['windows']:
                    close_result = await self.close_application(w['process_id'], force=True)
                    if close_result.success:
                        closed_count += 1
        
        await asyncio.sleep(1)
        
        # Final verification
        tool = tool_registry.get_tool('window_list')
        ctx = ToolExecutionContext(
            tool_name='window_list', arguments={'filter_process': process_name.replace('.exe', '')},
            correlation_id='final-verify', user_id=self.user_id, permissions_granted=['app:control'],
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        final_remaining = result.result.get("total", 0) if result.success else 0
        
        return InteractionResult(
            success=final_remaining == 0,
            method_used=InteractionMethod.SHORTCUT,
            details={
                "closed_count": closed_count,
                "final_remaining": final_remaining
            }
        )
    
    async def get_application_info(self, identifier: str) -> Optional[ApplicationInfo]:
        """Get comprehensive information about an application."""
        # Get process info
        result = await self._execute_tool("process_list", {
            "filter_name": identifier,
            "include_details": True
        }, ["system:info"])
        
        if not result["success"] or not result["result"]["processes"]:
            return None
        
        proc = result["result"]["processes"][0]
        
        # Get windows
        windows_result = await self._execute_tool("window_list", {
            "filter_process": identifier
        }, ["app:control"])
        
        windows = []
        if windows_result["success"]:
            for w in windows_result["result"]["windows"]:
                windows.append(WindowInfo(
                    hwnd=w["hwnd"],
                    title=w["title"],
                    class_name=w["class_name"],
                    process_id=w["process_id"],
                    process_name=w["process_name"],
                    rect=w.get("rect", {}),
                    width=w.get("width", 0),
                    height=w.get("height", 0),
                    is_visible=w.get("is_visible", False),
                    is_minimized=w.get("is_minimized", False),
                    is_maximized=w.get("is_maximized", False),
                    state=self._determine_window_state(w)
                ))
        
        return ApplicationInfo(
            name=proc["name"],
            process_name=proc["name"],
            process_ids=[proc["pid"]],
            windows=windows,
            state=self._determine_app_state(proc, windows)
        )
    
    # ============================================================
    # Window Management
    # ============================================================
    
    async def focus_window(self, identifier: Union[str, int], 
                          verify: bool = True, is_hwnd: bool = False) -> InteractionResult:
        """
        Focus/activate a window with robust error handling.
        
        Args:
            identifier: Window title, process name, PID, or HWND
            verify: Whether to verify focus was achieved
            is_hwnd: If True, treat integer identifier as HWND instead of PID
            
        Returns:
            InteractionResult with focus status
        """
        cid = self._get_correlation_id("focus")
        
        # Determine identifier type - prefer HWND for reliability
        if isinstance(identifier, int):
            if is_hwnd:
                arguments = {"action": "focus", "hwnd": identifier}
            else:
                arguments = {"action": "focus", "process_id": identifier}
        elif identifier.endswith(".exe"):
            arguments = {"action": "focus", "process_name": identifier}
        else:
            # Assume it's a window title - but this is fragile, prefer HWND/PID
            arguments = {"action": "focus", "window_title": identifier}
        
        result = await self._execute_tool("app_control", arguments, ["app:control"], cid)
        
        if not result["success"]:
            # If focus failed with title, try to find by PID if we have one
            if not isinstance(identifier, int) and not identifier.endswith(".exe"):
                # Try to find the window by PID from our tracked windows
                pass  # Could implement PID tracking here
            return InteractionResult(
                success=False, method_used=InteractionMethod.MOUSE_KEYBOARD,
                error=result.get("error", "Focus failed")
            )
        
        # Verify focus if requested
        if verify:
            await asyncio.sleep(0.3)
            # For HWND verification, we need to check differently
            if isinstance(identifier, int) and is_hwnd:
                import win32gui
                verified = win32gui.GetForegroundWindow() == identifier
            else:
                verified = await self._verify_foreground_window(identifier)
            return InteractionResult(
                success=result["success"],
                method_used=InteractionMethod.MOUSE_KEYBOARD,
                details=result["result"],
                verification_passed=verified
            )
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            details=result["result"]
        )
    
    def _find_saved_file(self, filename: str, target_dir: Optional[str] = None) -> tuple[Optional[str], List[str]]:
        """Search common user directories for a newly saved file."""
        search_dirs: List[Path] = []
        if target_dir:
            search_dirs.append(Path(target_dir))
        home = Path.home()
        for rel in ("Desktop", "Documents", "Downloads", "OneDrive/Desktop", "OneDrive/Documents"):
            p = home / rel
            if p.exists():
                search_dirs.append(p)
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            up = Path(user_profile)
            for rel in ("Desktop", "Documents", "OneDrive/Desktop"):
                p = up / rel
                if p.exists():
                    search_dirs.append(p)

        candidates: List[str] = []
        target_path = Path(filename)
        if target_path.is_absolute():
            candidates.append(str(target_path))
        base_name = target_path.name
        for d in search_dirs:
            candidates.append(str(d / base_name))
            if d.is_dir():
                try:
                    for sub in d.iterdir():
                        if sub.is_dir() and (sub / base_name).exists():
                            candidates.append(str(sub / base_name))
                except OSError:
                    pass

        candidates = list(dict.fromkeys(candidates))
        for path in candidates:
            if os.path.exists(path):
                return path, candidates
        return None, candidates

    async def save_file(self, window_title: str, filename: str, 
                       target_dir: str = None) -> InteractionResult:
        """
        Save a file using the application's Save As dialog.
        
        Args:
            window_title: Title of the application window (e.g., "Notepad")
            filename: Name of the file to save
            target_dir: Optional target directory (not fully supported yet)
            
        Returns:
            InteractionResult with save status and file path
        """
        import os
        cid = self._get_correlation_id("save")
        
        # Check if window title indicates file already has a name (not "Untitled")
        is_untitled = "untitled" in window_title.lower()
        
        # Press Ctrl+Shift+S for Save As (works regardless of whether file has name)
        # or Ctrl+S if untitled
        if is_untitled:
            hotkey = ['ctrl', 's']
        else:
            hotkey = ['ctrl', 'shift', 's']  # Save As
        
        result = await self.send_hotkey(hotkey)
        if not result.success:
            return InteractionResult(
                success=False, method_used=InteractionMethod.SHORTCUT,
                error=f"Failed to open Save As dialog ({'+'.join(hotkey)})"
            )
        
        # Wait for Save As dialog to appear with retries
        save_dialog = None
        dialog_title = None
        dialog_hwnd = None
        
        # Use win32gui directly for more reliable dialog detection
        import win32gui
        
        for attempt in range(10):
            await asyncio.sleep(0.5)  # Wait for dialog
            
            # First try window_list tool - case-insensitive match
            result = await self._execute_tool("window_list", {"filter_title": "Save"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                for w in result["result"]["windows"]:
                    if "save" in w["title"].lower():
                        save_dialog = w
                        dialog_title = w["title"]
                        dialog_hwnd = w["hwnd"]
                        logger.debug("Found Save As dialog via window_list: %s (attempt %d)", dialog_title, attempt + 1)
                        break
                if save_dialog:
                    break
            
            # Fallback: use win32gui directly
            def enum_windows(hwnd, results):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title and ('save as' in title.lower() or 'save' == title.lower().strip()):
                        results.append((hwnd, title))
                return True
            
            results = []
            win32gui.EnumWindows(enum_windows, results)
            if results:
                dialog_hwnd, dialog_title = results[0]
                logger.debug("Found Save As dialog via win32gui: %s HWND=%s (attempt %d)", dialog_title, dialog_hwnd, attempt + 1)
                break
        
        if not dialog_hwnd:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error="Save As dialog not found after retries"
            )
        
        # Focus Save As dialog using HWND
        focus_result = await self.focus_window(dialog_hwnd, verify=True)
        if not focus_result.success:
            logger.warning("Could not verify focus on Save As dialog: %s", dialog_title)
        
        await asyncio.sleep(0.5)
        
        save_target_text = str(Path(target_dir) / filename) if target_dir and not Path(filename).is_absolute() else filename
        filename_ctrl = {"type": "Edit", "title": "File name:", "index": 0}
        result = await self._execute_tool("window_interact", {
            "action": "type", "window_title": dialog_title, "control_identifier": filename_ctrl, 
            "text": save_target_text
        }, ["automation:window"])
        
        if not result["success"]:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error=f"Failed to type filename: {result.get('error')}"
            )
        
        await asyncio.sleep(0.5)
        
        # Press Enter to save (more reliable than clicking Save button)
        result = await self.send_hotkey(['enter'])
        if not result.success:
            return InteractionResult(
                success=False, method_used=InteractionMethod.SHORTCUT,
                error=f"Failed to press Enter to save: {result.get('error')}"
            )
        
        # Handle potential "Confirm Save As" dialog - need to select "Yes" to replace
        await asyncio.sleep(1)
        confirm_dialog = None
        for attempt in range(5):
            await asyncio.sleep(0.5)
            result = await self._execute_tool("window_list", {"filter_title": "Confirm"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                for w in result["result"]["windows"]:
                    if "confirm" in w["title"].lower() and "save" in w["title"].lower():
                        confirm_dialog = w
                        confirm_title = w["title"]
                        logger.debug("Found Confirm Save As dialog: %s", confirm_title)
                        break
                if confirm_dialog:
                    break
        
        if confirm_dialog:
            # Use UI Automation to find and click the "Yes" button
            yes_button = {"type": "Button", "title": "Yes", "index": 0}
            result = await self._execute_tool("window_interact", {
                "action": "click", "window_title": confirm_title, 
                "control_identifier": yes_button
            }, ["automation:window"])
            if not result["success"]:
                # Fallback: try Tab to navigate to Yes, then Enter
                result = await self.send_hotkey(['tab'])
                if result.success:
                    await asyncio.sleep(0.2)
                    await self.send_hotkey(['enter'])
        
        await asyncio.sleep(2)  # Wait for save to complete
        
        saved_path, possible_paths = self._find_saved_file(filename, target_dir)
        
        if saved_path:
            return InteractionResult(
                success=True,
                method_used=InteractionMethod.UI_AUTOMATION,
                details={"file_path": saved_path, "dialog_title": dialog_title}
            )
        else:
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.UI_AUTOMATION,
                error=f"File not found after save. Checked {len(possible_paths)} locations.",
                details={"dialog_title": dialog_title, "searched_paths": possible_paths}
            )
    
    async def save_file_fallback(self, window_title: str, filename: str, 
                                 target_dir: str = None) -> InteractionResult:
        """
        Fallback save method using keyboard shortcuts.
        Uses Alt+Shift+S for Save As, then navigates to Replace button.
        """
        import os
        cid = self._get_correlation_id("save_fallback")
        
        # Press Alt+Shift+S for Save As
        result = await self.send_hotkey(['alt', 'shift', 's'])
        if not result.success:
            return InteractionResult(
                success=False, method_used=InteractionMethod.SHORTCUT,
                error=f"Failed to open Save As dialog (Alt+Shift+S)"
            )
        
        await asyncio.sleep(1)
        
        # Find Save As dialog
        save_dialog = None
        dialog_title = None
        for attempt in range(10):
            await asyncio.sleep(0.5)
            result = await self._execute_tool("window_list", {"filter_title": "Save as"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                save_dialog = result["result"]["windows"][0]
                dialog_title = save_dialog["title"]
                logger.debug("Found Save As dialog: %s", dialog_title)
                break
            result = await self._execute_tool("window_list", {"filter_title": "Save As"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                save_dialog = result["result"]["windows"][0]
                dialog_title = save_dialog["title"]
                logger.debug("Found Save As dialog: %s", dialog_title)
                break
        
        if not dialog_title:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error="Save As dialog not found"
            )
        
        # Focus dialog
        result = await self.focus_window(dialog_title, verify=True)
        
        await asyncio.sleep(0.5)
        
        save_target_text = str(Path(target_dir) / filename) if target_dir and not Path(filename).is_absolute() else filename
        filename_ctrl = {"type": "Edit", "title": "File name:", "index": 0}
        result = await self._execute_tool("window_interact", {
            "action": "type", "window_title": dialog_title, "control_identifier": filename_ctrl, 
            "text": save_target_text
        }, ["automation:window"])
        
        if not result["success"]:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error=f"Failed to type filename: {result.get('error')}"
            )
        
        await asyncio.sleep(0.5)
        
        # Press Enter to save
        result = await self.send_hotkey(['enter'])
        if not result.success:
            return InteractionResult(
                success=False, method_used=InteractionMethod.SHORTCUT,
                error=f"Failed to press Enter to save"
            )
        
        # Handle potential "Confirm Save As" dialog
        await asyncio.sleep(1)
        for attempt in range(5):
            await asyncio.sleep(0.5)
            result = await self._execute_tool("window_list", {"filter_title": "Confirm Save As"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                confirm_dialog = result["result"]["windows"][0]
                confirm_title = confirm_dialog["title"]
                logger.debug("Found Confirm Save As dialog: %s", confirm_title)
                
                # Navigate to "Yes" button using left arrow (default is "No")
                result = await self.send_hotkey(['left'])
                if result.success:
                    await asyncio.sleep(0.2)
                    await self.send_hotkey(['enter'])
                break
        
        await asyncio.sleep(3)  # Wait for save to complete
        
        saved_path, possible_paths = self._find_saved_file(filename, target_dir)
        
        if saved_path:
            return InteractionResult(
                success=True,
                method_used=InteractionMethod.UI_AUTOMATION,
                details={"file_path": saved_path, "dialog_title": dialog_title}
            )
        else:
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.UI_AUTOMATION,
                error=f"File not found after fallback save. Checked {len(possible_paths)} locations.",
                details={"dialog_title": dialog_title, "searched_paths": possible_paths}
            )
    
    # ============================================================
    # New Tab & Window Helpers (Discovered during testing)
    # ============================================================
    
    async def open_new_tab(self, window_title: str) -> InteractionResult:
        """
        Open a new tab in a tabbed application (e.g., Notepad).
        
        Uses UI Automation to click the "Add New Tab" button.
        Discovered: Notepad uses auto_id="AddButton" with title "Add New Tab".
        """
        cid = self._get_correlation_id("new_tab")
        
        # Try multiple selectors for the new tab button
        new_tab_selectors = [
            {"type": "Button", "auto_id": "AddButton", "index": 0},      # Notepad: "Add New Tab"
            {"type": "Button", "title": "Add New Tab", "index": 0},     # Title match
            {"type": "Button", "title": "New tab", "index": 0},         # Alternative title
            {"type": "Button", "title": "+", "index": 0},               # Compact title
            {"type": "Button", "auto_id": "NewTabButton", "index": 0},  # Other apps
        ]
        
        for selector in new_tab_selectors:
            result = await self._execute_tool("window_interact", {
                "action": "click", "window_title": window_title, 
                "control_identifier": selector
            }, ["automation:window"], cid)
            
            if result["success"]:
                await asyncio.sleep(0.5)
                return InteractionResult(
                    success=True,
                    method_used=InteractionMethod.UI_AUTOMATION,
                    details={"selector_used": selector}
                )
        
        return InteractionResult(
            success=False, method_used=InteractionMethod.UI_AUTOMATION,
            error="Could not find new tab button with any known selector"
        )
    
    async def open_new_tab_by_hwnd(self, hwnd: int) -> InteractionResult:
        """
        Open a new tab using window handle (HWND) instead of title.
        More reliable for Notepad and other tabbed applications.
        """
        cid = self._get_correlation_id("new_tab_hwnd")
        
        # Try multiple selectors for the new tab button
        new_tab_selectors = [
            {"type": "Button", "auto_id": "AddButton", "index": 0},      # Notepad: "Add New Tab"
            {"type": "Button", "title": "Add New Tab", "index": 0},     # Title match
            {"type": "Button", "title": "New tab", "index": 0},         # Alternative title
            {"type": "Button", "title": "+", "index": 0},               # Compact title
            {"type": "Button", "auto_id": "NewTabButton", "index": 0},  # Other apps
        ]
        
        for selector in new_tab_selectors:
            result = await self._execute_tool("window_interact", {
                "action": "click", "window_handle": hwnd, 
                "control_identifier": selector
            }, ["automation:window"], cid)
            
            if result["success"]:
                await asyncio.sleep(0.5)
                return InteractionResult(
                    success=True,
                    method_used=InteractionMethod.UI_AUTOMATION,
                    details={"selector_used": selector}
                )
        
        return InteractionResult(
            success=False, method_used=InteractionMethod.UI_AUTOMATION,
            error="Could not find new tab button with any known selector"
        )
    
    async def get_current_window_title(self, process_name: str = "notepad", pid: int = None) -> str:
        """
        Get the current window title for a process.
        Handles dynamic titles (e.g., Notepad adds * prefix when modified).
        If pid is provided, match by PID to avoid stale windows.
        """
        result = await self._execute_tool("window_list", {
            "filter_process": process_name
        }, ["app:control"])
        
        if result["success"] and result["result"]["windows"]:
            for w in result["result"]["windows"]:
                if w["is_visible"] and process_name.lower() in w["title"].lower():
                    # If PID is provided, match by PID to avoid stale windows
                    if pid is not None and w["process_id"] != pid:
                        continue
                    return w["title"]
        return ""
    
    async def ensure_fresh_document(self, window_title: str, process_name: str = "notepad", pid: int = None) -> InteractionResult:
        """
        Ensure we have a fresh, untitled document by opening a new tab.
        Useful for Notepad and similar tabbed editors.
        If pid is provided, use it to track the correct window.
        """
        # Open new tab
        result = await self.open_new_tab(window_title)
        if not result.success:
            return result
        
        # Wait for new tab to settle and get updated title
        await asyncio.sleep(1)
        new_title = await self.get_current_window_title(process_name, pid)
        
        return InteractionResult(
            success=True,
            method_used=InteractionMethod.UI_AUTOMATION,
            details={"new_window_title": new_title}
        )
    
    async def save_file_with_fallback(self, window_title: str, filename: str, 
                                       target_dir: str = None, process_name: str = "notepad", pid: int = None) -> InteractionResult:
        """
        Complete save workflow with automatic fallback.
        Tries primary save_file, then fallback, then ensures fresh document if needed.
        """
        # Try primary save
        result = await self.save_file(window_title, filename, target_dir)
        if result.success:
            return result
        
        # Try fallback
        print(f"   Primary save failed: {result.error}. Trying fallback...")
        result = await self.save_file_fallback(window_title, filename, target_dir)
        if result.success:
            return result
        
        # As last resort, ensure fresh document and retry
        print(f"   Fallback save failed. Ensuring fresh document and retrying...")
        fresh_result = await self.ensure_fresh_document(window_title, process_name, pid)
        if fresh_result.success:
            new_title = fresh_result.details.get("new_window_title", window_title)
            result = await self.save_file_fallback(new_title, filename, target_dir)
        
        return result
    
    async def get_window_state(self, identifier: Union[str, int]) -> Optional[WindowInfo]:
        """Get current state of a window."""
        if isinstance(identifier, int):
            result = await self._execute_tool("window_list", {
                "filter_process": ""
            }, ["app:control"])
            # Filter by PID in results
        else:
            result = await self._execute_tool("window_list", {
                "filter_process": identifier if identifier.endswith(".exe") else ""
            }, ["app:control"])
        
        if result["success"] and result["result"]["windows"]:
            w = result["result"]["windows"][0]
            return WindowInfo(
                hwnd=w["hwnd"],
                title=w["title"],
                class_name=w["class_name"],
                process_id=w["process_id"],
                process_name=w["process_name"],
                rect=w.get("rect", {}),
                width=w.get("width", 0),
                height=w.get("height", 0),
                is_visible=w.get("is_visible", False),
                is_minimized=w.get("is_minimized", False),
                is_maximized=w.get("is_maximized", False),
                state=self._determine_window_state(w)
            )
        return None
    
    async def maximize_window(self, identifier: Union[str, int]) -> InteractionResult:
        """Maximize a window."""
        cid = self._get_correlation_id("maximize")
        if isinstance(identifier, int):
            arguments = {"action": "maximize", "process_id": identifier}
        else:
            arguments = {"action": "maximize", "process_name": identifier}
        return await self._execute_tool("app_control", arguments, ["app:control"], cid)
    
    async def minimize_window(self, identifier: Union[str, int]) -> InteractionResult:
        """Minimize a window."""
        cid = self._get_correlation_id("minimize")
        if isinstance(identifier, int):
            arguments = {"action": "minimize", "process_id": identifier}
        else:
            arguments = {"action": "minimize", "process_name": identifier}
        return await self._execute_tool("app_control", arguments, ["app:control"], cid)
    
    async def restore_window(self, identifier: Union[str, int]) -> InteractionResult:
        """Restore a minimized/maximized window."""
        cid = self._get_correlation_id("restore")
        if isinstance(identifier, int):
            arguments = {"action": "restore", "process_id": identifier}
        else:
            arguments = {"action": "restore", "process_name": identifier}
        return await self._execute_tool("app_control", arguments, ["app:control"], cid)
    
    # ============================================================
    # Input Automation
    # ============================================================
    
    async def type_text(self, text: str, interval: float = 0.02) -> InteractionResult:
        """Type text at current cursor position."""
        cid = self._get_correlation_id("type")
        result = await self._execute_tool("keyboard_input", {
            "action": "type", "text": text, "interval": interval
        }, ["automation:input"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            error=result.get("error")
        )
    
    async def press_key(self, key: str) -> InteractionResult:
        """Press a single key."""
        cid = self._get_correlation_id("press")
        result = await self._execute_tool("keyboard_input", {
            "action": "press", "key": key
        }, ["automation:input"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            error=result.get("error")
        )
    
    async def send_hotkey(self, keys: List[str]) -> InteractionResult:
        """Send a keyboard shortcut (e.g., ['ctrl', 's'])."""
        cid = self._get_correlation_id("hotkey")
        result = await self._execute_tool("keyboard_input", {
            "action": "hotkey", "keys": keys
        }, ["automation:input"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.SHORTCUT,
            details={"keys": keys},
            error=result.get("error")
        )
    
    async def click_at(self, x: int, y: int, button: str = "left", 
                      clicks: int = 1) -> InteractionResult:
        """Click at specific coordinates."""
        cid = self._get_correlation_id("click")
        result = await self._execute_tool("mouse_control", {
            "action": "click", "x": x, "y": y, "button": button, 
            "clicks": clicks
        }, ["automation:input"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            error=result.get("error")
        )
    
    async def move_mouse(self, x: int, y: int, duration: float = 0.5) -> InteractionResult:
        """Move mouse to coordinates smoothly."""
        cid = self._get_correlation_id("move")
        result = await self._execute_tool("mouse_control", {
            "action": "move", "x": x, "y": y, "duration": duration
        }, ["automation:input"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            error=result.get("error")
        )
    
    # ============================================================
    # UI Automation (via window_interact)
    # ============================================================
    
    async def ui_click(self, window_title: str, control_identifier: Dict[str, Any]) -> InteractionResult:
        """Click a UI control using UI Automation."""
        cid = self._get_correlation_id("ui_click")
        result = await self._execute_tool("window_interact", {
            "action": "click", "window_title": window_title,
            "control_identifier": control_identifier
        }, ["automation:window"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.UI_AUTOMATION,
            error=result.get("error")
        )
    
    async def ui_type(self, window_title: str, control_identifier: Dict[str, Any], 
                     text: str) -> InteractionResult:
        """Type into a UI control using UI Automation."""
        cid = self._get_correlation_id("ui_type")
        result = await self._execute_tool("window_interact", {
            "action": "type", "window_title": window_title,
            "control_identifier": control_identifier, "text": text
        }, ["automation:window"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.UI_AUTOMATION,
            error=result.get("error")
        )
    
    async def ui_find_control(self, window_title: str, 
                             control_identifier: Dict[str, Any]) -> InteractionResult:
        """Find a UI control using UI Automation."""
        cid = self._get_correlation_id("ui_find")
        result = await self._execute_tool("window_interact", {
            "action": "find_control", "window_title": window_title,
            "control_identifier": control_identifier
        }, ["automation:window"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.UI_AUTOMATION,
            details=result.get("result", {}),
            error=result.get("error")
        )
    
    async def ui_list_controls(self, window_title: str) -> InteractionResult:
        """List all controls in a window."""
        cid = self._get_correlation_id("ui_list")
        result = await self._execute_tool("window_interact", {
            "action": "list_controls", "window_title": window_title
        }, ["automation:window"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.UI_AUTOMATION,
            details=result.get("result", {}),
            error=result.get("error")
        )
    
    # ============================================================
    # Visual Verification & OCR
    # ============================================================
    
    async def capture_screenshot(self, window_title: str = None, 
                                region: Dict[str, int] = None) -> InteractionResult:
        """Capture a screenshot of screen, window, or region."""
        cid = self._get_correlation_id("capture")
        
        if window_title:
            result = await self._execute_tool("screen_capture", {
                "mode": "window", "window_title": window_title, "output_format": "png"
            }, ["screen:capture"], cid)
        elif region:
            result = await self._execute_tool("screen_capture", {
                "mode": "region", "region": region, "output_format": "png"
            }, ["screen:capture"], cid)
        else:
            result = await self._execute_tool("screen_capture", {
                "mode": "full", "output_format": "png"
            }, ["screen:capture"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.MOUSE_KEYBOARD,
            details=result.get("result", {}),
            screenshot_path=result.get("result", {}).get("image_path"),
            error=result.get("error")
        )
    
    async def ocr_screen(self, window_title: str = None, 
                        region: Dict[str, int] = None) -> InteractionResult:
        """Extract text from screen, window, or region using OCR."""
        cid = self._get_correlation_id("ocr")
        
        if window_title:
            result = await self._execute_tool("screen_ocr", {
                "source": "window", "window_title": window_title, "preprocess": True
            }, ["screen:ocr"], cid)
        elif region:
            result = await self._execute_tool("screen_ocr", {
                "source": "region", "region": region, "preprocess": True
            }, ["screen:ocr"], cid)
        else:
            result = await self._execute_tool("screen_ocr", {
                "source": "screen", "preprocess": True
            }, ["screen:ocr"], cid)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.OCR_BASED,
            details=result.get("result", {}),
            ocr_text=result.get("result", {}).get("text"),
            error=result.get("error")
        )
    
    async def find_text_on_screen(self, search_text: str, 
                                 window_title: str = None,
                                 threshold: float = 0.5) -> InteractionResult:
        """Find text on screen using OCR."""
        cid = self._get_correlation_id("find_text")
        
        args = {
            "method": "text", "search_text": search_text, "threshold": threshold
        }
        if window_title:
            args["window_title"] = window_title
        
        result = await self._execute_tool("visual_find", args, ["screen:find"])
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.OCR_BASED,
            details=result.get("result", {}),
            error=result.get("error")
        )
    
    async def verify_text_present(self, search_text: str, 
                                 window_title: str = None) -> InteractionResult:
        """Verify specific text is present on screen/window."""
        cid = self._get_correlation_id("verify")
        result = await self._execute_tool("visual_verify", {
            "check_type": "text_present", "search_text": search_text,
            "window_title": window_title
        }, ["screen:verify"], cid)
        
        # result["result"] is the dict from visual_verify's _execute_impl
        passed = False
        if result["success"] and result["result"]:
            passed = result["result"].get("passed", False)
        
        return InteractionResult(
            success=result["success"],
            method_used=InteractionMethod.OCR_BASED,
            verification_passed=passed,
            error=result.get("error")
        )
    
    # ============================================================
    # High-Level Workflows
    # ============================================================
    
    async def wait_for_window(self, process_name: str, pid: int = None, 
                             timeout: float = 30.0) -> Optional[WindowInfo]:
        """Wait for application main window to appear."""
        start = time.time()
        while time.time() - start < timeout:
            window = await self.get_window_state(process_name)
            if window and window.is_visible and window.title:
                return window
            await asyncio.sleep(0.5)
        return None
    
    async def _wait_for_window(self, pid: int, process_name: str, 
                              timeout: float) -> Optional[WindowInfo]:
        """Internal method to wait for window by PID."""
        start = time.time()
        while time.time() - start < timeout:
            result = await self._execute_tool("window_list", {
                "filter_process": process_name
            }, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                for w in result["result"]["windows"]:
                    if w["process_id"] == pid and w["is_visible"]:
                        return WindowInfo(
                            hwnd=w["hwnd"], title=w["title"], class_name=w["class_name"],
                            process_id=w["process_id"], process_name=w["process_name"],
                            rect=w.get("rect", {}), width=w.get("width", 0),
                            height=w.get("height", 0), is_visible=w.get("is_visible", False),
                            is_minimized=w.get("is_minimized", False),
                            is_maximized=w.get("is_maximized", False),
                            state=self._determine_window_state(w)
                        )
            await asyncio.sleep(0.5)
        return None
    
    async def _is_application_running(self, identifier: Union[str, int]) -> bool:
        """Check if application is still running."""
        if isinstance(identifier, int):
            result = await self._execute_tool("process_list", {
                "filter_name": "", "include_details": True
            }, ["system:info"])
            if result["success"]:
                return any(p["pid"] == identifier for p in result["result"]["processes"])
        else:
            result = await self._execute_tool("process_list", {
                "filter_name": identifier, "include_details": True
            }, ["system:info"])
            if result["success"]:
                return len(result["result"]["processes"]) > 0
        return False
    
    async def _verify_foreground_window(self, identifier: Union[str, int]) -> bool:
        """Verify the target window is in foreground using direct win32gui calls."""
        import win32gui
        import win32process
        
        fg_hwnd = win32gui.GetForegroundWindow()
        if not fg_hwnd:
            return False
        
        # Check if it matches our target
        if isinstance(identifier, int):
            # Check by PID
            _, fg_pid = win32process.GetWindowThreadProcessId(fg_hwnd)
            return fg_pid == identifier
        elif isinstance(identifier, str) and identifier.endswith(".exe"):
            # Check by process name
            _, fg_pid = win32process.GetWindowThreadProcessId(fg_hwnd)
            try:
                import psutil
                fg_proc = psutil.Process(fg_pid)
                return fg_proc.name().lower() == identifier.lower()
            except:
                return False
        else:
            # Check by window title (substring match)
            title = win32gui.GetWindowText(fg_hwnd)
            return identifier.lower() in title.lower()
    
    # ============================================================
    # Evidence & Verification
    # ============================================================
    
    async def _capture_screenshot(self, step_name: str) -> Optional[str]:
        """Capture and save screenshot for evidence."""
        self._screenshot_count += 1
        name = f"{step_name}_{self._screenshot_count}_{int(time.time())}.png"
        path = self.evidence_dir / name
        
        result = await self._execute_tool("screen_capture", {
            "mode": "full", "output_format": "png", "save_to_file": True
        }, ["screen:capture"])
        
        if result["success"] and "image_path" in result["result"]:
            import shutil
            shutil.copy2(result["result"]["image_path"], path)
            return str(path)
        return None
    
    async def verify_action_result(self, expected: Dict[str, Any], 
                                  window_title: str = None) -> InteractionResult:
        """
        Verify the result of an action by checking screen state.
        
        Args:
            expected: Dictionary with expected state (text, window_title, etc.)
            window_title: Optional window to check
            
        Returns:
            InteractionResult with verification_passed
        """
        checks_passed = 0
        total_checks = 0
        
        # Check for expected text
        if "text" in expected:
            total_checks += 1
            result = await self.verify_text_present(expected["text"], window_title)
            if result.verification_passed:
                checks_passed += 1
        
        # Check for expected window
        if "window_title" in expected:
            total_checks += 1
            window = await self.get_window_state(expected["window_title"])
            if window and expected["window_title"].lower() in window.title.lower():
                checks_passed += 1
        
        # Check for expected file
        if "file_path" in expected:
            total_checks += 1
            path = Path(expected["file_path"])
            if path.exists():
                checks_passed += 1
        
        all_passed = checks_passed == total_checks and total_checks > 0
        
        return InteractionResult(
            success=True,
            method_used=InteractionMethod.OCR_BASED,
            verification_passed=all_passed,
            details={
                "checks_passed": checks_passed,
                "total_checks": total_checks,
                "expected": expected
            }
        )
    
    # ============================================================
    # SCREENSHOT-FIRST VISUAL INTELLIGENCE
    # ============================================================
    
    async def capture_screenshot_with_meta(
        self,
        mode: str = "full",
        window_title: Optional[str] = None,
        save_evidence: bool = True,
    ) -> Dict[str, Any]:
        """Capture screenshot returning base64 plus coordinate origin and dimensions."""
        result = await self._execute_tool("screen_capture", {
            "mode": mode, "output_format": "png", "save_to_file": save_evidence,
            "window_title": window_title
        }, ["screen:capture"])

        if not result.get("success") or not result.get("result"):
            return {"success": False, "error": result.get("error")}

        res = result["result"]
        img_b64 = res.get("image_base64")
        saved_path = None
        if save_evidence and "image_path" in res:
            self._screenshot_count += 1
            name = f"screenshot_{self._screenshot_count}_{int(time.time())}.png"
            dest_path = self.evidence_dir / name
            import shutil
            shutil.copy2(res["image_path"], dest_path)
            saved_path = str(dest_path)
            with open(dest_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

        return {
            "success": bool(img_b64),
            "base64": img_b64,
            "path": saved_path,
            "width": int(res.get("width", 0)),
            "height": int(res.get("height", 0)),
            "origin_x": int(res.get("origin_x", 0)),
            "origin_y": int(res.get("origin_y", 0)),
            "mode": res.get("mode", mode),
        }

    async def capture_screenshot_base64(self, mode: str = "full", window_title: str = None) -> Optional[str]:
        """Capture screenshot and return as base64 string for vision analysis."""
        meta = await self.capture_screenshot_with_meta(mode=mode, window_title=window_title, save_evidence=True)
        return meta.get("base64") if meta.get("success") else None
    
    async def capture_window_screenshot(self, window_title: str) -> Optional[str]:
        """Capture screenshot of a specific window."""
        return await self.capture_screenshot_base64(mode="window", window_title=window_title)

    def _compute_frame_hash(self, screenshot_b64: Optional[str]) -> Optional[str]:
        """Compute perceptual/frame hash to adaptively detect UI changes."""
        if not screenshot_b64:
            return None
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64))).convert("L").resize((16, 16))
            pixels = list(img.getdata())
            avg = sum(pixels) / max(1, len(pixels))
            bits = "".join("1" if p >= avg else "0" for p in pixels)
            return f"{int(bits, 2):064x}"
        except Exception:
            return hashlib.sha256(screenshot_b64.encode("utf-8")).hexdigest()[:32]

    async def _get_foreground_window_info(self) -> Optional[Dict[str, Any]]:
        """Get metadata for the currently focused top-level Windows window."""
        try:
            win_res = await self._execute_tool("window_list", {}, ["app:control"])
            windows = (win_res.get("result") or {}).get("windows", []) if win_res.get("success") else []
            visible = [w for w in windows if w.get("is_visible") and w.get("title")]
            try:
                import win32gui
                fg = win32gui.GetForegroundWindow()
                for w in visible:
                    if w.get("hwnd") == fg:
                        return w
            except Exception:
                pass
            return visible[0] if visible else None
        except Exception:
            return None

    async def observe_desktop(
        self,
        window_title: Optional[str] = None,
        capture_mode: str = "adaptive",
        include_uia: bool = True,
        include_screenshot: bool = True,
        max_uia_controls: int = 40,
    ) -> DesktopObservation:
        """
        Observe current desktop state using adaptive screenshot capture and live UIA tree.

        In 'adaptive' mode: captures the focused target window when stable and unambiguous,
        and automatically broadens to 'full' desktop if focus is unclear, multiple popups/dialogs
        may span outside window bounds, or repeated unchanged frames occurred.
        """
        if not self._initialized:
            await self.initialize()

        win_res = await self._execute_tool("window_list", {}, ["app:control"])
        raw_windows = (win_res.get("result") or {}).get("windows", []) if win_res.get("success") else []
        windows = [w for w in raw_windows if w.get("is_visible") and w.get("title")][:25]

        active_window: Optional[Dict[str, Any]] = None
        if window_title:
            wt_low = window_title.lower()
            active_window = next((w for w in windows if wt_low in (w.get("title") or "").lower()), None)
        if active_window is None:
            try:
                import win32gui
                fg = win32gui.GetForegroundWindow()
                active_window = next((w for w in windows if w.get("hwnd") == fg), None)
            except Exception:
                pass
        if active_window is None and windows:
            active_window = windows[0]

        uia_controls: List[Dict[str, Any]] = []
        if include_uia and active_window and active_window.get("title"):
            try:
                ui_res = await self._execute_tool(
                    "window_interact",
                    {"action": "list_controls", "window_title": active_window["title"]},
                    ["automation:window"],
                )
                if ui_res.get("success") and ui_res.get("result"):
                    raw_ctrls = ui_res["result"].get("controls", [])
                    interactive_types = {
                        "Button", "Edit", "Document", "ComboBox", "CheckBox",
                        "RadioButton", "MenuItem", "TabItem", "Hyperlink", "ListItem", "TreeItem"
                    }
                    for c in raw_ctrls:
                        if not c.get("visible", True):
                            continue
                        rect = c.get("rect") or {}
                        if rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
                            continue
                        if c.get("type") in interactive_types or (c.get("title") or "").strip():
                            uia_controls.append({
                                "type": c.get("type"),
                                "title": (c.get("title") or "")[:120],
                                "auto_id": c.get("auto_id"),
                                "enabled": c.get("enabled", True),
                                "rect": rect,
                                "center_x": rect.get("left", 0) + rect.get("width", 0) // 2,
                                "center_y": rect.get("top", 0) + rect.get("height", 0) // 2,
                            })
                            if len(uia_controls) >= max_uia_controls:
                                break
            except Exception as e:
                logger.debug("UIA observation skipped: %s", e)

        resolved_mode = capture_mode
        target_cap_title = window_title or (active_window.get("title") if active_window else None)
        if capture_mode == "adaptive":
            has_modal = any(
                k in (w.get("title") or "").lower()
                for w in windows[:6]
                for k in ("save", "open", "confirm", "dialog", "warning", "error")
            )
            if target_cap_title and not has_modal and self._unchanged_streak < 2:
                resolved_mode = "window"
            else:
                resolved_mode = "full"

        cap_meta: Dict[str, Any] = {}
        if include_screenshot:
            cap_meta = await self.capture_screenshot_with_meta(
                mode=resolved_mode,
                window_title=target_cap_title if resolved_mode == "window" else None,
                save_evidence=True,
            )
            if not cap_meta.get("success") and resolved_mode != "full":
                cap_meta = await self.capture_screenshot_with_meta(mode="full", save_evidence=True)
                resolved_mode = "full"

        b64 = cap_meta.get("base64")
        frame_hash = self._compute_frame_hash(b64)
        win_sig = f"{active_window.get('hwnd')}:{active_window.get('title')}" if active_window else ""
        changed = (
            frame_hash != self._last_frame_hash
            or win_sig != self._last_window_signature
        )
        self._unchanged_streak = 0 if changed else self._unchanged_streak + 1
        self._last_frame_hash = frame_hash
        self._last_window_signature = win_sig

        return DesktopObservation(
            timestamp=time.time(),
            active_window=active_window,
            windows=windows,
            uia_controls=uia_controls,
            screenshot_base64=b64,
            screenshot_path=cap_meta.get("path"),
            capture_mode=cap_meta.get("mode", resolved_mode),
            origin_x=int(cap_meta.get("origin_x", 0)),
            origin_y=int(cap_meta.get("origin_y", 0)),
            width=int(cap_meta.get("width", 0)),
            height=int(cap_meta.get("height", 0)),
            frame_hash=frame_hash,
            screen_changed=changed,
        )

    async def resolve_target(
        self,
        target_description: str,
        observation: Optional[DesktopObservation] = None,
        window_title: Optional[str] = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Dynamically identify a GUI element without hardcoded coordinates.

        Resolution order:
        1. Live UIA controls from active/target window
        2. BlueprintManager structural UIA selector verified dynamically
        3. Screen OCR visible text match (adjusted to absolute virtual-screen coords)
        4. NVIDIA Vision element grounding (mapped by observation origin_x/origin_y)
        """
        query = (target_description or "").strip()
        if not query:
            return None

        if observation is None:
            observation = await self.observe_desktop(window_title=window_title, capture_mode="adaptive")

        target_win_title = window_title or (
            observation.active_window.get("title") if observation.active_window else None
        )
        query_low = query.lower()
        terms = [t for t in query_low.split() if len(t) > 2]

        # 1. Live UIA control matching
        best_uia = None
        best_score = 0.0
        for ctrl in observation.uia_controls:
            title_l = (ctrl.get("title") or "").lower()
            auto_l = (ctrl.get("auto_id") or "").lower()
            type_l = (ctrl.get("type") or "").lower()
            combined = f"{title_l} {auto_l} {type_l}".strip()
            if not combined:
                continue
            score = 0.0
            if query_low == title_l or (auto_l and query_low == auto_l):
                score += 6.0
            elif query_low in title_l or (auto_l and query_low in auto_l):
                score += 4.0
            if terms:
                score += sum(1.0 for t in terms if t in combined)
            if score > best_score and ctrl.get("enabled", True):
                best_score = score
                best_uia = ctrl

        if best_uia and best_score >= 2.0:
            return {
                "method": "uia",
                "x": best_uia["center_x"],
                "y": best_uia["center_y"],
                "bbox": best_uia.get("rect"),
                "control_identifier": {
                    k: best_uia[k] for k in ("type", "title", "auto_id") if best_uia.get(k)
                },
                "window_title": target_win_title,
                "confidence": min(1.0, best_score / 6.0),
            }

        # 2. BlueprintManager structural selector lookup (verified live via UIA)
        if self._blueprint_manager and observation.active_window and target_win_title:
            try:
                proc = observation.active_window.get("process_name") or ""
                if proc:
                    bp = self._blueprint_manager.store.get_blueprint_for_app(proc)
                    if bp:
                        bp_elem = await self._blueprint_manager.find_element(bp, target_win_title, query)
                        if bp_elem:
                            ident = self._blueprint_manager.get_control_identifier(bp_elem)
                            if ident:
                                find_res = await self._execute_tool(
                                    "window_interact",
                                    {
                                        "action": "find_control",
                                        "window_title": target_win_title,
                                        "control_identifier": ident,
                                    },
                                    ["automation:window"],
                                )
                                if find_res.get("success") and (find_res.get("result") or {}).get("control"):
                                    rect = find_res["result"]["control"].get("rect") or {}
                                    if rect.get("width", 0) > 0:
                                        return {
                                            "method": "blueprint_uia",
                                            "x": rect["left"] + rect["width"] // 2,
                                            "y": rect["top"] + rect["height"] // 2,
                                            "bbox": rect,
                                            "control_identifier": ident,
                                            "window_title": target_win_title,
                                            "confidence": bp_elem.confidence,
                                        }
            except Exception as e:
                logger.debug("Blueprint resolution skipped: %s", e)

        # 3. OCR text matching (VisualFindTool returns absolute virtual-screen coordinates)
        try:
            ocr_res = await self.find_text_on_screen(query, window_title=target_win_title, threshold=0.4)
            matches = (ocr_res.details or {}).get("matches", []) if ocr_res.success else []
            if matches:
                m = matches[0]
                cx = m.get("center_x", int(m["x"]) + int(m.get("width", 0)) // 2)
                cy = m.get("center_y", int(m["y"]) + int(m.get("height", 0)) // 2)
                return {
                    "method": "ocr",
                    "x": cx,
                    "y": cy,
                    "bbox": m,
                    "window_title": target_win_title,
                    "confidence": float(m.get("confidence", 0.6)),
                }
        except Exception as e:
            logger.debug("OCR target lookup failed: %s", e)

        # 4. NVIDIA Vision visual grounding (convert window/image pixels -> screen coordinates)
        if self._vision_factory and observation.screenshot_base64:
            try:
                elems = await self._vision_factory.find_elements(
                    observation.screenshot_base64,
                    query,
                    provider_type=provider_type,
                )
                for elem in elems:
                    bbox = elem.get("bbox")
                    if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                        sx = observation.origin_x + int(bbox["left"]) + int(bbox["width"]) // 2
                        sy = observation.origin_y + int(bbox["top"]) + int(bbox["height"]) // 2
                        return {
                            "method": "vision",
                            "x": sx,
                            "y": sy,
                            "bbox": {
                                "left": observation.origin_x + int(bbox["left"]),
                                "top": observation.origin_y + int(bbox["top"]),
                                "width": int(bbox["width"]),
                                "height": int(bbox["height"]),
                            },
                            "element": elem,
                            "window_title": target_win_title,
                            "confidence": float(elem.get("confidence", 0.6)),
                        }
            except Exception as e:
                logger.debug("Vision element grounding failed: %s", e)

        return None
    
    async def analyze_screen(
        self,
        task_description: str = "",
        window_title: str = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> ScreenAnalysis:
        """
        Analyze current screen using vision model.
        
        This is the primary method for understanding desktop state.
        Captures screenshot, sends to vision model, returns structured analysis.
        
        Args:
            task_description: Context about what we're looking for
            window_title: Optional specific window to analyze
            provider_type: Force specific vision provider
            
        Returns:
            ScreenAnalysis with elements, text, suggested actions, app type
        """
        if not self._vision_factory:
            return ScreenAnalysis(
                description="Visual intelligence not initialized",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="none",
                screenshot_dimensions={},
            )
        
        # Capture screenshot
        screenshot = await self.capture_screenshot_base64(mode="window" if window_title else "full", window_title=window_title)
        if not screenshot:
            return ScreenAnalysis(
                description="Failed to capture screenshot",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="none",
                screenshot_dimensions={},
            )
        
        # Analyze with vision model
        return await self._vision_factory.analyze_screen(
            screenshot_base64=screenshot,
            task_description=task_description,
            provider_type=provider_type,
        )
    
    async def get_next_action(
        self,
        current_task: str,
        previous_actions: List[str] = None,
        window_title: str = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> VisionAction:
        """
        Get the next recommended action from vision model.
        
        Implements the core vision-guided automation loop:
        1. Capture screenshot
        2. Send to vision model with task context
        3. Model returns specific action to take
        
        Args:
            current_task: High-level task description
            previous_actions: History of actions taken
            window_title: Optional specific window
            provider_type: Force specific vision provider
            
        Returns:
            VisionAction with action type, target, params, reasoning, confidence
        """
        if not self._vision_factory:
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning="Visual intelligence not initialized",
                confidence=0.0,
            )
        
        # Capture screenshot
        screenshot = await self.capture_screenshot_base64(mode="window" if window_title else "full", window_title=window_title)
        if not screenshot:
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning="Failed to capture screenshot",
                confidence=0.0,
            )
        
        # Get action from vision model
        return await self._vision_factory.get_next_action(
            screenshot_base64=screenshot,
            current_task=current_task,
            previous_actions=previous_actions,
            provider_type=provider_type,
        )
    
    async def execute_vision_action(
        self,
        action: VisionAction,
        observation: Optional[DesktopObservation] = None,
        coord_offset: Tuple[int, int] = (0, 0),
        window_title: Optional[str] = None,
    ) -> InteractionResult:
        """
        Execute a vision-recommended action through Windows automation engine.

        Supports dynamic element resolution (UIA -> Blueprint -> OCR -> Vision) when
        coordinates are omitted, and maps window-relative image coordinates into
        absolute virtual-screen pixels via coord_offset.
        """
        if action.action == "error":
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.MOUSE_KEYBOARD,
                error=action.reasoning,
            )

        offset_x, offset_y = coord_offset
        if observation is not None and coord_offset == (0, 0):
            offset_x, offset_y = observation.origin_x, observation.origin_y

        def _map_xy(raw_x: Any, raw_y: Any) -> Optional[Tuple[int, int]]:
            if raw_x is None or raw_y is None:
                return None
            try:
                sx = int(round(float(raw_x))) + int(offset_x)
                sy = int(round(float(raw_y))) + int(offset_y)
                return sx, sy
            except (TypeError, ValueError):
                return None

        mapped_xy = _map_xy(action.params.get("x"), action.params.get("y"))
        if mapped_xy is not None:
            mx, my = mapped_xy
            if mx < -7680 or my < -4320 or mx > 7680 or my > 4320:
                return InteractionResult(
                    success=False,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    error=f"Coordinates out of virtual screen bounds: ({mx}, {my})",
                )

        try:
            act = (action.action or "").lower().strip()
            win_ctx = window_title or (
                observation.active_window.get("title")
                if observation and observation.active_window
                else None
            )

            if act in ("click", "double_click", "right_click"):
                coords = mapped_xy
                resolved = None
                # Treat (0, 0) with an explicit element description as missing coords -> resolve dynamically
                if (coords is None or coords == (offset_x, offset_y)) and action.target:
                    resolved = await self.resolve_target(
                        action.target,
                        observation=observation,
                        window_title=win_ctx,
                    )
                    if resolved:
                        coords = (int(resolved["x"]), int(resolved["y"]))

                if resolved and resolved.get("method") in ("uia", "blueprint_uia") and act == "click":
                    ci = resolved.get("control_identifier") or {}
                    wt = resolved.get("window_title") or win_ctx
                    if ci and wt:
                        ui_res = await self._execute_tool(
                            "window_interact",
                            {"action": "click", "window_title": wt, "control_identifier": ci},
                            ["automation:window"],
                        )
                        if ui_res.get("success"):
                            return InteractionResult(
                                success=True,
                                method_used=InteractionMethod.UI_AUTOMATION,
                                details={"resolved": resolved},
                            )

                if coords is None:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error=f"Could not resolve target coordinates for '{action.target}'",
                    )

                button = "right" if act == "right_click" else action.params.get("button", "left")
                result = await self._execute_tool(
                    "mouse_control",
                    {"action": act, "x": coords[0], "y": coords[1], "button": button},
                    ["automation:input"],
                )

            elif act == "drag":
                start_xy = mapped_xy
                end_xy = _map_xy(action.params.get("drag_to_x"), action.params.get("drag_to_y"))
                if start_xy is None or end_xy is None:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Drag requires x, y, drag_to_x, and drag_to_y coordinates",
                    )
                result = await self._execute_tool(
                    "mouse_control",
                    {
                        "action": "drag",
                        "x": start_xy[0],
                        "y": start_xy[1],
                        "drag_to_x": end_xy[0],
                        "drag_to_y": end_xy[1],
                        "duration": float(action.params.get("duration", 0.5)),
                    },
                    ["automation:input"],
                )

            elif act == "type":
                text = action.params.get("text", "")
                if not text:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Type action requires text parameter",
                    )
                # Focus target control/point first if specified
                if mapped_xy and mapped_xy != (offset_x, offset_y):
                    await self._execute_tool(
                        "mouse_control",
                        {"action": "click", "x": mapped_xy[0], "y": mapped_xy[1], "button": "left"},
                        ["automation:input"],
                    )
                    await asyncio.sleep(0.1)
                elif action.target and action.target.lower() not in ("", "active", "focused", "screen"):
                    resolved = await self.resolve_target(
                        action.target, observation=observation, window_title=win_ctx
                    )
                    if resolved:
                        await self._execute_tool(
                            "mouse_control",
                            {"action": "click", "x": resolved["x"], "y": resolved["y"], "button": "left"},
                            ["automation:input"],
                        )
                        await asyncio.sleep(0.1)

                result = await self._execute_tool(
                    "keyboard_input",
                    {"action": "type", "text": text, "interval": float(action.params.get("interval", 0.02))},
                    ["automation:input"],
                )

            elif act in ("press", "press_key"):
                key = action.params.get("key") or action.target
                if not key:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Press action requires key parameter",
                    )
                result = await self._execute_tool(
                    "keyboard_input",
                    {"action": "press", "key": str(key)},
                    ["automation:input"],
                )

            elif act == "hotkey":
                keys = action.params.get("keys", [])
                if not keys:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Hotkey action requires keys parameter",
                    )
                result = await self._execute_tool(
                    "keyboard_input",
                    {"action": "hotkey", "keys": [str(k) for k in keys]},
                    ["automation:input"],
                )

            elif act == "wait":
                duration = float(action.params.get("duration", 1.0))
                await asyncio.sleep(duration)
                return InteractionResult(
                    success=True,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    details={"waited": duration},
                )

            elif act == "scroll":
                direction = str(action.params.get("direction", "down")).lower()
                raw_amt = action.params.get("scroll_amount", action.params.get("amount", 3))
                amt = abs(int(raw_amt)) if raw_amt is not None else 3
                signed_amount = amt if direction in ("up", "left") else -amt
                if mapped_xy and mapped_xy != (offset_x, offset_y):
                    await self._execute_tool(
                        "mouse_control",
                        {"action": "move", "x": mapped_xy[0], "y": mapped_xy[1], "duration": 0.1},
                        ["automation:input"],
                    )
                result = await self._execute_tool(
                    "mouse_control",
                    {"action": "scroll", "scroll_amount": signed_amount},
                    ["automation:input"],
                )

            elif act == "focus_window":
                target_win = action.params.get("window_title") or action.target or win_ctx
                if not target_win:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.UI_AUTOMATION,
                        error="focus_window requires window title",
                    )
                return await self.focus_window(target_win, verify=True)

            elif act == "navigate":
                url = action.params.get("url", "")
                if not url:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Navigate action requires url parameter",
                    )
                await self._execute_tool("keyboard_input", {"action": "hotkey", "keys": ["ctrl", "l"]}, ["automation:input"])
                await asyncio.sleep(0.2)
                await self._execute_tool("keyboard_input", {"action": "type", "text": url}, ["automation:input"])
                await asyncio.sleep(0.2)
                result = await self._execute_tool("keyboard_input", {"action": "press", "key": "enter"}, ["automation:input"])

            elif act == "complete":
                return InteractionResult(
                    success=True,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    details={"completed": True, "reasoning": action.reasoning},
                    verification_passed=True,
                )

            else:
                return InteractionResult(
                    success=False,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    error=f"Unknown action: {action.action}",
                )

            return InteractionResult(
                success=result.get("success", False),
                method_used=InteractionMethod.MOUSE_KEYBOARD,
                details=result.get("result", {}),
                error=result.get("error"),
            )

        except Exception as e:
            logger.error("Vision action execution failed: %s", e)
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.MOUSE_KEYBOARD,
                error=str(e),
            )
    
    async def verify_action_result_visual(
        self,
        before_screenshot: str,
        expected_change: str,
        window_title: str = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> Dict[str, Any]:
        """
        Verify action result using vision model comparison.
        
        Captures new screenshot, compares with before screenshot using vision model.
        
        Args:
            before_screenshot: Base64 screenshot before action
            expected_change: Description of expected visual change
            window_title: Optional specific window
            provider_type: Force specific vision provider
            
        Returns:
            Dict with verified (bool), confidence, reasoning
        """
        if not self._vision_factory:
            return {"verified": False, "confidence": 0.0, "error": "Visual intelligence not initialized"}
        
        # Capture after screenshot
        after_screenshot = await self.capture_screenshot_base64(mode="window" if window_title else "full", window_title=window_title)
        if not after_screenshot:
            return {"verified": False, "confidence": 0.0, "error": "Failed to capture after screenshot"}
        
        # Verify with vision model
        return await self._vision_factory.verify_action(
            before_screenshot=before_screenshot,
            after_screenshot=after_screenshot,
            expected_change=expected_change,
            provider_type=provider_type,
        )
    
    async def run_vision_guided_task(
        self,
        task: str,
        max_steps: int = 20,
        window_title: str = None,
        provider_type: Optional[VisionProviderType] = None,
        verification_threshold: float = 0.7,
        cancellation_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Run a vision-guided automation task with adaptive screenshot capture,
        dynamic coordinate translation, and recovery from failed/stuck steps.
        """
        if not self._initialized:
            await self.initialize()
        if not self._vision_factory or not await self._vision_factory.get_provider():
            return {"success": False, "error": "Visual intelligence not available"}
        
        actions_history = []
        previous_actions = []
        consecutive_failures = 0
        obs: Optional[DesktopObservation] = None
        
        for step in range(max_steps):
            if cancellation_check and cancellation_check():
                return {
                    "success": False,
                    "cancelled": True,
                    "error": "Task cancelled",
                    "steps": step,
                    "actions": actions_history,
                }

            logger.info("Vision-guided task step %d/%d: %s", step + 1, max_steps, task)
            
            # 1. Reuse post-action observation when valid, otherwise capture adaptively
            if obs is None or not obs.screenshot_base64:
                obs = await self.observe_desktop(
                    window_title=window_title,
                    capture_mode="adaptive",
                )
            if not obs.screenshot_base64:
                return {"success": False, "error": "Failed to capture screenshot", "steps": step}
            
            # 2. Decide next action using shared screenshot (no redundant re-capture)
            action = await self._vision_factory.get_next_action(
                screenshot_base64=obs.screenshot_base64,
                current_task=task,
                previous_actions=previous_actions,
                provider_type=provider_type,
            )
            actions_history.append(action.to_dict())
            
            if action.action == "error":
                return {"success": False, "error": action.reasoning, "steps": step, "actions": actions_history}
            
            if action.action == "complete":
                return {
                    "success": True,
                    "completed": True,
                    "steps": step + 1,
                    "actions": actions_history,
                    "final_reasoning": action.reasoning,
                }
            
            before_screenshot = obs.screenshot_base64
            before_hash = obs.frame_hash
            
            # 3. Execute action with observation & dynamic coord offset
            execute_result = await self.execute_vision_action(
                action,
                observation=obs,
                coord_offset=(obs.origin_x, obs.origin_y),
                window_title=window_title,
            )
            previous_actions.append(
                f"{action.action}: {action.target} -> {'ok' if execute_result.success else execute_result.error}"
            )
            
            if not execute_result.success:
                consecutive_failures += 1
                logger.warning("Action failed (%s); forcing full observation for recovery", execute_result.error)
                obs = await self.observe_desktop(window_title=window_title, capture_mode="full")
                if consecutive_failures >= 4:
                    return {
                        "success": False,
                        "error": f"Aborted after {consecutive_failures} consecutive action failures: {execute_result.error}",
                        "steps": step + 1,
                        "actions": actions_history,
                    }
                continue
            
            consecutive_failures = 0
            await asyncio.sleep(0.8)
            
            # 4. Observe post-action state adaptively (single capture reused in next step!)
            after_obs = await self.observe_desktop(window_title=window_title, capture_mode="adaptive")
            obs = after_obs
            
            if after_obs.screenshot_base64 and (after_obs.frame_hash != before_hash or action.is_complete):
                verification = await self._vision_factory.verify_action(
                    before_screenshot=before_screenshot,
                    after_screenshot=after_obs.screenshot_base64,
                    expected_change=action.expected_result or action.reasoning,
                    provider_type=provider_type,
                )
                if verification.get("verified") and verification.get("confidence", 0) >= verification_threshold:
                    logger.info("Step %d verified: %s", step + 1, verification.get("reasoning"))
                    if action.is_complete:
                        return {
                            "success": True,
                            "completed": True,
                            "steps": step + 1,
                            "actions": actions_history,
                        }
                else:
                    previous_actions.append(f"verify_note: {verification.get('reasoning', 'change not confirmed')}")
        
        return {
            "success": False,
            "error": f"Max steps ({max_steps}) reached",
            "steps": max_steps,
            "actions": actions_history,
        }
    
    # ============================================================
    # Helpers
    # ============================================================
    
    def _determine_window_state(self, window: Dict) -> ApplicationState:
        """Determine window state from window info."""
        if not window.get("is_visible", False):
            return ApplicationState.NOT_RUNNING
        if window.get("is_minimized", False):
            return ApplicationState.MINIMIZED
        if window.get("is_maximized", False):
            return ApplicationState.MAXIMIZED
        return ApplicationState.RUNNING
    
    def _determine_app_state(self, proc: Dict, windows: List) -> ApplicationState:
        """Determine application state from process and windows."""
        if not windows:
            return ApplicationState.NOT_RUNNING
        for w in windows:
            if w.is_visible and not w.is_minimized:
                return ApplicationState.RUNNING
        if any(w.is_minimized for w in windows):
            return ApplicationState.MINIMIZED
        return ApplicationState.RUNNING


    async def close(self) -> None:
        """Release desktop automation resources cleanly."""
        if self._blueprint_manager and getattr(self._blueprint_manager, "store", None):
            try:
                self._blueprint_manager.store.close()
            except Exception as e:
                logger.warning("Failed to close blueprint store: %s", e)
        self._initialized = False


# Global default desktop automation instance
desktop_automation = DesktopAutomation()


# Convenience function for quick automation
async def create_automation(user_id: str = "default", 
                           evidence_dir: Optional[Path] = None) -> DesktopAutomation:
    """Create and initialize a DesktopAutomation instance."""
    auto = DesktopAutomation(user_id, evidence_dir)
    await auto.initialize()
    return auto