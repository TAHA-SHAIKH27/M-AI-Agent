"""Tools package for M AI Assistant."""
from .schemas import (
    ToolManifest,
    ToolInvocation,
    ToolResult,
    ToolExecutionContext,
    ToolRiskLevel,
    ToolCategory,
)
from .registry import ToolRegistry, tool_registry
from .base import BaseTool
from .builtins import (
    SystemInfoTool,
    FileReadTool,
    FileListTool,
    FileSearchTool,
    FileWriteTool,
    FileDeleteTool,
    MemoryReadTool,
    MemorySearchTool,
    WebSearchTool,
    AppLaunchTool,
    AppControlTool,
    PowerShellTool,
    ProcessListTool,
    WindowListTool,
    WindowsShortcutTool,
    SystemCommandTool,
    VoiceRecordTool,
    VoiceTranscribeTool,
    VoiceSpeakTool,
    WakeWordListenTool,
    ScreenCaptureTool,
    ScreenOCRTool,
    VisualFindTool,
    MouseControlTool,
    KeyboardInputTool,
    WindowInteractTool,
    VisualVerifyTool,
    BUILTIN_TOOLS,
)

# Register built-in tools
def register_builtin_tools():
    """Register all built-in tools with the tool registry."""
    from .registry import tool_registry
    from .builtins import (
        SystemInfoTool,
        FileReadTool,
        FileListTool,
        FileSearchTool,
        FileWriteTool,
        FileDeleteTool,
        MemoryReadTool,
        MemorySearchTool,
        WebSearchTool,
        AppLaunchTool,
        AppControlTool,
        PowerShellTool,
        ProcessListTool,
        WindowListTool,
        WindowsShortcutTool,
        SystemCommandTool,
        VoiceRecordTool,
        VoiceTranscribeTool,
        VoiceSpeakTool,
        WakeWordListenTool,
        ScreenCaptureTool,
        ScreenOCRTool,
        VisualFindTool,
        MouseControlTool,
        KeyboardInputTool,
        WindowInteractTool,
        VisualVerifyTool,
    )
    
    # Register each tool
    tool_registry.register_tool_class(SystemInfoTool)
    tool_registry.register_tool_class(FileReadTool)
    tool_registry.register_tool_class(FileListTool)
    tool_registry.register_tool_class(FileSearchTool)
    tool_registry.register_tool_class(FileWriteTool)
    tool_registry.register_tool_class(FileDeleteTool)
    tool_registry.register_tool_class(MemoryReadTool)
    tool_registry.register_tool_class(MemorySearchTool)
    tool_registry.register_tool_class(WebSearchTool)
    tool_registry.register_tool_class(AppLaunchTool)
    tool_registry.register_tool_class(AppControlTool)
    tool_registry.register_tool_class(PowerShellTool)
    tool_registry.register_tool_class(ProcessListTool)
    tool_registry.register_tool_class(WindowListTool)
    tool_registry.register_tool_class(WindowsShortcutTool)
    tool_registry.register_tool_class(SystemCommandTool)
    tool_registry.register_tool_class(VoiceRecordTool)
    tool_registry.register_tool_class(VoiceTranscribeTool)
    tool_registry.register_tool_class(VoiceSpeakTool)
    tool_registry.register_tool_class(WakeWordListenTool)
    tool_registry.register_tool_class(ScreenCaptureTool)
    tool_registry.register_tool_class(ScreenOCRTool)
    tool_registry.register_tool_class(VisualFindTool)
    tool_registry.register_tool_class(MouseControlTool)
    tool_registry.register_tool_class(KeyboardInputTool)
    tool_registry.register_tool_class(WindowInteractTool)
    tool_registry.register_tool_class(VisualVerifyTool)
    
    return tool_registry


# Auto-register on import
register_builtin_tools()

__all__ = [
    # Schemas
    "ToolManifest",
    "ToolInvocation",
    "ToolResult",
    "ToolExecutionContext",
    "ToolRiskLevel",
    "ToolCategory",
    # Registry
    "ToolRegistry",
    "tool_registry",
    # Base
    "BaseTool",
    # Builtins
    "SystemInfoTool",
    "FileReadTool",
    "FileListTool",
    "FileSearchTool",
    "FileWriteTool",
    "FileDeleteTool",
    "MemoryReadTool",
    "MemorySearchTool",
    "WebSearchTool",
    "AppLaunchTool",
    "AppControlTool",
    "PowerShellTool",
    "ProcessListTool",
    "WindowListTool",
    "WindowsShortcutTool",
    "SystemCommandTool",
    "VoiceRecordTool",
    "VoiceTranscribeTool",
    "VoiceSpeakTool",
    "WakeWordListenTool",
    "ScreenCaptureTool",
    "ScreenOCRTool",
    "VisualFindTool",
    "MouseControlTool",
    "KeyboardInputTool",
    "WindowInteractTool",
    "VisualVerifyTool",
    "BUILTIN_TOOLS",
    # Registration
    "register_builtin_tools",
]