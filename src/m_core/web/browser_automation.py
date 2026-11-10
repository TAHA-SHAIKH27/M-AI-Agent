"""Browser Automation for M AI Assistant.

Provides high-level browser automation using Playwright with vision fallback.
Supports Chrome, Edge, Firefox, and WebKit.
"""
import asyncio
import base64
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

import structlog

from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope

logger = structlog.get_logger(__name__)


class BrowserType(str, Enum):
    """Supported browser types."""
    CHROMIUM = "chromium"
    CHROME = "chrome"
    EDGE = "msedge"
    FIREFOX = "firefox"
    WEBKIT = "webkit"


class InteractionMethod(str, Enum):
    """Method used for browser interaction."""
    PLAYWRIGHT_DOM = "playwright_dom"
    PLAYWRIGHT_ACCESSIBILITY = "playwright_accessibility"
    VISION_FALLBACK = "vision_fallback"
    VISION_GROUNDING = "vision_grounding"


@dataclass
class BrowserElement:
    """A web element found by the browser automation."""
    element_id: str
    selector: str  # CSS/XPath selector
    tag_name: str
    text: str = ""
    attributes: Dict[str, str] = field(default_factory=dict)
    bounding_box: Optional[Dict[str, int]] = None
    is_visible: bool = True
    is_enabled: bool = True
    tag_type: str = ""  # button, input, link, etc.
    confidence: float = 1.0
    found_by: str = "dom"  # dom, accessibility, vision
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BrowserAction:
    """A browser action to execute."""
    action_type: str  # click, type, navigate, select, scroll, wait, etc.
    selector: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None
    key: Optional[str] = None
    keys: List[str] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class BrowserTab:
    """Information about a browser tab."""
    tab_id: str
    url: str
    title: str
    is_active: bool = False
    is_loading: bool = False


@dataclass
class BrowserSession:
    """A browser session with state."""
    session_id: str
    browser_type: BrowserType
    headless: bool
    tabs: List[BrowserTab] = field(default_factory=list)
    current_tab: Optional[BrowserTab] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    last_activity: int = field(default_factory=lambda: int(time.time()))


