"""
Genuine End-to-End Visual Action Loop Test
Tests NVIDIA Vision (nvidia/nemotron-3.5-super-vl-120b-a12b) and Gemini Vision (gemini-2.5-flash)
with real Windows desktop screenshots.
"""
import asyncio
import sys
import os
import base64
import json
import time
from pathlib import Path
from PIL import Image

sys.path.insert(0, 'src')

from m_core.desktop.automation import create_automation
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.model_providers.vision_provider import get_vision_factory, VisionProviderType, NVIDIANIMVisionProvider, GeminiVisionProvider
from m_core.config import settings


async def capture_screenshot_base64(auto, mode="full", window_title=None):
    """Capture screenshot and return base64."""
    args = {
        "mode": mode, 
        "output_format": "png", 
        "save_to_file": False
    }
    if window_title:
        args["window_title"] = window_title
    
    result = await auto._execute_tool("screen_capture", args, ["screen:capture"])
    
    if result["success"] and "image_base64" in result["result"]:
        return result["result"]["image_base64"]
    elif result["success"] and "image_path" in result["result"]:
        with open(result["result"]["image_path"], "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    return None


async def test_provider_vision_loop(provider_name, vision_factory, auto):
    """Run the complete visual action loop for a specific provider."""
    print(f"\n{'='*60}")
    print(f"TESTING {provider_name.upper()} VISION PROVIDER")
    print(f"{'='*60}")
    
    provider = vision_factory._providers.get(provider_name)
    if not provider or not provider._initialized:
        print(f"[FAIL] {provider_name} provider not available or not initialized")
        return False
    
    print(f"[OK] Provider: {provider.provider_name}")
    if isinstance(provider, NVIDIANIMVisionProvider):
        print(f"   Model: {provider.config.nvidia_vision_model}")
    elif isinstance(provider, GeminiVisionProvider):
        print(f"   Model: {provider.config.gemini_vision_model}")
    else:
        print(f"   Model: unknown")
    
    # Step 1: Capture initial screenshot
    print("\n[1] Capturing initial desktop screenshot...")
    screenshot_before = await capture_screenshot_base64(auto, mode="full")
    if not screenshot_before:
        print("[FAIL] Failed to capture screenshot")
        return False
    print(f"[OK] Screenshot captured ({len(screenshot_before)} chars base64)")
    
    # Step 2: Analyze screen for interactive elements
    print("\n[2] Analyzing screen for interactive elements...")
    analysis = await provider.analyze_screen(
        screenshot_base64=screenshot_before,
        task_description="Identify all visible interactive elements on the Windows desktop: buttons, icons, taskbar items, window controls, start menu, etc. Return structured JSON with bounding boxes."
    )
    
    print(f"   Description: {analysis.description[:200]}")
    print(f"   Elements found: {len(analysis.elements)}")
    print(f"   App type: {analysis.app_type}")
    print(f"   Model: {analysis.model_type}")
    
    if analysis.elements:
        print("\n   Top 5 elements:")
        for i, elem in enumerate(analysis.elements[:5]):
            bbox = elem.get("bbox", {})
            print(f"   {i+1}. {elem.get('type')} - '{elem.get('label')}' @ ({bbox.get('left',0)},{bbox.get('top',0)}) {bbox.get('width',0)}x{bbox.get('height',0)} conf={elem.get('confidence',0):.2f}")
    
    if not analysis.elements:
        print("[FAIL] No interactive elements detected")
        return False
    
    # Step 3: Select an element to click (prefer taskbar/start button)
    target_element = None
    for elem in analysis.elements:
        elem_type = elem.get("type", "").lower()
        label = elem.get("label", "").lower()
        if "start" in label or "taskbar" in elem_type or "button" in elem_type:
            target_element = elem
            break
    
    if not target_element:
        target_element = analysis.elements[0]
    
    bbox = target_element.get("bbox", {})
    click_x = bbox.get("left", 0) + bbox.get("width", 0) // 2
    click_y = bbox.get("top", 0) + bbox.get("height", 0) // 2
    
    print(f"\n[3] Selected target - {target_element.get('type')} '{target_element.get('label')}'")
    print(f"   Click coordinates: ({click_x}, {click_y})")
    print(f"   Bounding box: {bbox}")
    
    # Verify coordinates are within screen bounds
    import win32api
    screen_w = win32api.GetSystemMetrics(0)
    screen_h = win32api.GetSystemMetrics(1)
    print(f"   Screen resolution: {screen_w}x{screen_h}")
    
    if not (0 <= click_x < screen_w and 0 <= click_y < screen_h):
        print("[FAIL] Coordinates out of bounds!")
        return False
    print("[OK] Coordinates within bounds")
    
    # Step 4: Execute click action
    print(f"\n[4] Executing mouse click at ({click_x}, {click_y})...")
    result = await auto._execute_tool("mouse_control", {
        "action": "click",
        "x": click_x,
        "y": click_y,
        "button": "left"
    }, ["automation:input"])
    
    if not result.get("success"):
        print(f"[FAIL] Click failed: {result.get('error')}")
        return False
    print("[OK] Click executed successfully")
    
    # Step 5: Wait for UI to respond
    print("\n[5] Waiting for UI response...")
    await asyncio.sleep(2)
    
    # Step 6: Capture post-action screenshot
    print("\n[6] Capturing post-action screenshot...")
    screenshot_after = await capture_screenshot_base64(auto, mode="full")
    if not screenshot_after:
        print("[FAIL] Failed to capture post-action screenshot")
        return False
    print(f"[OK] Post-action screenshot captured ({len(screenshot_after)} chars)")
    
    # Step 7: Verify action result with vision
    print("\n[7] Verifying visual change with vision model...")
    verification = await provider.verify_action(
        before_screenshot=screenshot_before,
        after_screenshot=screenshot_after,
        expected_change=f"Clicked on {target_element.get('type')} '{target_element.get('label')}' at ({click_x},{click_y}). The UI should show a response (menu open, button pressed, window focus change, etc.)"
    )
    
    verified = verification.get('verified', False)
    confidence = verification.get('confidence', 0)
    reasoning = verification.get('reasoning', 'N/A')
    
    print(f"   Verified: {verified}")
    print(f"   Confidence: {confidence:.2f}")
    print(f"   Reasoning: {reasoning[:200]}")
    print(f"   Before elements: {verification.get('before_elements_count', 'N/A')}")
    print(f"   After elements: {verification.get('after_elements_count', 'N/A')}")
    
    success = verified and confidence > 0.5
    
    print(f"\n{'='*60}")
    if success:
        print(f"[SUCCESS] {provider_name.upper()} VISUAL ACTION LOOP: PASSED")
    else:
        print(f"[FAIL] {provider_name.upper()} VISUAL ACTION LOOP: FAILED")
    print(f"{'='*60}")
    
    return success


async def main():
    print("="*60)
    print("NVIDIA & GEMINI VISION END-TO-END ACCEPTANCE TEST")
    print("="*60)
    
    # Grant permissions
    for cap in ['app:launch', 'app:control', 'automation:window', 'automation:input', 'screen:capture', 'screen:ocr', 'screen:find', 'screen:verify']:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # Create automation
    auto = await create_automation('test-user')
    
    # Get vision factory
    vision_factory = await get_vision_factory()
    print(f"\nVision Factory initialized: {vision_factory._initialized}")
    print(f"Available providers: {[p.value for p in vision_factory._providers.keys()]}")
    print(f"Current provider: {vision_factory._current_provider.provider_name if vision_factory._current_provider else 'None'}")
    
    results = {}
    
    # Test NVIDIA Vision (primary)
    if VisionProviderType.NVIDIA_NIM in vision_factory._providers:
        results["nvidia_nim"] = await test_provider_vision_loop(
            "nvidia_nim", vision_factory, auto
        )
    
    # Test Gemini Vision (fallback)
    if VisionProviderType.GEMINI in vision_factory._providers:
        # Switch to Gemini
        await vision_factory.switch_provider(VisionProviderType.GEMINI)
        results["gemini"] = await test_provider_vision_loop(
            "gemini", vision_factory, auto
        )
    
    # Final Report
    print("\n" + "="*60)
    print("FINAL ACCEPTANCE TEST REPORT")
    print("="*60)
    
    for provider, success in results.items():
        status = "[PASS]" if success else "[FAIL]"
        print(f"  {provider}: {status}")
    
    all_pass = all(results.values())
    print(f"\nOverall: {'ALL PROVIDERS PASSED' if all_pass else 'SOME PROVIDERS FAILED'}")
    
    # Verify .env not in git
    print("\n[Security] Checking .env not in git...")
    import subprocess
    git_check = subprocess.run(["git", "ls-files", ".env"], capture_output=True, text=True, cwd="C:/agent M")
    if git_check.stdout.strip():
        print("  [FAIL] .env IS TRACKED BY GIT!")
    else:
        print("  [OK] .env is NOT tracked by Git (in .gitignore)")
    
    return all_pass


if __name__ == "__main__":
    asyncio.run(main())