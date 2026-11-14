"""
M Desktop Automation Package

Universal Windows desktop automation engine with Blueprint intelligence.
"""

from .automation import (
    DesktopAutomation,
    DesktopObservation,
    ApplicationInfo,
    WindowInfo,
    InteractionResult,
    ApplicationState,
    InteractionMethod,
    desktop_automation,
    create_automation,
)

from .blueprint import (
    BlueprintManager,
    BlueprintStore,
    ApplicationBlueprint,
    BlueprintElement,
    BlueprintAction,
    BlueprintStatus,
    ElementType,
    create_blueprint_manager
)

__all__ = [
    "DesktopAutomation",
    "DesktopObservation",
    "ApplicationInfo", 
    "WindowInfo",
    "InteractionResult",
    "ApplicationState",
    "InteractionMethod",
    "desktop_automation",
    "create_automation",
    "BlueprintManager",
    "BlueprintStore",
    "ApplicationBlueprint",
    "BlueprintElement",
    "BlueprintAction",
    "BlueprintStatus",
    "ElementType",
    "create_blueprint_manager"
]