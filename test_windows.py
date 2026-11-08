import asyncio
import sys
import os
sys.path.insert(0, 'src')

from m_core.tools import tool_registry
from m_core.tools.schemas import ToolExecutionContext
from m_core.security import consent_manager

# Grant all required consents
print("=== Granting Consents ===")
from m_core.security import ConsentScope
for cap in ["system:info", "app:control", "app:launch", "system:admin", "file:write"]:
    consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    print(f"  Granted: {cap}")

# Test Windows process_list tool
async def test_process_list():
    print("\n=== Testing process_list ===")
    tool = tool_registry.get_tool("process_list")
    context = ToolExecutionContext(
        tool_name="process_list",
        arguments={"limit": 10, "sort_by": "cpu"},
        correlation_id="test-1",
        user_id="test-client",
        permissions_granted=["system:info"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Process count: {result.result['total']}")
        for p in result.result['processes'][:5]:
            print(f"  PID: {p['pid']}, Name: {p['name']}, CPU: {p.get('cpu_percent', 0):.1f}%")
    else:
        print(f"Error: {result.error}")

# Test Windows window_list tool
async def test_window_list():
    print("\n=== Testing window_list ===")
    tool = tool_registry.get_tool("window_list")
    context = ToolExecutionContext(
        tool_name="window_list",
        arguments={"limit": 10},
        correlation_id="test-2",
        user_id="test-client",
        permissions_granted=["app:control"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Window count: {result.result['total']}")
        for w in result.result['windows'][:5]:
            print(f"  Title: {w['title'][:50]}, PID: {w['process_id']}, Visible: {w['is_visible']}")
    else:
        print(f"Error: {result.error}")

# Test app_launch (notepad) - requires approval
async def test_app_launch():
    print("\n=== Testing app_launch (notepad) ===")
    tool = tool_registry.get_tool("app_launch")
    context = ToolExecutionContext(
        tool_name="app_launch",
        arguments={"app_name": "notepad", "approved": True},
        correlation_id="test-3",
        user_id="test-client",
        permissions_granted=["app:launch"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Process ID: {result.result['process_id']}")
        print(f"Process Name: {result.result['process_name']}")
    else:
        print(f"Error: {result.error}")

# Test app_control (list windows)
async def test_app_control():
    print("\n=== Testing app_control (list) ===")
    tool = tool_registry.get_tool("app_control")
    context = ToolExecutionContext(
        tool_name="app_control",
        arguments={"action": "list"},
        correlation_id="test-4",
        user_id="test-client",
        permissions_granted=["app:control"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Window count: {result.result['count']}")

# Test system_command (echo)
async def test_system_command():
    print("\n=== Testing system_command (echo) ===")
    tool = tool_registry.get_tool("system_command")
    context = ToolExecutionContext(
        tool_name="system_command",
        arguments={"command": "echo", "arguments": "Hello from M", "approved": True},
        correlation_id="test-5",
        user_id="test-client",
        permissions_granted=["system:admin"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Output: {result.result['stdout']}")
    else:
        print(f"Error: {result.error}")

# Test powershell (simple command) - requires approval
async def test_powershell():
    print("\n=== Testing powershell ===")
    tool = tool_registry.get_tool("powershell")
    context = ToolExecutionContext(
        tool_name="powershell",
        arguments={"command": "Get-Date", "approved": True},
        correlation_id="test-6",
        user_id="test-client",
        permissions_granted=["system:admin"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Output: {result.result['stdout']}")
    else:
        print(f"Error: {result.error}")

# Test shortcut_create
async def test_shortcut():
    print("\n=== Testing shortcut_create ===")
    tool = tool_registry.get_tool("shortcut_create")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.lnk', delete=False) as tmp:
        shortcut_path = tmp.name
    try:
        context = ToolExecutionContext(
            tool_name="shortcut_create",
            arguments={
                "target_path": "C:\\Windows\\notepad.exe",
                "shortcut_path": shortcut_path,
                "approved": True
            },
            correlation_id="test-7",
            user_id="test-client",
            permissions_granted=["file:write"],
            consent_manager=consent_manager
        )
        result = await tool.execute(context)
        print(f"Success: {result.success}")
        if result.success:
            print(f"Shortcut created: {result.result['shortcut_path']}")
            if os.path.exists(shortcut_path):
                print("  Verified: shortcut file exists")
        else:
            print(f"Error: {result.error}")
    finally:
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)

async def main():
    await test_process_list()
    await test_window_list()
    await test_app_launch()
    await test_app_control()
    await test_system_command()
    await test_powershell()
    await test_shortcut()
    print("\n=== All Windows Automation Tests Complete ===")

if __name__ == "__main__":
    asyncio.run(main())