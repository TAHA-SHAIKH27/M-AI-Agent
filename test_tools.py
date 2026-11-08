import sys
sys.path.insert(0, 'src')
from m_core.tools import tool_registry

# Test Windows tools
print('=== Windows Tools ===')
for name in ['app_launch', 'app_control', 'powershell', 'process_list', 'window_list', 'shortcut_create', 'system_command']:
    manifest = tool_registry.get_manifest(name)
    print(f'  {name}: {manifest["display_name"]} (risk: {manifest["risk_level"]})')

# Test Voice tools
print('=== Voice Tools ===')
for name in ['voice_record', 'voice_transcribe', 'voice_speak', 'wake_word_listen']:
    manifest = tool_registry.get_manifest(name)
    print(f'  {name}: {manifest["display_name"]} (risk: {manifest["risk_level"]})')