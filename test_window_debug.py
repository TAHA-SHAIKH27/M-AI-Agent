import asyncio
import sys
sys.path.insert(0, 'src')
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope

async def test():
    # Grant permissions
    for cap in ['app:control', 'system:info', 'app:launch']:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    user_id = 'test'
    cid = 'launch-test'
    
    # Launch notepad first with approval token
    tool = tool_registry.get_tool('app_launch')
    ctx = ToolExecutionContext(
        tool_name='app_launch', 
        arguments={'app_name': 'notepad', 'approval_token': f'auto-{user_id}-{cid}'},
        correlation_id=cid, user_id=user_id, permissions_granted=['app:launch'],
        consent_manager=consent_manager
    )
    result = await tool.execute(ctx)
    print(f'Launch: {result.success}, PID: {result.result.get("process_id") if result.result else None}')
    
    await asyncio.sleep(2)
    
    # List ALL windows
    tool = tool_registry.get_tool('window_list')
    ctx = ToolExecutionContext(
        tool_name='window_list', arguments={},
        correlation_id='test', user_id=user_id, permissions_granted=['app:control'],
        consent_manager=consent_manager
    )
    result = await tool.execute(ctx)
    print(f'Success: {result.success}')
    print(f'Total windows: {result.result.get("total")}')
    for w in result.result.get('windows', [])[:10]:
        if 'notepad' in w['process_name'].lower() or 'notepad' in w['title'].lower():
            print(f'  Notepad: title="{w["title"]}" process="{w["process_name"]}" pid={w["process_id"]} visible={w["is_visible"]}')

asyncio.run(test())