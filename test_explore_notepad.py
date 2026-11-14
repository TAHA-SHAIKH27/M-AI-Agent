import asyncio
import sys
sys.path.insert(0, 'src')
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope

async def explore_notepad_ui():
    # Grant permissions
    for cap in ['app:control', 'automation:window', 'app:launch']:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    user_id = 'test'
    
    # Launch Notepad first
    tool = tool_registry.get_tool('app_launch')
    ctx = ToolExecutionContext(
        tool_name='app_launch', 
        arguments={'app_name': 'notepad', 'approval_token': f'auto-{user_id}-launch'},
        correlation_id='launch', user_id=user_id, permissions_granted=['app:launch'],
        consent_manager=consent_manager
    )
    result = await tool.execute(ctx)
    print(f'Launch: {result.success}, PID: {result.result.get("process_id")}')
    
    await asyncio.sleep(2)
    
    # List all windows to find Notepad
    tool = tool_registry.get_tool('window_list')
    ctx = ToolExecutionContext(
        tool_name='window_list', arguments={'filter_process': 'notepad'},
        correlation_id='find-notepad', user_id=user_id, permissions_granted=['app:control'],
        consent_manager=consent_manager
    )
    result = await tool.execute(ctx)
    print(f'Notepad windows: {result.result.get("total")}')
    for w in result.result.get('windows', []):
        print(f'  Title: "{w["title"]}" PID: {w["process_id"]} Visible: {w["is_visible"]}')
    
    if not result.result['windows']:
        print("No Notepad window found!")
        return
    
    notepad_title = result.result['windows'][0]['title']
    print(f'\nUsing window: "{notepad_title}"')
    
    # Now list all controls in Notepad
    tool = tool_registry.get_tool('window_interact')
    ctx = ToolExecutionContext(
        tool_name='window_interact', 
        arguments={'action': 'list_controls', 'window_title': notepad_title, 'approval_token': f'auto-{user_id}-list'},
        correlation_id='list-controls', user_id=user_id, permissions_granted=['automation:window'],
        consent_manager=consent_manager
    )
    result = await tool.execute(ctx)
    if result.success:
        controls = result.result.get('controls', [])
        print(f'\nTotal controls: {len(controls)}')
        # Look for tab-related controls
        for c in controls:
            if 'tab' in c['type'].lower() or 'tab' in c.get('title', '').lower() or c['title'] in ['+', 'New tab']:
                print(f'  TAB-RELATED: type={c["type"]} title="{c["title"]}" auto_id={c.get("auto_id")} class={c.get("class_name")}')
        # Also print all buttons
        for c in controls:
            if c['type'] == 'Button':
                print(f'  BUTTON: title="{c["title"]}" auto_id={c.get("auto_id")} class={c.get("class_name")}')
    else:
        print(f'Failed to list controls: {result.error}')

asyncio.run(explore_notepad_ui())