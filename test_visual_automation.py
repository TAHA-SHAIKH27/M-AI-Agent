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
for cap in ["system:info", "app:control", "app:launch", "system:admin", "file:write", 
            "voice:tts", "voice:stt", "voice:mic", "screen:capture", "screen:ocr", 
            "screen:find", "automation:input", "automation:window", "screen:verify"]:
    consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    print(f"  Granted: {cap}")

async def test_screen_capture():
    print("\n=== Testing screen_capture ===")
    tool = tool_registry.get_tool("screen_capture")
    context = ToolExecutionContext(
        tool_name="screen_capture",
        arguments={"mode": "full", "output_format": "png"},
        correlation_id="test-1",
        user_id="test-client",
        permissions_granted=["screen:capture"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Image path: {result.result['image_path']}")
        print(f"  Size: {result.result['width']}x{result.result['height']}")
        if os.path.exists(result.result['image_path']):
            print("  Verified: file exists")
        else:
            print("  ERROR: file does not exist")
    else:
        print(f"  Error: {result.error}")

async def test_screen_capture_window():
    print("\n=== Testing screen_capture (window mode) ===")
    tool = tool_registry.get_tool("screen_capture")
    context = ToolExecutionContext(
        tool_name="screen_capture",
        arguments={"mode": "window", "window_title": "Notepad"},
        correlation_id="test-2",
        user_id="test-client",
        permissions_granted=["screen:capture"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Image path: {result.result['image_path']}")
        print(f"  Size: {result.result['width']}x{result.result['height']}")
    else:
        print(f"  Error: {result.error}")

async def test_screen_ocr():
    print("\n=== Testing screen_ocr ===")
    tool = tool_registry.get_tool("screen_ocr")
    context = ToolExecutionContext(
        tool_name="screen_ocr",
        arguments={"source": "screen", "language": "eng"},
        correlation_id="test-3",
        user_id="test-client",
        permissions_granted=["screen:ocr"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Text length: {len(result.result['text'])}")
        print(f"  Confidence: {result.result['confidence']:.2f}")
        print(f"  Words found: {len(result.result['words'])}")
    else:
        print(f"  Error: {result.error}")

async def test_visual_find():
    print("\n=== Testing visual_find (text method) ===")
    tool = tool_registry.get_tool("visual_find")
    context = ToolExecutionContext(
        tool_name="visual_find",
        arguments={"method": "text", "search_text": "the", "threshold": 0.5},
        correlation_id="test-4",
        user_id="test-client",
        permissions_granted=["screen:find"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Matches found: {len(result.result['matches'])}")
        for m in result.result['matches'][:3]:
            print(f"  Match: ({m['x']}, {m['y']}) size: {m['width']}x{m['height']} conf: {m['confidence']:.2f}")
    else:
        print(f"  Error: {result.error}")

async def test_visual_verify():
    print("\n=== Testing visual_verify ===")
    tool = tool_registry.get_tool("visual_verify")
    context = ToolExecutionContext(
        tool_name="visual_verify",
        arguments={"check_type": "text_present", "search_text": "Windows", "threshold": 0.5},
        correlation_id="test-5",
        user_id="test-client",
        permissions_granted=["screen:verify"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Passed: {result.result['passed']}")
        print(f"  Actual: {result.result['actual']}")
        print(f"  Confidence: {result.result['confidence']:.2f}")
    else:
        print(f"  Error: {result.error}")

async def test_mouse_control():
    print("\n=== Testing mouse_control (position only) ===")
    tool = tool_registry.get_tool("mouse_control")
    context = ToolExecutionContext(
        tool_name="mouse_control",
        arguments={"action": "position"},
        correlation_id="test-6",
        user_id="test-client",
        permissions_granted=["automation:input"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Position: {result.result['position']}")
    else:
        print(f"  Error: {result.error}")

async def test_keyboard_input():
    print("\n=== Testing keyboard_input (position only - no approval) ===")
    tool = tool_registry.get_tool("keyboard_input")
    # This should fail because no approval for press/type
    context = ToolExecutionContext(
        tool_name="keyboard_input",
        arguments={"action": "press", "key": "enter"},
        correlation_id="test-7",
        user_id="test-client",
        permissions_granted=["automation:input"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if not result.success:
        print(f"  Expected error (no approval): {result.error}")

async def test_window_interact():
    print("\n=== Testing window_interact (list_controls) ===")
    tool = tool_registry.get_tool("window_interact")
    context = ToolExecutionContext(
        tool_name="window_interact",
        arguments={"action": "list_controls", "window_title": "Notepad"},
        correlation_id="test-8",
        user_id="test-client",
        permissions_granted=["automation:window"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Controls found: {len(result.result['controls'])}")
        for c in result.result['controls'][:3]:
            print(f"  Control: {c['type']} - {c['title'][:30]}")
    else:
        print(f"  Error: {result.error}")

async def test_visual_verify_pixel():
    print("\n=== Testing visual_verify (pixel_color) ===")
    tool = tool_registry.get_tool("visual_verify")
    context = ToolExecutionContext(
        tool_name="visual_verify",
        arguments={
            "check_type": "pixel_color",
            "expected_color": [255, 255, 255],
            "coordinates": {"x": 100, "y": 100}
        },
        correlation_id="test-9",
        user_id="test-client",
        permissions_granted=["screen:verify"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"  Passed: {result.result['passed']}")
        print(f"  Actual: {result.result['actual']}")
        print(f"  Confidence: {result.result['confidence']:.2f}")
    else:
        print(f"  Error: {result.error}")

async def main():
    await test_screen_capture()
    await test_screen_capture_window()
    await test_screen_ocr()
    await test_visual_find()
    await test_visual_verify()
    await test_mouse_control()
    await test_keyboard_input()
    await test_window_interact()
    await test_visual_verify_pixel()
    print("\n=== All Visual Automation Tests Complete ===")

if __name__ == "__main__":
    asyncio.run(main())