import asyncio
import sys
import os
sys.path.insert(0, 'src')

from m_core.tools import tool_registry
from m_core.tools.schemas import ToolExecutionContext
from m_core.security import consent_manager
from m_core.tools.builtins.voice import voice_manager, EDGE_TTS_AVAILABLE, FASTER_WHISPER_AVAILABLE

async def test_voice_speak():
    print("=== Testing voice_speak (Edge TTS) ===")
    # Initialize voice manager with Edge TTS
    await voice_manager.initialize(tts_provider="edge", tts_voice="en-US-AriaNeural")
    
    tool = tool_registry.get_tool("voice_speak")
    context = ToolExecutionContext(
        tool_name="voice_speak",
        arguments={
            "text": "Hello! This is M speaking through Edge TTS.",
            "voice": "en-US-AriaNeural",
            "rate": "+0%",
            "volume": "+0%"
        },
        correlation_id="voice-1",
        user_id="test-client",
        permissions_granted=["voice:tts"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Audio file: {result.result['audio_file']}")
        print(f"Duration estimate: {result.result['duration_estimate']:.1f}s")
        # Verify file exists
        if os.path.exists(result.result['audio_file']):
            print("  Verified: audio file exists")
            size = os.path.getsize(result.result['audio_file'])
            print(f"  File size: {size} bytes")
        else:
            print("  ERROR: audio file does not exist")
    else:
        print(f"Error: {result.error}")

async def test_voice_speak_different_voice():
    print("\n=== Testing voice_speak (different voice) ===")
    tool = tool_registry.get_tool("voice_speak")
    context = ToolExecutionContext(
        tool_name="voice_speak",
        arguments={
            "text": "Testing a different voice.",
            "voice": "en-US-GuyNeural",
            "rate": "+10%",
            "volume": "+0%"
        },
        correlation_id="voice-2",
        user_id="test-client",
        permissions_granted=["voice:tts"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"Success: {result.success}")
    if result.success:
        print(f"Audio file: {result.result['audio_file']}")
        if os.path.exists(result.result['audio_file']):
            print("  Verified: audio file exists")
        else:
            print("  ERROR: audio file does not exist")
    else:
        print(f"Error: {result.error}")

async def main():
    print(f"EDGE_TTS_AVAILABLE: {EDGE_TTS_AVAILABLE}")
    print(f"FASTER_WHISPER_AVAILABLE: {FASTER_WHISPER_AVAILABLE}")
    
    # Grant consent for TTS
    from m_core.security import ConsentScope
    consent_manager.grant("voice:tts", scope=ConsentScope.GLOBAL, ttl_hours=1)
    print("Granted: voice:tts")
    
    await test_voice_speak()
    await test_voice_speak_different_voice()
    
    print("\n=== Voice AI Tests Complete ===")

if __name__ == "__main__":
    asyncio.run(main())