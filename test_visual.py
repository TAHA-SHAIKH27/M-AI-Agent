import sys
sys.path.insert(0, 'src')
from m_core.tools import tool_registry
tools = tool_registry.list_tools()
print('Total tools:', len(tools))
visual_tools = [t for t in tools if t['category'] == 'visual']
print('Visual tools:')
for t in visual_tools:
    print('  ' + t['name'] + ': ' + t['display_name'] + ' (risk: ' + t['risk_level'] + ')')