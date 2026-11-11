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
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from enum import Enum

from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.model_providers.vision_provider import (
    get_vision_factory, VisionConfig, VisionAction, ScreenAnalysis,
    VisionProviderType
)

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


class DesktopAutomation:
    """
    Universal desktop automation engine for Windows 11.
    
    Provides application-agnostic desktop interaction capabilities.
    """
    
    def __init__(self, user_id: str = "default", evidence_dir: Optional[Path] = None):
        self.user_id = user_id
        self.evidence_dir = evidence_dir or Path.cwd() / "evidence" / f"desktop_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_count = 0
        self._initialized = False
        # Visual intelligence
        self._vision_factory = None
        self._vision_config = VisionConfig()
    
    async def initialize(self) -> None:
        """Initialize the automation engine.
        
        Does NOT grant permissions automatically - permissions must be explicitly
        granted by the user through the consent system before sensitive operations.
        """
        if self._initialized:
            return
        
        self._initialized = True
        logger.info("DesktopAutomation initialized (permissions require explicit consent)")
        
        # Initialize vision factory for screenshot-first visual intelligence
        try:
            self._vision_factory = await get_vision_factory(self._vision_config)
            if self._vision_factory._current_provider:
                logger.info(f"Visual intelligence enabled: {self._vision_factory._current_provider.provider_name}")
            else:
                logger.warning("Visual intelligence disabled: no vision providers available")
        except Exception as e:
            logger.warning(f"Failed to initialize visual intelligence: {e}")
    
    def _get_correlation_id(self, prefix: str = "auto") -> str:
        """Generate a correlation ID for tracking."""
        return f"{prefix}-{int(time.time() * 1000)}"
    
    async def _execute_tool(self, tool_name: str, arguments: Dict[str, Any], 
                           permissions: List[str], correlation_id: str = None) -> Dict[str, Any]:
        """Execute a tool with proper context."""
        if not self._initialized:
            await self.initialize()
        
        cid = correlation_id or self._get_correlation_id(tool_name)
        tool = tool_registry.get_tool(tool_name)
        
        # Check if all required permissions are granted - if so, provide auto-approval
        approval_token = None
        all_granted = True
        for perm in permissions:
            if not consent_manager.is_granted(perm):
                all_granted = False
                break
        
        # Get requires_approval from manifest (handles both dict and object)
        manifest = tool.manifest
        requires_approval = manifest.get('requires_approval', False) if isinstance(manifest, dict) else getattr(manifest, 'requires_approval', False)
        
        if all_granted and requires_approval:
            approval_token = f"auto-{self.user_id}-{cid}"
        
        # Prepare arguments with approval token if available
        tool_args = dict(arguments)
        if approval_token:
            tool_args["approval_token"] = approval_token
        
        ctx = ToolExecutionContext(
            tool_name=tool_name,
            arguments=tool_args,
            correlation_id=cid,
            user_id=self.user_id,
            permissions_granted=permissions,
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        # ToolResult has attributes: success, result, error, error_code
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
        print(f'   taskkill result: {result.returncode}, stdout: {result.stdout.strip()}, stderr: {result.stderr.strip()}')
        await asyncio.sleep(2)
        
        # Verify all closed
        from m_core.tools import tool_registry
        from m_core.common_schemas import ToolExecutionContext
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
                        print(f'   Found Save As dialog via window_list: "{dialog_title}" (attempt {attempt+1})')
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
                print(f'   Found Save As dialog via win32gui: "{dialog_title}" HWND={dialog_hwnd} (attempt {attempt+1})')
                break
        
        if not dialog_hwnd:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error="Save As dialog not found after retries"
            )
        
        # Focus Save As dialog using HWND
        focus_result = await self.focus_window(dialog_hwnd, verify=True)
        if not focus_result.success:
            print(f'   Warning: Could not verify focus on Save As dialog')
        
        await asyncio.sleep(0.5)
        
        # Type filename in Save As dialog
        # Use title + control_type to find the filename edit control
        filename_ctrl = {"type": "Edit", "title": "File name:", "index": 0}
        result = await self._execute_tool("window_interact", {
            "action": "type", "window_title": dialog_title, "control_identifier": filename_ctrl, 
            "text": filename
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
                        print(f'   Found Confirm Save As dialog: "{confirm_title}"')
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
            if result["success"]:
                print(f'   Clicked "Yes" button via UI Automation')
            else:
                # Fallback: try Tab to navigate to Yes, then Enter
                print(f'   UI Automation failed, trying Tab navigation')
                result = await self.send_hotkey(['tab'])
                if result.success:
                    await asyncio.sleep(0.2)
                    result = await self.send_hotkey(['enter'])
                    if result.success:
                        print(f'   Pressed Tab then Enter to confirm replace')
        
        await asyncio.sleep(2)  # Wait for save to complete
        
        # Verify file was saved - search comprehensively
        possible_paths = [
            os.path.expanduser(f'~/Desktop/{filename}'),
            os.path.expanduser(f'~/Documents/{filename}'),
            os.path.expanduser(f'~/OneDrive/Desktop/{filename}'),
            os.path.expanduser(f'~/OneDrive/Desktop/ALIYAJSHAIKH/{filename}'),
            os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop', filename),
            os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', filename),
            os.path.join(os.environ.get('USERPROFILE', ''), 'OneDrive', 'Desktop', filename),
        ]
        
        if target_dir:
            possible_paths.insert(0, os.path.join(target_dir, filename))
        
        # Search OneDrive Desktop subfolders
        onedrive_desktop = os.path.expanduser('~/OneDrive/Desktop')
        if os.path.exists(onedrive_desktop):
            for root, dirs, files in os.walk(onedrive_desktop):
                if filename in files:
                    possible_paths.append(os.path.join(root, filename))
        
        # Also search common user folders
        user_profile = os.environ.get('USERPROFILE', '')
        if user_profile:
            for root, dirs, files in os.walk(user_profile):
                if filename in files:
                    possible_paths.append(os.path.join(root, filename))
        
        # Deduplicate
        possible_paths = list(dict.fromkeys(possible_paths))
        
        saved_path = None
        for path in possible_paths:
            if os.path.exists(path):
                saved_path = path
                break
        
        if saved_path:
            return InteractionResult(
                success=True,
                method_used=InteractionMethod.UI_AUTOMATION,
                details={"file_path": saved_path, "dialog_title": dialog_title}
            )
        else:
            # Debug: list all .txt files in common locations to help diagnose
            debug_info = {"searched_paths": possible_paths}
            for base in [os.path.expanduser('~/Desktop'), os.path.expanduser('~/Documents'), os.path.expanduser('~/OneDrive/Desktop')]:
                if os.path.exists(base):
                    txt_files = [f for f in os.listdir(base) if f.endswith('.txt')]
                    debug_info[f"{base}_txt_files"] = txt_files[:10]
            
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.UI_AUTOMATION,
                error=f"File not found after save. Checked {len(possible_paths)} locations.",
                details={**{"dialog_title": dialog_title}, **debug_info}
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
                print(f'   Found Save As dialog: "{dialog_title}"')
                break
            result = await self._execute_tool("window_list", {"filter_title": "Save As"}, ["app:control"])
            if result["success"] and result["result"]["windows"]:
                save_dialog = result["result"]["windows"][0]
                dialog_title = save_dialog["title"]
                print(f'   Found Save As dialog: "{dialog_title}"')
                break
        
        if not dialog_title:
            return InteractionResult(
                success=False, method_used=InteractionMethod.UI_AUTOMATION,
                error="Save As dialog not found"
            )
        
        # Focus dialog
        result = await self.focus_window(dialog_title, verify=True)
        
        await asyncio.sleep(0.5)
        
        # Type filename
        filename_ctrl = {"type": "Edit", "title": "File name:", "index": 0}
        result = await self._execute_tool("window_interact", {
            "action": "type", "window_title": dialog_title, "control_identifier": filename_ctrl, 
            "text": filename
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
                print(f'   Found Confirm Save As dialog: "{confirm_title}"')
                
                # Navigate to "Yes" button using left arrow (default is "No")
                result = await self.send_hotkey(['left'])
                if result.success:
                    print(f'   Moved to "Yes" button')
                    await asyncio.sleep(0.2)
                    result = await self.send_hotkey(['enter'])
                    if result.success:
                        print(f'   Pressed Enter to confirm replace')
                break
        
        await asyncio.sleep(3)  # Wait for save to complete
        
        # Verify file was saved - search comprehensively
        possible_paths = [
            os.path.expanduser(f'~/Desktop/{filename}'),
            os.path.expanduser(f'~/Documents/{filename}'),
            os.path.expanduser(f'~/OneDrive/Desktop/{filename}'),
            os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop', filename),
            os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', filename),
            os.path.join(os.environ.get('USERPROFILE', ''), 'OneDrive', 'Desktop', filename),
        ]
        
        if target_dir:
            possible_paths.insert(0, os.path.join(target_dir, filename))
        
        # Search OneDrive Desktop subfolders
        onedrive_desktop = os.path.expanduser('~/OneDrive/Desktop')
        if os.path.exists(onedrive_desktop):
            for root, dirs, files in os.walk(onedrive_desktop):
                if filename in files:
                    possible_paths.append(os.path.join(root, filename))
        
        # Also search common user folders
        user_profile = os.environ.get('USERPROFILE', '')
        if user_profile:
            for root, dirs, files in os.walk(user_profile):
                if filename in files:
                    possible_paths.append(os.path.join(root, filename))
        
        # Deduplicate
        possible_paths = list(dict.fromkeys(possible_paths))
        
        saved_path = None
        for path in possible_paths:
            if os.path.exists(path):
                saved_path = path
                break
        
        if saved_path:
            return InteractionResult(
                success=True,
                method_used=InteractionMethod.UI_AUTOMATION,
                details={"file_path": saved_path, "dialog_title": dialog_title}
            )
        else:
            debug_info = {"searched_paths": possible_paths}
            for base in [os.path.expanduser('~/Desktop'), os.path.expanduser('~/Documents'), os.path.expanduser('~/OneDrive/Desktop')]:
                if os.path.exists(base):
                    txt_files = [f for f in os.listdir(base) if f.endswith('.txt')]
                    debug_info[f"{base}_txt_files"] = txt_files[:10]
            
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.UI_AUTOMATION,
                error=f"File not found after fallback save. Checked {len(possible_paths)} locations.",
                details={**{"dialog_title": dialog_title}, **debug_info}
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
    
    async def capture_screenshot_base64(self, mode: str = "full", window_title: str = None) -> Optional[str]:
        """Capture screenshot and return as base64 string for vision analysis."""
        self._screenshot_count += 1
        name = f"screenshot_{self._screenshot_count}_{int(time.time())}.png"
        path = self.evidence_dir / name
        
        result = await self._execute_tool("screen_capture", {
            "mode": mode, "output_format": "png", "save_to_file": True,
            "window_title": window_title
        }, ["screen:capture"])
        
        if result["success"] and "image_path" in result["result"]:
            import shutil
            shutil.copy2(result["result"]["image_path"], path)
            
            # Read and encode as base64
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        return None
    
    async def capture_window_screenshot(self, window_title: str) -> Optional[str]:
        """Capture screenshot of a specific window."""
        return await self.capture_screenshot_base64(mode="window", window_title=window_title)
    
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
    
    async def execute_vision_action(self, action: VisionAction) -> InteractionResult:
        """
        Execute a vision-recommended action through Windows automation engine.
        
        Validates action, checks permissions, executes via existing automation tools.
        Never executes arbitrary code from vision model.
        
        Args:
            action: VisionAction from vision model
            
        Returns:
            InteractionResult with success status
        """
        if action.action == "error":
            return InteractionResult(
                success=False,
                method_used=InteractionMethod.MOUSE_KEYBOARD,
                error=action.reasoning,
            )
        
        # Validate coordinates if present
        if action.params.get("x") is not None or action.params.get("y") is not None:
            x = action.params.get("x", 0)
            y = action.params.get("y", 0)
            # Basic bounds check (will be refined per screen resolution)
            if x < 0 or y < 0 or x > 7680 or y > 4320:  # 8K max
                return InteractionResult(
                    success=False,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    error=f"Coordinates out of bounds: ({x}, {y})",
                )
        
        # Execute action based on type
        try:
            if action.action == "click":
                x = action.params.get("x")
                y = action.params.get("y")
                if x is not None and y is not None:
                    result = await self._execute_tool("mouse_control", {
                        "action": "click", "x": x, "y": y, "button": "left"
                    }, ["automation:input"])
                else:
                    # Click on element by description (would need element finding)
                    result = await self._execute_tool("mouse_control", {
                        "action": "click", "x": 0, "y": 0, "button": "left"
                    }, ["automation:input"])
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Click requires x,y coordinates from vision model",
                    )
            
            elif action.action == "type":
                text = action.params.get("text", "")
                if text:
                    result = await self._execute_tool("keyboard_input", {
                        "action": "type", "text": text
                    }, ["automation:input"])
                else:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Type action requires text parameter",
                    )
            
            elif action.action == "hotkey":
                keys = action.params.get("keys", [])
                if keys:
                    result = await self._execute_tool("keyboard_input", {
                        "action": "press", "keys": keys
                    }, ["automation:input"])
                else:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Hotkey action requires keys parameter",
                    )
            
            elif action.action == "wait":
                await asyncio.sleep(action.params.get("duration", 1.0))
                return InteractionResult(
                    success=True,
                    method_used=InteractionMethod.MOUSE_KEYBOARD,
                    details={"waited": action.params.get("duration", 1.0)},
                )
            
            elif action.action == "scroll":
                direction = action.params.get("direction", "down")
                amount = action.params.get("amount", 3)
                result = await self._execute_tool("mouse_control", {
                    "action": "scroll", "direction": direction, "amount": amount
                }, ["automation:input"])
            
            elif action.action == "navigate":
                # Navigate to URL or path
                url = action.params.get("url", "")
                if url:
                    result = await self._execute_tool("keyboard_input", {
                        "action": "hotkey", "keys": ["ctrl", "l"]
                    }, ["automation:input"])
                    await asyncio.sleep(0.3)
                    result = await self._execute_tool("keyboard_input", {
                        "action": "type", "text": url
                    }, ["automation:input"])
                    await asyncio.sleep(0.3)
                    result = await self._execute_tool("keyboard_input", {
                        "action": "press", "keys": ["enter"]
                    }, ["automation:input"])
                else:
                    return InteractionResult(
                        success=False,
                        method_used=InteractionMethod.MOUSE_KEYBOARD,
                        error="Navigate action requires url parameter",
                    )
            
            elif action.action == "complete":
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
            logger.error(f"Vision action execution failed: {e}")
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
    ) -> Dict[str, Any]:
        """
        Run a complete vision-guided automation task.
        
        Implements the continuous visual feedback loop:
        SCREENSHOT → VISION ANALYSIS → ACTION PLAN → VALIDATION → ACTION → NEW SCREENSHOT → VERIFICATION
        
        Args:
            task: High-level task description
            max_steps: Maximum number of action steps
            window_title: Optional specific window
            provider_type: Force specific vision provider
            verification_threshold: Minimum confidence to accept verification
            
        Returns:
            Dict with success, steps_taken, final_state, actions_history
        """
        if not self._vision_factory or not self._vision_factory._current_provider:
            return {"success": False, "error": "Visual intelligence not available"}
        
        actions_history = []
        previous_actions = []
        
        for step in range(max_steps):
            logger.info(f"Vision-guided task step {step + 1}/{max_steps}: {task}")
            
            # 1. Capture screenshot and analyze
            analysis = await self.analyze_screen(task, window_title, provider_type)
            if "error" in analysis.to_dict():
                return {"success": False, "error": analysis.to_dict().get("error"), "steps": step}
            
            # 2. Get next action from vision model
            action = await self.get_next_action(task, previous_actions, window_title, provider_type)
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
            
            # 3. Capture before screenshot for verification
            before_screenshot = await self.capture_screenshot_base64(mode="window" if window_title else "full", window_title=window_title)
            
            # 4. Execute action through Windows automation
            execute_result = await self.execute_vision_action(action)
            previous_actions.append(f"{action.action}: {action.target} ({action.reasoning})")
            
            if not execute_result.success:
                # Action failed, try to recover
                logger.warning(f"Action failed: {execute_result.error}, attempting recovery")
                # Could implement recovery logic here
                continue
            
            # 5. Wait for UI to update
            await asyncio.sleep(1.0)
            
            # 6. Verify result
            verification = await self.verify_action_result_visual(
                before_screenshot=before_screenshot,
                expected_change=action.expected_result or action.reasoning,
                window_title=window_title,
                provider_type=provider_type,
            )
            
            if verification.get("verified") and verification.get("confidence", 0) >= verification_threshold:
                logger.info(f"Step {step + 1} verified: {verification.get('reasoning')}")
                if action.is_complete:
                    return {
                        "success": True,
                        "completed": True,
                        "steps": step + 1,
                        "actions": actions_history,
                    }
            else:
                logger.warning(f"Step {step + 1} verification failed or low confidence: {verification}")
                # Could implement retry/recovery here
        
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


# Convenience function for quick automation
async def create_automation(user_id: str = "default", 
                           evidence_dir: Optional[Path] = None) -> DesktopAutomation:
    """Create and initialize a DesktopAutomation instance."""
    auto = DesktopAutomation(user_id, evidence_dir)
    await auto.initialize()
    return auto