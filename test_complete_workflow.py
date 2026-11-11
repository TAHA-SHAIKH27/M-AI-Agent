import asyncio
import sys
import os
import time
sys.path.insert(0, 'src')

from m_core.desktop.automation import create_automation, DesktopAutomation
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.tools.builtins.visual_automation import WindowInteractTool

async def test_complete_workflow():
    # Grant all needed permissions
    for cap in ['app:launch', 'app:control', 'automation:window', 'automation:input', 'file:write', 'file:read', 'screen:capture', 'screen:ocr', 'screen:find', 'screen:verify']:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    print('=' * 60)
    print('COMPLETE NOTEPAD AUTOMATION WORKFLOW TEST')
    print('=' * 60)
    
    # Create automation instance
    auto = await create_automation('test-user')
    
    # Generate unique filename for this run
    timestamp = int(time.time())
    filename = f"Test_Automation_{timestamp}.txt"
    
    # ============================================================
    # Step 1: Force close ALL existing Notepad instances
    # ============================================================
    print('\n1. Force closing ALL existing Notepad instances...')
    import subprocess
    result = subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"], capture_output=True, text=True)
    print(f'   taskkill result: {result.returncode}, stdout: {result.stdout.strip()}, stderr: {result.stderr.strip()}')
    await asyncio.sleep(2)
    
    # Verify all closed - keep checking until none remain
    from m_core.tools import tool_registry
    from m_core.common_schemas import ToolExecutionContext
    max_retries = 10
    for retry in range(max_retries):
        tool = tool_registry.get_tool('window_list')
        ctx = ToolExecutionContext(
            tool_name='window_list', arguments={'filter_process': 'notepad'},
            correlation_id='verify-closed', user_id='test', permissions_granted=['app:control'],
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        remaining = result.result.get("total", 0)
        print(f'   Notepad windows after force close (retry {retry+1}): {remaining}')
        if remaining == 0:
            break
        # Force kill again
        subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"], capture_output=True)
        await asyncio.sleep(1)
    else:
        print(f'   WARNING: Could not close all Notepad instances after {max_retries} retries')
    
    # ============================================================
    # Step 2: Launch Notepad and get the ACTUAL window title
    # ============================================================
    print('\n2. Launching Notepad...')
    result = await auto.launch_application('notepad', wait_for_window=True, timeout=30)
    print(f'   Launch: {result.success}')
    notepad_window_title = "Untitled - Notepad"  # Default for new Notepad
    notepad_hwnd = None
    pid = None
    if result.success:
        pid = result.details.get('process_id')
        print(f'   PID: {pid}')
        if result.details.get('window'):
            w = result.details['window']
            print(f'   Window: "{w["title"]}" (HWND: {w["hwnd"]})')
            notepad_window_title = w["title"]
            notepad_hwnd = w["hwnd"]
    
    # Wait for window to fully appear
    await asyncio.sleep(2)
    
    # Verify the launched window - USE the title from launch result
    # DO NOT re-query window_list as it may find stale windows
    print(f'   Using window title from launch: "{notepad_window_title}"')
    
    # ============================================================
    # DEBUG: Test _connect_window directly
    # ============================================================
    print('\nDEBUG: Testing _connect_window directly...')
    wintool = WindowInteractTool()
    window = wintool._connect_window(title=notepad_window_title)
    if window:
        print(f'   _connect_window SUCCESS: "{window.window_text()}"')
        for ctrl in window.descendants():
            try:
                if ctrl.element_info.control_type == 'Document':
                    print(f'   Found Document control: {ctrl.element_info.class_name}')
            except:
                pass
    else:
        print(f'   _connect_window FAILED for title: "{notepad_window_title}"')
    
    # ============================================================
    # Helper to get current Notepad window title using PID from launch
    # ============================================================
    async def get_current_notepad_title():
        # Use the PID from launch to find the correct window
        tool = tool_registry.get_tool('window_list')
        ctx = ToolExecutionContext(
            tool_name='window_list', arguments={'filter_process': 'notepad'},
            correlation_id='get-title', user_id='test', permissions_granted=['app:control'],
            consent_manager=consent_manager
        )
        result = await tool.execute(ctx)
        if result.success and result.result['windows']:
            for w in result.result['windows']:
                # Match by PID from our launch to avoid stale windows
                if w["is_visible"] and w["process_id"] == pid:
                    return w["title"]
        return notepad_window_title  # fallback
    
    # ============================================================
    # Step 3: Focus Notepad window using HWND (more reliable)
    # ============================================================
    print('\n3. Focusing Notepad...')
    # Get the HWND from the launch result
    if notepad_hwnd:
        print(f'   Using HWND: {notepad_hwnd}')
        result = await auto.focus_window(notepad_hwnd, verify=True, is_hwnd=True)
    else:
        result = await auto.focus_window(notepad_window_title, verify=True)
    print(f'   Focus: {result.success}, Verified: {result.verification_passed}')
    if not result.success:
        print(f'   Error: {result.error}')
        # Try to find the correct window title
        notepad_window_title = await get_current_notepad_title()
        print(f'   Retrying with detected title: "{notepad_window_title}"')
        result = await auto.focus_window(notepad_window_title, verify=True)
        print(f'   Focus retry: {result.success}, Verified: {result.verification_passed}')
    
    await asyncio.sleep(1)
    
    # ============================================================
    # Step 3b: Open new tab in Notepad (click "+" button) to get fresh document
    # ============================================================
    print('\n3b. Opening new tab in Notepad (click "+" button)...')
    # Use the new open_new_tab method with HWND
    if result.details.get('window'):
        hwnd = result.details['window']['hwnd']
        result = await auto.open_new_tab_by_hwnd(hwnd)
    else:
        new_tab_button = {"type": "Button", "title": "New tab", "index": 0}
        result = await auto.ui_click(notepad_window_title, new_tab_button)
        if not result.success:
            new_tab_button = {"type": "Button", "title": "+", "index": 0}
            result = await auto.ui_click(notepad_window_title, new_tab_button)
        if not result.success:
            new_tab_button = {"type": "Button", "class_name": "TabButton", "index": 0}
            result = await auto.ui_click(notepad_window_title, new_tab_button)
    print(f'   New tab: {result.success}' if result.success else f'   New tab failed: {result.error}')
    
    # Re-detect window title after new tab (should be "Untitled - Notepad")
    await asyncio.sleep(0.5)
    notepad_window_title = await get_current_notepad_title()
    print(f'   Updated window title: "{notepad_window_title}"')
    
    await asyncio.sleep(1)
    
    # ============================================================
    # Step 4: Type text using UI Automation
    # ============================================================
    notepad_ctrl = {"type": "Document", "class_name": "RichEditD2DPT", "index": 0}
    
    async def append_text_via_uia(window_title, text):
        # Get current text
        result = await auto._execute_tool("window_interact", {
            "action": "get_text", "window_title": window_title, "control_identifier": notepad_ctrl
        }, ["automation:window"])
        if not result["success"]:
            return False, result.get("error")
        current = result["result"].get("text", "")
        combined = current + text
        
        # Set combined text
        result = await auto._execute_tool("window_interact", {
            "action": "type", "window_title": window_title, "control_identifier": notepad_ctrl, "text": combined
        }, ["automation:window"])
        return result["success"], result.get("error")
    
    async def verify_text_contains(window_title, fragment):
        result = await auto._execute_tool("window_interact", {
            "action": "get_text", "window_title": window_title, "control_identifier": notepad_ctrl
        }, ["automation:window"])
        if not result["success"]:
            return False
        return fragment in result["result"].get("text", "")
    
    print('\n4. Typing initial text via UI Automation...')
    success, err = await append_text_via_uia(notepad_window_title, "Hello from M Desktop Automation!\n")
    print(f'   Append: {success}' if success else f'   Append failed: {err}')
    
    # Re-detect window title after modification
    notepad_window_title = await get_current_notepad_title()
    print(f'   Updated window title: "{notepad_window_title}"')
    
    await asyncio.sleep(0.5)
    
    print('\n5. Verifying text...')
    found = await verify_text_contains(notepad_window_title, "Hello from M")
    print(f'   Contains "Hello from M": {found}')
    
    await asyncio.sleep(0.5)
    
    print('\n6. Appending more text...')
    success, err = await append_text_via_uia(notepad_window_title, "\nThis is a test of the universal desktop automation engine.\n")
    print(f'   Append: {success}' if success else f'   Append failed: {err}')
    
    # Re-detect window title after modification
    notepad_window_title = await get_current_notepad_title()
    print(f'   Updated window title: "{notepad_window_title}"')
    
    await asyncio.sleep(0.5)
    
    print('\n7. Appending final text...')
    success, err = await append_text_via_uia(notepad_window_title, "\nFinal test of universal desktop automation.\n")
    print(f'   Append: {success}' if success else f'   Append failed: {err}')
    
    # Re-detect window title after modification
    notepad_window_title = await get_current_notepad_title()
    print(f'   Updated window title: "{notepad_window_title}"')
    
    await asyncio.sleep(0.5)
    
    print('\n8. Final verification...')
    checks = {
        'Hello from M': await verify_text_contains(notepad_window_title, "Hello from M"),
        'universal desktop automation': await verify_text_contains(notepad_window_title, "universal desktop automation"),
        'Final test': await verify_text_contains(notepad_window_title, "Final test")
    }
    for check, passed in checks.items():
        print(f'   Contains "{check}": {passed}')
    
    # ============================================================
    # Step 9: Save file using the new save_file_with_fallback method
    # ============================================================
    # Use the same filename generated at the start
    print(f'\n9. Saving file as "{filename}" using save_file_with_fallback...')
    result = await auto.save_file_with_fallback(notepad_window_title, filename, process_name="notepad")
    print(f'   Save: {result.success}')
    if result.success:
        print(f'   File saved to: {result.details.get("file_path")}')
    else:
        print(f'   Error: {result.error}')
        print(f'   Details: {result.details}')
    
    # ============================================================
    # Step 10: Verify saved file
    # ============================================================
    print('\n10. Verifying saved file...')
    if result.success and result.details.get("file_path"):
        path = result.details["file_path"]
        print(f'   File found at: {path}')
        size = os.path.getsize(path)
        print(f'   File size: {size} bytes')
        with open(path, 'r') as f:
            content = f.read()
        print(f'   Content length: {len(content)}')
        print(f'   Content preview: {repr(content[:300])}')
        print(f'   Contains Hello from M: {"Hello from M" in content}')
        print(f'   Contains universal desktop automation: {"universal desktop automation" in content}')
        print(f'   Contains Final test: {"Final test" in content}')
    else:
        # Search for the file using the ORIGINAL filename
        possible_paths = [
            os.path.expanduser(f'~/Desktop/{filename}'),
            os.path.expanduser(f'~/Documents/{filename}'),
            os.path.expanduser(f'~/OneDrive/Desktop/{filename}'),
            os.path.expanduser(f'~/OneDrive/Desktop/ALIYAJSHAIKH/{filename}'),
        ]
        # Also check any subfolders in OneDrive Desktop
        onedrive_desktop = os.path.expanduser('~/OneDrive/Desktop')
        if os.path.exists(onedrive_desktop):
            for root, dirs, files in os.walk(onedrive_desktop):
                if filename in files:
                    possible_paths.append(os.path.join(root, filename))
        
        found = False
        for path in possible_paths:
            if os.path.exists(path):
                print(f'   File found at: {path}')
                size = os.path.getsize(path)
                print(f'   File size: {size} bytes')
                with open(path, 'r') as f:
                    content = f.read()
                print(f'   Content length: {len(content)}')
                print(f'   Content preview: {repr(content[:300])}')
                found = True
                break
        if not found:
            print('   File NOT found in common locations')
    
    # ============================================================
    # Step 11: Close Notepad (force close all instances)
    # ============================================================
    print('\n11. Closing Notepad...')
    result = await auto.close_all_instances('notepad.exe')
    print(f'   Close: {result.success}')
    if result.details:
        print(f'   Details: {result.details}')
    
    # Verify closure
    await asyncio.sleep(1)
    app_info = await auto.get_application_info('notepad.exe')
    if app_info and app_info.windows:
        print(f'   WARNING: {len(app_info.windows)} Notepad window(s) still open!')
        for w in app_info.windows:
            print(f'     - "{w.title}" (PID: {w.process_id})')
    else:
        print('   All Notepad instances closed successfully')
    
    print('\n' + '=' * 60)
    print('TEST COMPLETE')
    print('=' * 60)

asyncio.run(test_complete_workflow())