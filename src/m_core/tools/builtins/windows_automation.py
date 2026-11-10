"""Windows automation tools for M AI Assistant."""
import asyncio
import os
import platform
import shlex
import subprocess
import structlog
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from m_core.common_schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


# Check for optional Windows automation dependencies
try:
    import win32gui
    import win32con
    import win32process
    import win32api
    import pywinauto
    from pywinauto.application import Application
    from pywinauto.findwindows import find_windows
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


def _ensure_default_desktop() -> None:
    """Ensure the calling thread is attached to the interactive Windows default desktop."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass


@dataclass
class WindowInfo:
    """Information about a window."""
    hwnd: int
    title: str
    class_name: str
    process_id: int
    process_name: str
    rect: tuple
    is_visible: bool
    is_minimized: bool
    is_maximized: bool


class AppLaunchTool(BaseTool):
    """Launch an application on Windows."""
    
    manifest = {
        "name": "app_launch",
        "display_name": "Launch Application",
        "description": "Launch an application on Windows by name or path.",
        "category": "windows",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Application name (e.g., 'notepad', 'calc', 'explorer') or full executable path"},
                "arguments": {"type": "string", "default": "", "description": "Command line arguments"},
                "working_directory": {"type": "string", "description": "Working directory for the process"},
                "run_as_admin": {"type": "boolean", "default": False, "description": "Run as administrator"},
                "wait": {"type": "boolean", "default": False, "description": "Wait for process to complete"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
            },
            "required": ["app_name"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "process_id": {"type": "integer"},
                "process_name": {"type": "string"},
                "started": {"type": "boolean"},
            },
        },
        "required_permissions": ["app:launch"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Launches applications on the system. Requires approval.",
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "launch", "application"],
    }
    
    def _get_app_path(self, app_name: str) -> str:
        """Resolve app name to executable path."""
        # Common Windows apps
        common_apps = {
            "notepad": "notepad.exe",
            "calc": "calc.exe",
            "calculator": "calc.exe",
            "explorer": "explorer.exe",
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
            "pwsh": "pwsh.exe",
            "notepad++": "notepad++.exe",
            "vscode": "code.exe",
            "code": "code.exe",
            "chrome": "chrome.exe",
            "firefox": "firefox.exe",
            "edge": "msedge.exe",
            "outlook": "outlook.exe",
            "word": r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
            "excel": "excel.exe",
            "powerpoint": "powerpnt.exe",
            "teams": "teams.exe",
            "zoom": "zoom.exe",
            "slack": "slack.exe",
            "discord": "discord.exe",
            "spotify": "spotify.exe",
            "vlc": "vlc.exe",
            "paint": "mspaint.exe",
            "snippingtool": "snippingtool.exe",
            "snip": "snippingtool.exe",
            "terminal": "wt.exe",
            "wt": "wt.exe",
        }
        
        app_lower = app_name.lower()
        if app_lower in common_apps:
            return common_apps[app_lower]
        
        # If it looks like a path, return as-is
        if "\\" in app_name or "/" in app_name or app_name.endswith(".exe"):
            return app_name
        
        # Try to find in PATH
        return app_name + ".exe"
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        app_name = args.get("app_name")
        arguments = args.get("arguments", "")
        working_directory = args.get("working_directory")
        run_as_admin = args.get("run_as_admin", False)
        wait = args.get("wait", False)
        
        if not app_name:
            return {"success": False, "error": "app_name is required", "error_code": "INVALID_INPUT"}
        
        # Validate approval
        if not args.get("approved", False):
            return {"success": False, "error": "Explicit approval required to launch applications", "error_code": "APPROVAL_REQUIRED"}
        
        app_path = self._get_app_path(app_name)
        
        try:
            # Prepare command
            cmd = [app_path]
            if arguments:
                if isinstance(arguments, str):
                    cmd.extend(shlex.split(arguments))
                elif isinstance(arguments, list):
                    cmd.extend(arguments)
            
            # Prepare startup info
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
            
            # Prepare environment
            env = os.environ.copy()
            
            # Set working directory
            cwd = working_directory if working_directory else None
            
            # Run as admin on Windows
            if run_as_admin and platform.system() == "Windows":
                import ctypes
                if not ctypes.windll.shell32.IsUserAnAdmin():
                    # Use runas verb
                    shell32 = __import__('ctypes.windll.shell32', fromlist=['ShellExecuteW'])
                    result = shell32.ShellExecuteW(None, "runas", cmd[0], " ".join(cmd[1:]), cwd or "", 1)
                    if result <= 32:
                        return {"success": False, "error": "Failed to run as administrator", "error_code": "ADMIN_REQUIRED"}
                    
                    return {
                        "success": True,
                        "result": {
                            "process_id": 0,
                            "process_name": cmd[0],
                            "started": True,
                        },
                        "error": None,
                    }
            
            # Launch process
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                startupinfo=startupinfo,
                env=env,
                stdout=subprocess.PIPE if wait else None,
                stderr=subprocess.PIPE if wait else None,
            )
            
            if wait:
                stdout, stderr = proc.communicate(timeout=60)
                return {
                    "success": True,
                    "result": {
                        "process_id": proc.pid,
                        "process_name": cmd[0],
                        "started": True,
                        "exit_code": proc.returncode,
                        "stdout": stdout.decode("utf-8", errors="ignore") if stdout else "",
                        "stderr": stderr.decode("utf-8", errors="ignore") if stderr else "",
                    },
                    "error": None,
                }
            
            return {
                "success": True,
                "result": {
                    "process_id": proc.pid,
                    "process_name": cmd[0] if cmd else app_path,
                    "started": True,
                },
                "error": None,
            }
            
        except FileNotFoundError:
            return {"success": False, "error": f"Application not found: {app_path}", "error_code": "NOT_FOUND"}
        except PermissionError:
            return {"success": False, "error": "Permission denied. Try run_as_admin.", "error_code": "PERMISSION_DENIED"}
        except Exception as e:
            return {"success": False, "error": f"Launch failed: {str(e)}", "error_code": "LAUNCH_FAILED"}


class AppControlTool(BaseTool):
    """Control running applications (focus, minimize, maximize, close)."""
    
    manifest = {
        "name": "app_control",
        "display_name": "Control Application",
        "description": "Control running application windows (focus, minimize, maximize, close, list).",
        "category": "windows",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["focus", "minimize", "maximize", "restore", "close", "list", "get_info"],
                    "description": "Action to perform on the window"
                },
                "window_title": {"type": "string", "description": "Window title to match (partial match supported)"},
                "process_name": {"type": "string", "description": "Process name to match (e.g., 'notepad.exe')"},
                "process_id": {"type": "integer", "description": "Process ID to target"},
                "hwnd": {"type": "integer", "description": "Window handle (HWND)"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "windows": {"type": "array", "items": {"type": "object"}},
                "action": {"type": "string"},
                "success": {"type": "boolean"},
            },
        },
        "required_permissions": ["app:control"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Controls application windows. Requires approval for destructive actions (close, minimize, maximize, focus, restore). List and get_info are read-only.",
        "requires_approval": False,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "control", "window", "application"],
    }
    
    def _get_windows(self) -> List[Dict[str, Any]]:
        """Get list of all visible windows."""
        _ensure_default_desktop()
        windows = []
        if not WIN32_AVAILABLE:
            return windows
        
        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    title = win32gui.GetWindowText(hwnd)
                    class_name = win32gui.GetClassName(hwnd)
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    process_name = ""
                    try:
                        process_name = psutil.Process(pid).name()
                    except:
                        pass
                    rect = win32gui.GetWindowRect(hwnd)
                    is_minimized = win32gui.IsIconic(hwnd)
                    is_maximized = win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_MAXIMIZE
                    is_visible = win32gui.IsWindowVisible(hwnd)
                    
                    windows.append({
                        "hwnd": hwnd,
                        "title": title,
                        "class_name": class_name,
                        "process_id": pid,
                        "process_name": process_name,
                        "rect": rect,
                        "is_visible": is_visible,
                        "is_minimized": is_minimized,
                        "is_maximized": is_maximized,
                    })
                except Exception:
                    pass
            return True
        
        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            logger.warning(f"Failed to enumerate windows: {e}")
        
        return windows
    
    def _find_window(self, title: str = None, process_name: str = None, pid: int = None, hwnd: int = None) -> Optional[int]:
        """Find window handle matching criteria."""
        if hwnd:
            return hwnd
        
        windows = self._get_windows()
        
        for w in windows:
            if hwnd and w["hwnd"] == hwnd:
                return w["hwnd"]
            if pid and w["process_id"] == pid:
                return w["hwnd"]
            if process_name and process_name.lower() in w["process_name"].lower():
                return w["hwnd"]
            if title and title.lower() in w["title"].lower():
                return w["hwnd"]
        return None
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        action = args.get("action")
        window_title = args.get("window_title")
        process_name = args.get("process_name")
        pid = args.get("process_id")
        hwnd = args.get("hwnd")
        
        if not action:
            return {"success": False, "error": "action is required", "error_code": "INVALID_INPUT"}
        
        if not WIN32_AVAILABLE:
            return {"success": False, "error": "Windows automation dependencies not available", "error_code": "DEPENDENCY_MISSING"}
        
        # Validate approval for destructive actions
        if action in ["close", "minimize", "maximize", "focus", "restore"]:
            if not context.arguments.get("approved", False):
                return {"success": False, "error": "Explicit approval required for window control", "error_code": "APPROVAL_REQUIRED"}
        
        hwnd_target = self._find_window(
            title=window_title,
            process_name=process_name,
            pid=pid,
            hwnd=hwnd
        )
        
        if not hwnd_target and action != "list":
            return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
        
        try:
            if action == "list":
                windows = self._get_windows()
                return {
                    "success": True,
                    "result": {
                        "windows": windows,
                        "count": len(windows),
                    },
                    "error": None,
                }
            
            elif action == "get_info":
                if hwnd_target:
                    try:
                        title = win32gui.GetWindowText(hwnd_target)
                        class_name = win32gui.GetClassName(hwnd_target)
                        _, pid = win32process.GetWindowThreadProcessId(hwnd_target)
                        rect = win32gui.GetWindowRect(hwnd_target)
                        is_minimized = win32gui.IsIconic(hwnd_target)
                        is_maximized = win32gui.GetWindowPlacement(hwnd_target)[1] == win32con.SW_MAXIMIZE
                        is_visible = win32gui.IsWindowVisible(hwnd_target)
                        
                        return {
                            "success": True,
                            "result": {
                                "hwnd": hwnd_target,
                                "title": title,
                                "class_name": class_name,
                                "process_id": pid,
                                "rect": rect,
                                "is_minimized": is_minimized,
                                "is_maximized": is_maximized,
                                "is_visible": is_visible,
                            },
                            "error": None,
                        }
                    except Exception as e:
                        return {"success": False, "error": f"Failed to get window info: {e}", "error_code": "INFO_FAILED"}
            
            elif action == "focus":
                # Use robust window activation with AttachThreadInput to bypass ERROR_BUSY
                max_retries = 3
                retry_delay = 1.0
                
                for attempt in range(max_retries):
                    hwnd_target = self._find_window(
                        title=window_title,
                        process_name=process_name,
                        pid=pid,
                        hwnd=hwnd
                    )
                    
                    if not hwnd_target:
                        await asyncio.sleep(retry_delay)
                        continue
                    
                    try:
                        # Check if window is minimized and restore first
                        if win32gui.IsIconic(hwnd_target):
                            win32gui.ShowWindow(hwnd_target, win32con.SW_RESTORE)
                            await asyncio.sleep(0.3)
                        
                        # Get current foreground window and thread info
                        current_fg_hwnd = win32gui.GetForegroundWindow()
                        current_thread_id = win32api.GetCurrentThreadId()
                        target_thread_id, _ = win32process.GetWindowThreadProcessId(hwnd_target)
                        fg_thread_id, _ = win32process.GetWindowThreadProcessId(current_fg_hwnd) if current_fg_hwnd else (0, 0)
                        
                        # Attach to target thread input queue
                        attached = False
                        if target_thread_id != current_thread_id:
                            try:
                                attached = win32process.AttachThreadInput(current_thread_id, target_thread_id, True)
                            except Exception as e:
                                if "Access is denied" in str(e) or "denied" in str(e).lower():
                                    logger.warning(f"AttachThreadInput access denied, skipping: {e}")
                                    attached = False
                                else:
                                    raise
                        
                        try:
                            # Also attach to current foreground thread if different
                            fg_attached = False
                            if fg_thread_id and fg_thread_id != current_thread_id and fg_thread_id != target_thread_id:
                                try:
                                    fg_attached = win32process.AttachThreadInput(current_thread_id, fg_thread_id, True)
                                except Exception as e:
                                    if "Access is denied" in str(e) or "denied" in str(e).lower():
                                        logger.warning(f"AttachThreadInput (fg) access denied, skipping: {e}")
                                        fg_attached = False
                                    else:
                                        raise
                            
                            # Bring window to front
                            win32gui.ShowWindow(hwnd_target, win32con.SW_SHOW)
                            win32gui.SetWindowPos(
                                hwnd_target, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
                            )
                            win32gui.SetWindowPos(
                                hwnd_target, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
                            )
                            
                            # Force foreground
                            win32gui.SetForegroundWindow(hwnd_target)
                            
                            # Verify activation
                            await asyncio.sleep(0.2)
                            if win32gui.GetForegroundWindow() == hwnd_target:
                                return {
                                    "success": True, 
                                    "result": {"focused": True, "hwnd": hwnd_target}, 
                                    "error": None
                                }
                            else:
                                # Try alternative: SetActiveWindow
                                win32gui.SetActiveWindow(hwnd_target)
                                await asyncio.sleep(0.2)
                                if win32gui.GetForegroundWindow() == hwnd_target:
                                    return {
                                        "success": True, 
                                        "result": {"focused": True, "hwnd": hwnd_target}, 
                                        "error": None
                                    }
                                # Try one more time with AllowSetForegroundWindow
                                import ctypes
                                ctypes.windll.user32.AllowSetForegroundWindow(ctypes.windll.kernel32.GetCurrentProcessId())
                                win32gui.SetForegroundWindow(hwnd_target)
                                await asyncio.sleep(0.2)
                                if win32gui.GetForegroundWindow() == hwnd_target:
                                    return {
                                        "success": True, 
                                        "result": {"focused": True, "hwnd": hwnd_target}, 
                                        "error": None
                                    }
                                
                                raise Exception("Failed to activate window - not in foreground after all attempts")
                        
                        finally:
                            # Detach thread input
                            if attached:
                                win32process.AttachThreadInput(current_thread_id, target_thread_id, False)
                            if fg_attached:
                                win32process.AttachThreadInput(current_thread_id, fg_thread_id, False)
                        
                    except Exception as e:
                        logger.warning(f"Focus attempt {attempt + 1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        return {"success": False, "error": f"Failed to focus window after {max_retries} attempts: {e}", "error_code": "FOCUS_FAILED"}
                
                return {"success": False, "error": f"Window not found after {max_retries} retries", "error_code": "WINDOW_NOT_FOUND"}
            
            elif action == "minimize":
                try:
                    win32gui.ShowWindow(hwnd_target, win32con.SW_MINIMIZE)
                    return {"success": True, "result": {"minimized": True}, "error": None}
                except Exception as e:
                    return {"success": False, "error": f"Failed to minimize: {e}", "error_code": "MINIMIZE_FAILED"}
            
            elif action == "maximize":
                try:
                    win32gui.ShowWindow(hwnd_target, win32con.SW_MAXIMIZE)
                    return {"success": True, "result": {"maximized": True}, "error": None}
                except Exception as e:
                    return {"success": False, "error": f"Failed to maximize: {e}", "error_code": "MAXIMIZE_FAILED"}
            
            elif action == "restore":
                try:
                    win32gui.ShowWindow(hwnd_target, win32con.SW_RESTORE)
                    return {"success": True, "result": {"restored": True}, "error": None}
                except Exception as e:
                    return {"success": False, "error": f"Failed to restore: {e}", "error_code": "RESTORE_FAILED"}
            
            elif action == "close":
                try:
                    win32gui.PostMessage(hwnd_target, win32con.WM_CLOSE, 0, 0)
                    return {"success": True, "result": {"closed": True}, "error": None}
                except Exception as e:
                    return {"success": False, "error": f"Failed to close: {e}", "error_code": "CLOSE_FAILED"}
            
            return {"success": False, "error": f"Unknown action: {action}", "error_code": "UNKNOWN_ACTION"}
        
        except Exception as e:
            return {"success": False, "error": f"Window control failed: {str(e)}", "error_code": "CONTROL_FAILED"}


class PowerShellTool(BaseTool):
    """Execute PowerShell commands with approval and safety checks."""
    
    manifest = {
        "name": "powershell",
        "display_name": "Execute PowerShell",
        "description": "Execute PowerShell commands with strict safety controls and approval requirements.",
        "category": "windows",
        "risk_level": "critical",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "PowerShell command or script to execute"},
                "arguments": {"type": "string", "default": "", "description": "Arguments to pass to the script"},
                "execution_policy": {"type": "string", "default": "RemoteSigned", "enum": ["Restricted", "AllSigned", "RemoteSigned", "Unrestricted", "Bypass"], "description": "Execution policy"},
                "timeout_seconds": {"type": "integer", "default": 60, "description": "Timeout in seconds"},
                "run_as_admin": {"type": "boolean", "default": False, "description": "Run as administrator"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
                "approval_token": {"type": "string", "description": "Approval token"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
                "execution_time_ms": {"type": "integer"},
            },
        },
        "required_permissions": ["system:admin"],
        "optional_permissions": [],
        "risk_level": "critical",
        "risk_description": "Executes PowerShell commands with system-level access. Critical operation requiring explicit approval.",
        "requires_approval": True,
        "timeout_seconds": 120,
        "max_retries": 0,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "powershell", "admin", "system"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        command = args.get("command")
        arguments = args.get("arguments", "")
        execution_policy = args.get("execution_policy", "RemoteSigned")
        timeout_seconds = args.get("timeout_seconds", 60)
        run_as_admin = args.get("run_as_admin", False)
        
        if not command:
            return {"success": False, "error": "command is required", "error_code": "INVALID_INPUT"}
        
        # Validate approval
        if not args.get("approved", False):
            return {"success": False, "error": "Explicit approval required for PowerShell execution", "error_code": "APPROVAL_REQUIRED"}
        
        try:
            # Build PowerShell command
            if command.endswith(".ps1"):
                # Script file
                cmd = f"& '{command}'"
                if arguments:
                    cmd += f" {arguments}"
            else:
                # Inline command
                cmd = command
                if arguments:
                    cmd += f" {arguments}"
            
            # Build PowerShell command line
            ps_cmd = [
                "powershell.exe",
                "-ExecutionPolicy", execution_policy,
                "-NoProfile",
                "-NonInteractive",
                "-Command", cmd
            ]
            
            if run_as_admin:
                # Check if already admin
                import ctypes
                if not ctypes.windll.shell32.IsUserAnAdmin():
                    # Run with elevation
                    import ctypes
                    ctypes.windll.shell32.ShellExecuteW(
                        None, "runas", "powershell.exe",
                        " ".join(f'"{arg}"' if " " in arg else arg for arg in ps_cmd[1:]),
                        None, 1
                    )
                    return {
                        "success": True,
                        "result": {
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "execution_time_ms": 0,
                        },
                        "error": "Launched as administrator. Result will be in new window.",
                    }
            
            start_time = asyncio.get_event_loop().time()
            
            proc = await asyncio.create_subprocess_exec(
                *ps_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=os.getcwd(),
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "success": False,
                    "error": f"PowerShell command timed out after {timeout_seconds}s",
                    "error_code": "TIMEOUT",
                }
            
            execution_time_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            
            stdout_text = stdout.decode("utf-8", errors="ignore") if stdout else ""
            stderr_text = stderr.decode("utf-8", errors="ignore") if stderr else ""
            
            return {
                "success": proc.returncode == 0,
                "result": {
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "exit_code": proc.returncode,
                    "execution_time_ms": int(execution_time_ms * 1000),
                },
                "error": stderr_text if proc.returncode != 0 else None,
            }
            
        except Exception as e:
            return {"success": False, "error": f"PowerShell execution failed: {str(e)}", "error_code": "EXECUTION_FAILED"}


class ProcessListTool(BaseTool):
    """List running processes with detailed information."""
    
    manifest = {
        "name": "process_list",
        "display_name": "List Processes",
        "description": "List running processes with CPU, memory, and detailed information.",
        "category": "windows",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_name": {"type": "string", "description": "Filter by process name (partial match)"},
                "include_details": {"type": "boolean", "default": True, "description": "Include detailed information"},
                "sort_by": {"type": "string", "enum": ["cpu", "memory", "name", "pid"], "default": "cpu", "description": "Sort by field"},
                "limit": {"type": "integer", "default": 50, "description": "Maximum number of processes to return"},
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "processes": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
        },
        "required_permissions": ["system:info"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only process listing. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "process", "system", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        if not PSUTIL_AVAILABLE:
            return {"success": False, "error": "psutil not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        filter_name = args.get("filter_name", "").lower()
        include_details = args.get("include_details", True)
        sort_by = args.get("sort_by", "cpu")
        limit = args.get("limit", 50)
        
        try:
            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'create_time', 'username', 'cmdline']):
                try:
                    info = proc.info
                    if filter_name and filter_name not in (info['name'] or '').lower():
                        continue
                    
                    proc_data = {
                        "pid": info['pid'],
                        "name": info['name'],
                        "cpu_percent": info['cpu_percent'],
                        "memory_percent": info['memory_percent'],
                        "status": info['status'],
                        "create_time": info['create_time'],
                    }
                    
                    if include_details:
                        if info.get('memory_info'):
                            mem = info['memory_info']
                            proc_data["memory_rss_mb"] = round(mem.rss / (1024 * 1024), 2)
                            proc_data["memory_vms_mb"] = round(mem.vms / (1024 * 1024), 2)
                        if info.get('cmdline'):
                            proc_data["cmdline"] = " ".join(info['cmdline'])
                        if info.get('username'):
                            proc_data["username"] = info['username']
                    
                    processes.append(proc_data)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            
            # Sort
            if sort_by == "cpu":
                processes.sort(key=lambda x: x.get("cpu_percent", 0) or 0, reverse=True)
            elif sort_by == "memory":
                processes.sort(key=lambda x: x.get("memory_percent", 0) or 0, reverse=True)
            elif sort_by == "name":
                processes.sort(key=lambda x: (x.get("name") or "").lower())
            elif sort_by == "pid":
                processes.sort(key=lambda x: x.get("pid", 0))
            
            return {
                "success": True,
                "result": {
                    "processes": processes[:limit],
                    "total": len(processes),
                },
                "error": None,
            }
        except Exception as e:
            return {"success": False, "error": f"Process listing failed: {str(e)}", "error_code": "LIST_FAILED"}


class WindowListTool(BaseTool):
    """List all open windows with details."""
    
    manifest = {
        "name": "window_list",
        "display_name": "List Windows",
        "description": "List all visible windows with their details (title, process, position, state).",
        "category": "windows",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_title": {"type": "string", "description": "Filter by window title (partial match)"},
                "filter_process": {"type": "string", "description": "Filter by process name"},
                "include_hidden": {"type": "boolean", "default": False, "description": "Include hidden/invisible windows"},
                "limit": {"type": "integer", "default": 100, "description": "Maximum windows to return"},
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "windows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
        },
        "required_permissions": ["app:control"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only window listing.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "window", "list", "read-only"],
    }
    
    def _get_all_windows(self) -> List[Dict[str, Any]]:
        """Get all windows."""
        _ensure_default_desktop()
        windows = []
        if not WIN32_AVAILABLE:
            return windows
        
        def enum_callback(hwnd, _):
            if not win32gui.IsWindow(hwnd):
                return True
            
            try:
                title = win32gui.GetWindowText(hwnd)
                if not title and not win32gui.IsWindowVisible(hwnd):
                    return True
                
                class_name = win32gui.GetClassName(hwnd)
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process_name = ""
                try:
                    process_name = psutil.Process(pid).name()
                except:
                    pass
                
                rect = win32gui.GetWindowRect(hwnd)
                placement = win32gui.GetWindowPlacement(hwnd)
                is_minimized = placement[1] == win32con.SW_SHOWMINIMIZED
                is_maximized = placement[1] == win32con.SW_SHOWMAXIMIZED
                is_visible = win32gui.IsWindowVisible(hwnd)
                
                windows.append({
                    "hwnd": hwnd,
                    "title": title,
                    "class_name": class_name,
                    "process_id": pid,
                    "process_name": process_name,
                    "rect": {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]},
                    "width": rect[2] - rect[0],
                    "height": rect[3] - rect[1],
                    "is_visible": is_visible,
                    "is_minimized": is_minimized,
                    "is_maximized": is_maximized,
                })
            except Exception:
                pass
            return True
        
        win32gui.EnumWindows(enum_callback, None)
        return windows
    
    async def _execute_impl(self, context) -> dict:
        if not WIN32_AVAILABLE:
            return {"success": False, "error": "Windows automation dependencies not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        filter_title = args.get("filter_title", "").lower()
        filter_process = args.get("filter_process", "").lower()
        include_hidden = args.get("include_hidden", False)
        limit = args.get("limit", 100)
        
        try:
            windows = self._get_all_windows()
            
            # Filter
            if filter_title:
                windows = [w for w in windows if filter_title in w["title"].lower()]
            if filter_process:
                windows = [w for w in windows if filter_process in w["process_name"].lower()]
            if not include_hidden:
                windows = [w for w in windows if w["is_visible"]]
            
            # Limit
            windows = windows[:limit]
            
            return {
                "success": True,
                "result": {
                    "windows": windows,
                    "total": len(windows),
                },
                "error": None,
            }
        except Exception as e:
            return {"success": False, "error": f"Window listing failed: {str(e)}", "error_code": "LIST_FAILED"}


class WindowsShortcutTool(BaseTool):
    """Create Windows shortcuts (.lnk files)."""
    
    manifest = {
        "name": "shortcut_create",
        "display_name": "Create Shortcut",
        "description": "Create a Windows shortcut (.lnk file) for a file, folder, or application.",
        "category": "windows",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "target_path": {"type": "string", "description": "Target file, folder, or application path"},
                "shortcut_path": {"type": "string", "description": "Path where to create the shortcut (.lnk)"},
                "arguments": {"type": "string", "default": "", "description": "Command line arguments for the target"},
                "working_directory": {"type": "string", "description": "Working directory"},
                "description": {"type": "string", "description": "Shortcut description/tooltip"},
                "icon_path": {"type": "string", "description": "Path to icon file (.ico, .exe, .dll)"},
                "icon_index": {"type": "integer", "default": 0, "description": "Icon index if multiple icons in file"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
            },
            "required": ["target_path", "shortcut_path"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "shortcut_path": {"type": "string"},
                "created": {"type": "boolean"},
            },
        },
        "required_permissions": ["file:write"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Creates shortcut files on the filesystem.",
        "requires_approval": True,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "shortcut", "create", "lnk"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        target_path = args.get("target_path")
        shortcut_path = args.get("shortcut_path")
        arguments = args.get("arguments", "")
        working_directory = args.get("working_directory", "")
        description = args.get("description", "")
        icon_path = args.get("icon_path", "")
        icon_index = args.get("icon_index", 0)
        
        if not target_path or not shortcut_path:
            return {"success": False, "error": "target_path and shortcut_path are required", "error_code": "INVALID_INPUT"}
        
        if not args.get("approved", False):
            return {"success": False, "error": "Explicit approval required to create shortcuts", "error_code": "APPROVAL_REQUIRED"}
        
        if not WIN32_AVAILABLE:
            return {"success": False, "error": "Windows automation dependencies not available", "error_code": "DEPENDENCY_MISSING"}
        
        try:
            import pythoncom
            from win32com.client import Dispatch
            
            shell = Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(shortcut_path)
            shortcut.TargetPath = target_path
            shortcut.Arguments = arguments
            if working_directory:
                shortcut.WorkingDirectory = working_directory
            if description:
                shortcut.Description = description
            if icon_path:
                shortcut.IconLocation = f"{icon_path}, {icon_index}"
            
            shortcut.Save()
            
            return {
                "success": True,
                "result": {
                    "shortcut_path": shortcut_path,
                    "created": True,
                },
                "error": None,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create shortcut: {str(e)}", "error_code": "SHORTCUT_FAILED"}


class SystemCommandTool(BaseTool):
    """Execute system commands (cmd.exe) with safety controls."""
    
    manifest = {
        "name": "system_command",
        "display_name": "System Command",
        "description": "Execute Windows command prompt (cmd.exe) commands with safety controls.",
        "category": "windows",
        "risk_level": "high",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "arguments": {"type": "string", "default": "", "description": "Arguments to pass"},
                "timeout_seconds": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
                "run_as_admin": {"type": "boolean", "default": False, "description": "Run as administrator"},
                "approved": {"type": "boolean", "default": False, "description": "Explicit approval flag"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
            },
        },
        "required_permissions": ["system:admin"],
        "optional_permissions": [],
        "risk_level": "high",
        "risk_description": "Executes Windows commands with system-level access. High-risk operation requiring approval.",
        "requires_approval": True,
        "timeout_seconds": 60,
        "max_retries": 0,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["windows", "cmd", "command", "system"],
    }
    
    async def _execute_impl(self, context) -> dict:
        args = context.arguments
        command = args.get("command")
        arguments = args.get("arguments", "")
        timeout_seconds = args.get("timeout_seconds", 30)
        run_as_admin = args.get("run_as_admin", False)
        
        if not command:
            return {"success": False, "error": "command is required", "error_code": "INVALID_INPUT"}
        
        if not args.get("approved", False):
            return {"success": False, "error": "Explicit approval required for system commands", "error_code": "APPROVAL_REQUIRED"}
        
        try:
            cmd = f"cmd.exe /c {command}"
            if arguments:
                cmd += f" {arguments}"
            
            if run_as_admin:
                import ctypes
                if not ctypes.windll.shell32.IsUserAnAdmin():
                    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {command} {arguments}", None, 1)
                    return {
                        "success": True,
                        "result": {"stdout": "", "stderr": "", "exit_code": 0},
                        "error": "Launched as administrator in new window",
                    }
            
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"success": False, "error": f"Command timed out after {timeout_seconds}s", "error_code": "TIMEOUT"}
            
            return {
                "success": proc.returncode == 0,
                "result": {
                    "stdout": stdout.decode("utf-8", errors="ignore") if stdout else "",
                    "stderr": stderr.decode("utf-8", errors="ignore") if stderr else "",
                    "exit_code": proc.returncode,
                },
                "error": stderr.decode("utf-8", errors="ignore") if stderr and proc.returncode != 0 else None,
            }
        except Exception as e:
            return {"success": False, "error": f"Command execution failed: {str(e)}", "error_code": "EXECUTION_FAILED"}


# Tool registration
WINDOWS_TOOLS = [
    "AppLaunchTool",
    "AppControlTool",
    "PowerShellTool",
    "ProcessListTool",
    "WindowListTool",
    "WindowsShortcutTool",
    "SystemCommandTool",
]

__all__ = [
    "AppLaunchTool",
    "AppControlTool",
    "PowerShellTool",
    "ProcessListTool",
    "WindowListTool",
    "WindowsShortcutTool",
    "SystemCommandTool",
    "WINDOWS_TOOLS",
    "WIN32_AVAILABLE",
    "PSUTIL_AVAILABLE",
]