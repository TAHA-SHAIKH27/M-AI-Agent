"""Web automation package for M AI Assistant."""

from .browser_automation import (
    BrowserAutomation,
    PlaywrightBrowser,
    BrowserType,
    InteractionMethod,
    BrowserElement,
    BrowserAction,
    BrowserTab,
    BrowserSession,
    create_browser_automation,
)

__all__ = [
    "BrowserAutomation",
    "PlaywrightBrowser",
    "BrowserType",
    "InteractionMethod",
    "BrowserElement",
    "BrowserAction",
    "BrowserTab",
    "BrowserSession",
    "create_browser_automation",
]