class PlaywrightBrowser:
    """Playwright-based browser automation."""
    
    def __init__(
        self,
        browser_type: BrowserType = BrowserType.CHROMIUM,
        headless: bool = False,
        viewport: Optional[Dict[str, int]] = None,
        user_agent: Optional[str] = None,
        downloads_path: Optional[Path] = None,
    ):
        self.browser_type = browser_type
        self.headless = headless
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.downloads_path = downloads_path or Path.home() / "Downloads"
        
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._initialized = False
        self._session: Optional[BrowserSession] = None
    
    async def initialize(self) -> None:
        """Initialize Playwright and browser."""
        if self._initialized:
            return
        
        try:
            from playwright.async_api import async_playwright
            
            self._playwright = await async_playwright().start()
            
            # Launch browser
            launch_options = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            }
            
            if self.browser_type == BrowserType.CHROME:
                self._browser = await self._playwright.chromium.launch(
                    channel="chrome",
                    headless=self.headless,
                    args=launch_options["args"],
                )
            elif self.browser_type == BrowserType.EDGE:
                self._browser = await self._playwright.chromium.launch(
                    channel="msedge",
                    headless=self.headless,
                    args=launch_options["args"],
                )
            elif self.browser_type == BrowserType.FIREFOX:
                self._browser = await self._playwright.firefox.launch(
                    headless=self.headless,
                )
            elif self.browser_type == BrowserType.WEBKIT:
                self._browser = await self._playwright.webkit.launch(
                    headless=self.headless,
                )
            else:  # Default to Chromium
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless,
                    args=launch_options["args"],
                )
            
            # Create context
            context_options = {
                "viewport": self.viewport,
                "accept_downloads": True,
                "downloads_path": str(self.downloads_path),
            }
            
            if self.user_agent:
                context_options["user_agent"] = self.user_agent
            
            self._context = await self._browser.new_context(**context_options)
            
            # Create initial page
            self._page = await self._context.new_page()
            
            # Set up download handling
            self._page.on("download", self._on_download)
            
            self._initialized = True
            logger.info(f"Playwright browser initialized: {self.browser_type.value}")
            
        except ImportError:
            logger.error("Playwright not installed. Run: pip install playwright && playwright install")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            raise
    
    async def _on_download(self, download) -> None:
        """Handle download events."""
        try:
            path = self.downloads_path / download.suggested_filename
            await download.save_as(path)
            logger.info(f"Downloaded file to: {path}")
        except Exception as e:
            logger.error(f"Download failed: {e}")
    
    async def navigate(
        self,
        url: str,
        wait_until: str = "networkidle",
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Navigate to a URL."""
        if not self._initialized:
            await self.initialize()
        
        try:
            response = await self._page.goto(url, wait_until=wait_until, timeout=timeout * 1000)
            
            return {
                "success": True,
                "url": self._page.url,
                "title": await self._page.title(),
                "status": response.status if response else 0,
            }
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def find_element(
        self,
        selector: str,
        method: str = "css",
        timeout: float = 10.0,
    ) -> Optional[BrowserElement]:
        """Find an element using CSS or XPath selector."""
        try:
            if not self._page:
                return None
            
            if method == "xpath":
                locator = self._page.locator(f"xpath={selector}")
            else:
                locator = self._page.locator(selector)
            
            # Wait for element
            await locator.wait_for(state="visible", timeout=10000)
            
            # Get element info
            element_id = str(uuid.uuid4())[:8]
            bounding_box = await locator.bounding_box()
            text = await locator.text_content()
            tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
            
            # Get attributes
            attributes = await locator.evaluate("""el => {
                const attrs: Record<string, string> = {};
                for (const attr of el.attributes) {
                    attrs[attr.name] = attr.value;
                }
                return attrs;
            }""")
            
            is_visible = await locator.is_visible()
            is_enabled = await locator.is_enabled()
            
            # Determine element type
            tag_type = self._classify_element(tag_name, attributes)
            
            return BrowserElement(
                element_id=str(uuid.uuid4())[:8],
                selector=selector,
                tag_name=tag_name,
                text=text or "",
                attributes=attributes,
                bounding_box=bounding_box,
                is_visible=is_visible,
                is_enabled=is_enabled,
                tag_type=tag_type,
                confidence=1.0,
                found_by="dom",
            )
            
        except Exception as e:
            logger.debug(f"Element not found: {selector} - {e}")
            return None
    
    async def find_element_by_text(
        self,
        text: str,
        element_type: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Optional[BrowserElement]:
        """Find element by visible text."""
        try:
            # Use text-based locator
            if element_type:
                locator = self._page.locator(f"{element_type}:has-text('{text}')")
            else:
                locator = self._page.locator(f":has-text('{text}')")
            
            await locator.first.wait_for(state="visible", timeout=10000)
            
            return await self.find_element(locator.first)
            
        except Exception as e:
            logger.debug(f"Element by text not found: {text} - {e}")
            return None
    
    async def find_element_by_role(
        self,
        role: str,
        name: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Optional[BrowserElement]:
        """Find element by ARIA role (accessibility)."""
        try:
            if name:
                locator = self._page.get_by_role(role, name=name)
            else:
                locator = self._page.get_by_role(role)
            
            await locator.first.wait_for(state="visible", timeout=10000)
            
            # Get element info
            bounding_box = await locator.first.bounding_box()
            text = await locator.first.text_content()
            tag_name = await locator.first.evaluate("el => el.tagName.toLowerCase()")
            
            attributes = await locator.first.evaluate("""el => {
                const attrs: Record<string, string> = {};
                for (const attr of el.attributes) {
                    attrs[attr.name] = attr.value;
                }
                return attrs;
            }""")
            
            return BrowserElement(
                element_id=str(uuid.uuid4())[:8],
                selector=f"role={role}",
                tag_name=tag_name,
                text=text or "",
                attributes=attributes,
                bounding_box=bounding_box,
                is_visible=True,
                is_enabled=True,
                tag_type=self._classify_element(tag_name, attributes),
                confidence=0.9,
                found_by="accessibility",
            )
            
        except Exception as e:
            logger.debug(f"Element by role not found: {role} - {e}")
            return None
    
    async def click(
        self,
        selector: Optional[str] = None,
        element: Optional[BrowserElement] = None,
        button: str = "left",
        click_count: int = 1,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Click an element."""
        try:
            if element:
                locator = self._page.locator(element.selector)
            elif selector:
                locator = self._page.locator(selector)
            else:
                return {"success": False, "error": "No selector or element provided"}
            
            await locator.click(button=button, click_count=click_count, timeout=timeout * 1000)
            
            return {"success": True, "clicked": True}
            
        except Exception as e:
            logger.error(f"Click failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def double_click(
        self,
        selector: Optional[str] = None,
        element: Optional[BrowserElement] = None,
    ) -> Dict[str, Any]:
        return await self.click(selector, element, click_count=2)
    
    async def right_click(
        self,
        selector: Optional[str] = None,
        element: Optional[BrowserElement] = None,
    ) -> Dict[str, Any]:
        return await self.click(selector, element, button="right")
    
    async def type_text(
        self,
        selector: Optional[str] = None,
        element: Optional[BrowserElement] = None,
        text: str = "",
        delay: float = 0.05,
    ) -> Dict[str, Any]:
        """Type text into an element."""
        try:
            if element:
                locator = self._page.locator(element.selector)
            elif selector:
                locator = self._page.locator(selector)
            else:
                return {"success": False, "error": "No selector or element provided"}
            
            await locator.fill("")  # Clear first
            await locator.type(text, delay=delay * 1000)
            
            return {"success": True, "typed": text}
            
        except Exception as e:
            logger.error(f"Type failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def press_key(
        self,
        key: str,
        selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Press a keyboard key."""
        try:
            if selector:
                await self._page.locator(selector).press(key)
            else:
                await self._page.keyboard.press(key)
            
            return {"success": True, "key": key}
            
        except Exception as e:
            logger.error(f"Key press failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def select_option(
        self,
        selector: str,
        value: Optional[str] = None,
        label: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Select an option from a dropdown."""
        try:
            locator = self._page.locator(selector)
            
            if value:
                await locator.select_option(value=value)
            elif label:
                await locator.select_option(label=label)
            elif index is not None:
                await locator.select_option(index=index)
            else:
                return {"success": False, "error": "No value, label, or index provided"}
            
            return {"success": True}
            
        except Exception as e:
            logger.error(f"Select option failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def scroll(
        self,
        direction: str = "down",
        amount: int = 3,
        selector: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Scroll the page or an element."""
        try:
            if selector:
                locator = self._page.locator(selector)
                await locator.scroll(direction=direction, amount=amount)
            else:
                # Scroll page
                if direction == "down":
                    await self._page.mouse.wheel(0, amount * 100)
                elif direction == "up":
                    await self._page.mouse.wheel(0, -amount * 100)
                elif direction == "left":
                    await self._page.mouse.wheel(-amount * 100, 0)
                elif direction == "right":
                    await self._page.mouse.wheel(amount * 100, 0)
            
            return {"success": True}
            
        except Exception as e:
            logger.error(f"Scroll failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def wait_for(
        self,
        condition: str,
        selector: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Wait for a condition."""
        try:
            if condition == "load":
                await self._page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            elif condition == "dom":
                await self._page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
            elif condition == "visible" and selector:
                await self._page.locator(selector).wait_for(state="visible", timeout=timeout * 1000)
            elif condition == "hidden" and selector:
                await self._page.locator(selector).wait_for(state="hidden", timeout=timeout * 1000)
            elif condition == "networkidle":
                await self._page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            else:
                return {"success": False, "error": f"Unknown condition: {condition}"}
            
            return {"success": True}
            
        except Exception as e:
            logger.error(f"Wait failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_page_content(
        self,
        format: str = "html",
    ) -> Dict[str, Any]:
        """Get page content."""
        try:
            if format == "html":
                content = await self._page.content()
            elif format == "text":
                content = await self._page.evaluate("document.body.innerText")
            elif format == "markdown":
                # Convert HTML to markdown
                html = await self._page.content()
                # Simple conversion
                content = html.replace("<br>", "\n").replace("</p>", "\n").replace("</div>", "\n")
                import re
                content = re.sub(r"<[^>]+>", "", content)
            else:
                return {"success": False, "error": f"Unknown format: {format}"}
            
            return {"success": True, "content": content}
            
        except Exception as e:
            logger.error(f"Get content failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def take_screenshot(
        self,
        path: Optional[str] = None,
        full_page: bool = True,
    ) -> Dict[str, Any]:
        """Take a screenshot."""
        try:
            if not path:
                path = f"screenshot_{int(time.time())}.png"
            
            await self._page.screenshot(path=path, full_page=full_page)
            
            # Also return base64
            screenshot_bytes = await self._page.screenshot(full_page=full_page)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            
            return {
                "success": True,
                "path": path,
                "base64": screenshot_b64,
            }
            
        except Exception as e:
            logger.error(f"Screenshot failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_tabs(self) -> List[BrowserTab]:
        """Get all open tabs."""
        tabs = []
        for i, page in enumerate(self._context.pages):
            tabs.append(BrowserTab(
                tab_id=f"tab-{i}",
                url=page.url,
                title=await page.title(),
                is_active=(page == self._page),
            ))
        return tabs
    
    async def switch_tab(self, index: int) -> Dict[str, Any]:
        """Switch to a tab by index."""
        try:
            pages = self._context.pages
            if 0 <= index < len(pages):
                self._page = pages[index]
                await self._page.bring_to_front()
                return {"success": True, "url": self._page.url, "title": await self._page.title()}
            return {"success": False, "error": "Invalid tab index"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def new_tab(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Open a new tab."""
        try:
            page = await self._context.new_page()
            if url:
                await page.goto(url)
            self._page = page
            return {"success": True, "url": page.url, "title": await page.title()}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def close_tab(self, index: Optional[int] = None) -> Dict[str, Any]:
        """Close a tab."""
        try:
            pages = self._context.pages
            if index is None:
                page = self._page
            elif 0 <= index < len(pages):
                page = pages[index]
            else:
                return {"success": False, "error": "Invalid tab index"}
            
            if len(pages) > 1:
                await page.close()
                if page == self._page:
                    self._page = pages[0] if pages[0] != page else pages[1] if len(pages) > 1 else None
                return {"success": True}
            else:
                return {"success": False, "error": "Cannot close last tab"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def execute_javascript(self, script: str) -> Dict[str, Any]:
        """Execute JavaScript in the page context."""
        try:
            result = await self._page.evaluate(script)
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"JS execution failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def close(self) -> None:
        """Close browser."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._initialized = False
    
    # Helper methods
    def _classify_element(self, tag_name: str, attributes: Dict[str, str]) -> str:
        tag = tag_name.lower()
        role = attributes.get("role", "").lower()
        type_attr = attributes.get("type", "").lower()
        
        if tag == "button" or role == "button":
            return "button"
        elif tag == "a" or role == "link":
            return "link"
        elif tag == "input":
            if type_attr in ["text", "email", "password", "search", "tel", "url"]:
                return "input"
            elif type_attr in ["checkbox"]:
                return "checkbox"
            elif type_attr in ["radio"]:
                return "radio"
            elif type_attr in ["submit", "button"]:
                return "button"
            else:
                return "input"
        elif tag == "textarea":
            return "textarea"
        elif tag == "select":
            return "dropdown"
        elif tag == "button" or role == "button":
            return "button"
        elif tag in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            return "header"
        elif tag == "img":
            return "image"
        elif role == "menu":
            return "menu"
        elif role == "menuitem":
            return "menuitem"
        elif role == "tab":
            return "tab"
        elif role == "checkbox":
            return "checkbox"
        elif role == "radio":
            return "radio"
        elif role == "slider":
            return "slider"
        elif role == "progressbar":
            return "progressbar"
        elif role == "status":
            return "status"
        else:
            return "unknown"


class BrowserAutomation:
    """High-level browser automation with vision fallback."""
    
    def __init__(
        self,
        browser_type: BrowserType = BrowserType.CHROMIUM,
        headless: bool = False,
        use_vision_fallback: bool = True,
    ):
        self.browser_type = browser_type
        self.headless = headless
        self.use_vision_fallback = use_vision_fallback
        self._browser: Optional[PlaywrightBrowser] = None
        self._vision_provider = None
    
    async def initialize(self) -> None:
        """Initialize browser and vision provider."""
        self._browser = PlaywrightBrowser(
            browser_type=self.browser_type,
            headless=self.headless,
        )
        await self._browser.initialize()
        
        # Initialize vision provider for fallback
        try:
            from m_core.model_providers import provider_factory
            self._vision_provider = provider_factory.get_vision_provider()
            if self._vision_provider:
                await self._vision_provider.initialize()
        except Exception as e:
            logger.warning(f"Vision provider not available: {e}")
    
    async def navigate_and_explore(
        self,
        url: str,
        task: str = "",
    ) -> Dict[str, Any]:
        """Navigate to URL and analyze the page."""
        # Navigate
        nav_result = await self._browser.navigate(url)
        if not nav_result.get("success"):
            return nav_result
        
        # Wait for page to load
        await self._browser.wait_for("networkidle")
        
        # Get page content
        content_result = await self._browser.get_page_content("text")
        content = content_result.get("content", "") if content_result.get("success") else ""
        
        # Take screenshot for vision analysis
        screenshot_result = await self._browser.take_screenshot(full_page=True)
        screenshot_b64 = screenshot_result.get("base64", "") if screenshot_result.get("success") else ""
        
        # Analyze with vision if available
        vision_analysis = {}
        if self.use_vision_fallback and self._vision_provider:
            try:
                vision_analysis = await self._analyze_with_vision(screenshot_b64, "")
            except Exception as e:
                logger.warning(f"Vision analysis failed: {e}")
        
        return {
            "success": True,
            "url": self._browser._page.url if self._browser._page else "",
            "title": await self._browser._page.title() if self._browser._page else "",
            "content_preview": content[:2000] if content else "",
            "vision_analysis": vision_analysis,
        }
    
    async def _analyze_with_vision(self, screenshot_b64: str, task: str) -> Dict[str, Any]:
        """Analyze screenshot with vision provider."""
        if not hasattr(self, '_vision_provider') or not self._vision_provider:
            return {}
        
        try:
            return await self._vision_provider.analyze_screen(screenshot_b64, "")
        except Exception as e:
            logger.warning(f"Vision analysis failed: {e}")
            return {}
    
    async def find_and_click(
        self,
        description: str,
        by: str = "auto",
    ) -> Dict[str, Any]:
        """Find and click an element by description."""
        # Try DOM first
        if by in ["auto", "dom"]:
            # Try by role first (accessibility)
            result = await self._try_click_by_role(description)
            if result.get("success"):
                return result
            
            # Try by text
            result = await self._try_click_by_text(description)
            if result.get("success"):
                return result
            
            # Try by CSS selector patterns
            result = await self._try_click_by_selector_patterns(description)
            if result.get("success"):
                return result
        
        # Fallback to vision grounding
        if self.use_vision_fallback:
            return await self._click_by_vision(description)
        
        return {"success": False, "error": f"Could not find element: {description}"}
    
    async def _try_click_by_role(self, description: str) -> Dict[str, Any]:
        """Try to find and click by ARIA role."""
        role_mapping = {
            "button": "button",
            "link": "link",
            "textbox": "textbox",
            "searchbox": "searchbox",
            "combobox": "combobox",
            "menuitem": "menuitem",
            "tab": "tab",
            "checkbox": "checkbox",
            "radio": "radio",
            "slider": "slider",
            "menu": "menu",
            "tabpanel": "tabpanel",
        }
        
        for keyword, role in role_mapping.items():
            if keyword in description.lower():
                element = await self._browser.find_element_by_role(role)
                if element:
                    result = await self._browser.click(element=element)
                    if result.get("success"):
                        return {"success": True, "method": "role", "role": role}
        
        return {"success": False}
    
    async def _try_click_by_text(self, description: str) -> Dict[str, Any]:
        """Try to find and click by visible text."""
        # Extract key words from description
        words = description.lower().split()
        key_words = [w for w in words if len(w) > 3 and w not in ["the", "and", "click", "press", "button", "link"]]
        
        for word in key_words:
            element = await self._browser.find_element_by_text(word)
            if element:
                result = await self._browser.click(element=element)
                if result.get("success"):
                    return {"success": True, "method": "text", "text": word}
        
        return {"success": False}
    
    async def _try_click_by_selector_patterns(self, description: str) -> Dict[str, Any]:
        """Try common selector patterns."""
        patterns = [
            ("button", "button"),
            ("a", "link"),
            ("input[type='submit']", "submit button"),
            ("input[type='button']", "button"),
            ("[role='button']", "button"),
            (".btn", "button"),
            (".button", "button"),
        ]
        
        for selector, desc in patterns:
            element = await self._browser.find_element(selector)
            if element:
                result = await self._browser.click(element=element)
                if result.get("success"):
                    return {"success": True, "method": "selector", "selector": selector}
        
        return {"success": False}
    
    async def _click_by_vision(self, description: str) -> Dict[str, Any]:
        """Use vision grounding to find and click element."""
        # Take screenshot
        screenshot_result = await self._browser.take_screenshot(full_page=True)
        if not screenshot_result.get("success"):
            return {"success": False, "error": "Failed to take screenshot"}
        
        screenshot_b64 = screenshot_result.get("base64", "")
        
        # Use vision to find element
        if hasattr(self, '_vision_provider') and self._vision_provider:
            try:
                elements = await self._vision_provider.find_elements(screenshot_b64, description)
                if elements:
                    # Click the first matching element
                    elem = elements[0]
                    bbox = elem.get("bbox", {})
                    if bbox:
                        x = bbox.get("left", 0) + bbox.get("width", 0) // 2
                        y = bbox.get("top", 0) + bbox.get("height", 0) // 2
                        
                        # Click at coordinates
                        from m_core.tools import tool_registry
                        from m_core.common_schemas import ToolExecutionContext
                        from m_core.security import consent_manager, ConsentScope
                        
                        tool = tool_registry.get_tool('mouse_control')
                        ctx = ToolExecutionContext(
                            tool_name='mouse_control',
                            arguments={"action": "click", "x": x, "y": y, "approved": True},
                            correlation_id='vision-click',
                            user_id='system',
                            permissions_granted=['automation:input'],
                            consent_manager=consent_manager
                        )
                        tool = tool_registry.get_tool('mouse_control')
                        result = await tool.execute(ctx)
                        
                        return {"success": True, "method": "vision", "element": elem}
            except Exception as e:
                logger.warning(f"Vision click failed: {e}")
        
        return {"success": False, "error": "Vision click failed"}
    
    async def type_text(
        self,
        description: str,
        text: str,
    ) -> Dict[str, Any]:
        """Type text into an element described by description."""
        # Try to find input element
        element = await self._browser.find_element_by_role("textbox")
        if not element:
            element = await self._browser.find_element_by_role("searchbox")
        if not element:
            # Try by text nearby
            element = await self._browser.find_element_by_text(description)
        
        if element:
            return await self._browser.type_text(element=element, text=text)
        
        return {"success": False, "error": "Could not find input element"}
    
    async def fill_form(
        self,
        fields: Dict[str, str],
        submit: bool = False,
    ) -> Dict[str, Any]:
        """Fill multiple form fields."""
        results = {}
        
        for field_name, value in fields.items():
            result = await self.type_text(field_name, value)
            results[field_name] = result
            
            if not result.get("success"):
                return {"success": False, "results": results, "error": f"Failed to fill {field_name}"}
        
        if submit:
            # Try to find and click submit button
            await self.find_and_click("submit button")
        
        return {"success": True, "results": results}
    
    async def extract_data(
        self,
        selectors: Dict[str, str],
    ) -> Dict[str, Any]:
        """Extract data from page using CSS selectors."""
        data = {}
        
        for name, selector in selectors.items():
            element = await self._browser.find_element(selector)
            if element:
                data[name] = element.text
            else:
                data[name] = None
        
        return {"success": True, "data": data}
    
    async def take_screenshot(self, full_page: bool = True) -> Dict[str, Any]:
        """Take a screenshot."""
        return await self._browser.take_screenshot(full_page=full_page)
    
    async def close(self) -> None:
        """Close browser."""
        if self._browser:
            await self._browser.close()


# Convenience functions
async def create_browser_automation(
    browser_type: BrowserType = BrowserType.CHROMIUM,
    headless: bool = False,
    use_vision_fallback: bool = True,
) -> BrowserAutomation:
    """Create and initialize browser automation."""
    auto = BrowserAutomation(
        browser_type=browser_type,
        headless=headless,
        use_vision_fallback=use_vision_fallback,
    )
    await auto.initialize()
    return auto


# Export
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