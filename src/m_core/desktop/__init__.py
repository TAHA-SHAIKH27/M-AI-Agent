"""
M Desktop Automation Package

Universal Windows desktop automation engine.
"""

from .automation import (
    DesktopAutomation,
    ApplicationInfo,
    WindowInfo,
    InteractionResult,
    ApplicationState,
    InteractionMethod,
    create_automation
)

__all__ = [
    "DesktopAutomation",
    "ApplicationInfo", 
    "WindowInfo",
    "InteractionResult",
    "ApplicationState",
    "InteractionMethod",
    "create_automation"
]