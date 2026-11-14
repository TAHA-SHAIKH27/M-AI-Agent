"""Visual automation tools for M AI Assistant - Screen capture, OCR, UI interaction."""
import asyncio
import base64
import io
import os
import structlog
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from m_core.common_schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


# Check for optional visual automation dependencies
try:
    import mss
    import mss.tools
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

try:
    import pywinauto
    from pywinauto import Desktop, Application
    from pywinauto.findwindows import find_windows
    from pywinauto.controls.uiawrapper import UIAWrapper
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False

try:
    import win32gui
    import win32con
    import win32api
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False


_DPI_INITIALIZED = False


def _ensure_dpi_aware() -> None:
    """Enable per-monitor DPI awareness once on Windows so capture & cursor coordinates match."""
    global _DPI_INITIALIZED
    if _DPI_INITIALIZED:
        return
    import sys
    if sys.platform != "win32":
        _DPI_INITIALIZED = True
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                user32.SetProcessDPIAware()
    except Exception:
        pass
    _DPI_INITIALIZED = True


def _ensure_default_desktop() -> None:
    """Ensure the calling thread is attached to the interactive Windows default desktop."""
    import sys
    if sys.platform != "win32":
        return
    _ensure_dpi_aware()
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass


@dataclass
class ScreenRegion:
    """Screen region coordinates."""
    left: int
    top: int
    width: int
    height: int
    
    @property
    def right(self) -> int:
        return self.left + self.width
    
    @property
    def bottom(self) -> int:
        return self.top + self.height
    
    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class VisualElement:
    """A UI element found visually."""
    element_id: str
    element_type: str  # button, text, input, image, etc.
    text: str
    bounding_box: ScreenRegion
    confidence: float
    window_title: str = ""
    control_type: str = ""


class ScreenCaptureTool(BaseTool):
    """Capture screen, window, or region as image."""
    
    manifest = {
        "name": "screen_capture",
        "display_name": "Screen Capture",
        "description": "Capture screenshot of full screen, specific window, or custom region.",
        "category": "visual",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["full", "window", "region", "monitor"],
                    "default": "full",
                    "description": "Capture mode"
                },
                "window_title": {
                    "type": "string",
                    "description": "Window title to capture (partial match, for window mode)"
                },
                "window_handle": {
                    "type": "integer",
                    "description": "Window handle (HWND) to capture"
                },
                "region": {
                    "type": "object",
                    "description": "Custom region to capture",
                    "properties": {
                        "left": {"type": "integer"},
                        "top": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["left", "top", "width", "height"],
                },
                "monitor_index": {
                    "type": "integer",
                    "default": 0,
                    "description": "Monitor index for multi-monitor setups"
                },
                "output_format": {
                    "type": "string",
                    "enum": ["png", "jpeg", "bmp"],
                    "default": "png",
                    "description": "Output image format"
                },
                "save_to_file": {
                    "type": "boolean",
                    "default": True,
                    "description": "Save to file (vs returning base64)"
                },
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string"},
                "image_base64": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "origin_x": {"type": "integer"},
                "origin_y": {"type": "integer"},
                "mode": {"type": "string"},
                "format": {"type": "string"},
            },
        },
        "required_permissions": ["screen:capture"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Captures screen content. May capture sensitive information.",
        "requires_approval": False,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "screen", "capture", "screenshot"],
    }
    
    def _get_windows(self) -> List[Dict[str, Any]]:
        """Get all visible windows."""
        _ensure_default_desktop()
        windows = []
        if not WIN32_AVAILABLE:
            return windows
        
        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    title = win32gui.GetWindowText(hwnd)
                    if not title:
                        return True
                    rect = win32gui.GetWindowRect(hwnd)
                    windows.append({
                        "hwnd": hwnd,
                        "title": title,
                        "rect": rect,
                        "width": rect[2] - rect[0],
                        "height": rect[3] - rect[1],
                    })
                except Exception:
                    pass
            return True
        
        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            logger.warning(f"Failed to enumerate windows: {e}")
        
        return windows
    
    def _find_window(self, title: str = None, hwnd: int = None) -> Optional[int]:
        """Find window handle matching criteria."""
        if hwnd:
            return hwnd
        
        windows = self._get_windows()
        for w in windows:
            if title and title.lower() in w["title"].lower():
                return w["hwnd"]
        return None
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not MSS_AVAILABLE:
            return {"success": False, "error": "mss not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        mode = args.get("mode", "full")
        window_title = args.get("window_title")
        window_handle = args.get("window_handle")
        region = args.get("region")
        monitor_index = args.get("monitor_index", 0)
        output_format = args.get("output_format", "png")
        save_to_file = args.get("save_to_file", True)
        
        try:
            with mss.mss() as sct:
                # Determine capture region
                if mode == "window":
                    hwnd = self._find_window(window_title, window_handle)
                    if not hwnd:
                        return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
                    
                    if WIN32_AVAILABLE:
                        rect = win32gui.GetWindowRect(hwnd)
                        monitor = {"left": rect[0], "top": rect[1], 
                                  "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
                    else:
                        return {"success": False, "error": "win32 not available for window capture", "error_code": "DEPENDENCY_MISSING"}
                
                elif mode == "region":
                    if not region:
                        return {"success": False, "error": "region required for region mode", "error_code": "INVALID_INPUT"}
                    monitor = {
                        "left": region["left"],
                        "top": region["top"],
                        "width": region["width"],
                        "height": region["height"],
                    }
                
                elif mode == "monitor":
                    monitors = sct.monitors
                    if monitor_index >= len(monitors) - 1:
                        return {"success": False, "error": f"Monitor index {monitor_index} not found", "error_code": "INVALID_INPUT"}
                    monitor = monitors[monitor_index + 1]  # Index 0 is all monitors
                
                else:  # full screen
                    monitor = sct.monitors[0]  # All monitors combined
                
                # Capture screenshot
                screenshot = sct.grab(monitor)
                
                # Convert to PIL Image for processing
                from PIL import Image
                img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                
                width, height = img.size
                
                if save_to_file:
                    # Save to temporary file
                    with tempfile.NamedTemporaryFile(suffix=f".{output_format}", delete=False) as tmp:
                        output_path = tmp.name
                    
                    if output_format == "jpeg":
                        img.save(output_path, "JPEG", quality=90)
                    elif output_format == "bmp":
                        img.save(output_path, "BMP")
                    else:
                        img.save(output_path, "PNG")
                    
                    return {
                        "success": True,
                        "result": {
                            "image_path": output_path,
                            "width": width,
                            "height": height,
                            "origin_x": int(monitor.get("left", 0)),
                            "origin_y": int(monitor.get("top", 0)),
                            "mode": mode,
                            "format": output_format,
                        },
                        "error": None,
                    }
                else:
                    # Return base64
                    buffer = io.BytesIO()
                    if output_format == "jpeg":
                        img.save(buffer, "JPEG", quality=90)
                    elif output_format == "bmp":
                        img.save(buffer, "BMP")
                    else:
                        img.save(buffer, "PNG")
                    
                    img_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
                    
                    return {
                        "success": True,
                        "result": {
                            "image_base64": img_base64,
                            "width": width,
                            "height": height,
                            "origin_x": int(monitor.get("left", 0)),
                            "origin_y": int(monitor.get("top", 0)),
                            "mode": mode,
                            "format": output_format,
                        },
                        "error": None,
                    }
        
        except Exception as e:
            logger.error(f"Screen capture failed: {e}")
            return {"success": False, "error": f"Capture failed: {str(e)}", "error_code": "CAPTURE_FAILED"}


class ScreenOCRTool(BaseTool):
    """Extract text from screen capture or image file using OCR."""
    
    manifest = {
        "name": "screen_ocr",
        "display_name": "Screen OCR",
        "description": "Extract text from screen capture, window, or image file using Tesseract OCR.",
        "category": "visual",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["screen", "window", "region", "file"],
                    "default": "screen",
                    "description": "Source of image for OCR"
                },
                "image_file": {
                    "type": "string",
                    "description": "Path to image file (for file source)"
                },
                "window_title": {
                    "type": "string",
                    "description": "Window title (for window source)"
                },
                "region": {
                    "type": "object",
                    "description": "Region to capture",
                    "properties": {
                        "left": {"type": "integer"},
                        "top": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["left", "top", "width", "height"],
                },
                "language": {
                    "type": "string",
                    "default": "eng",
                    "description": "OCR language (eng, eng+fra, etc.)"
                },
                "preprocess": {
                    "type": "boolean",
                    "default": True,
                    "description": "Apply image preprocessing for better OCR"
                },
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "confidence": {"type": "number"},
                "words": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                            "bbox": {"type": "object"},
                        },
                    },
                },
            },
        },
        "required_permissions": ["screen:ocr"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Extracts text from screen. May read sensitive information.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "ocr", "text", "extract"],
    }
    
    def _preprocess_image(self, image: "np.ndarray") -> "np.ndarray":
        """Preprocess image for better OCR."""
        if not OPENCV_AVAILABLE:
            return image
        
        # Convert to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        
        # Apply threshold
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Denoise
        denoised = cv2.fastNlMeansDenoising(thresh, h=10)
        
        # Dilate to connect text
        kernel = np.ones((1, 1), np.uint8)
        dilated = cv2.dilate(denoised, kernel, iterations=1)
        
        return dilated
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not PYTESSERACT_AVAILABLE:
            return {"success": False, "error": "pytesseract not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        source = args.get("source", "screen")
        image_file = args.get("image_file")
        window_title = args.get("window_title")
        region = args.get("region")
        language = args.get("language", "eng")
        preprocess = args.get("preprocess", True)
        
        try:
            # Get image based on source
            if source == "file":
                if not image_file or not Path(image_file).exists():
                    return {"success": False, "error": "Image file not found", "error_code": "NOT_FOUND"}
                from PIL import Image
                pil_image = Image.open(image_file)
                cv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            elif source == "window":
                if not WIN32_AVAILABLE:
                    return {"success": False, "error": "win32 not available", "error_code": "DEPENDENCY_MISSING"}
                
                hwnd = None
                if window_title:
                    windows = self._get_windows()
                    for w in windows:
                        if window_title.lower() in w["title"].lower():
                            hwnd = w["hwnd"]
                            break
                
                if not hwnd:
                    return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
                
                try:
                    with mss.mss() as sct:
                        rect = win32gui.GetWindowRect(hwnd)
                        monitor = {"left": rect[0], "top": rect[1], 
                                  "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
                        screenshot = sct.grab(monitor)
                        from PIL import Image
                        pil_image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                        cv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
                except Exception as e:
                    return {"success": False, "error": f"Failed to capture window: {e}", "error_code": "CAPTURE_FAILED"}
            
            elif source == "region":
                if not region:
                    return {"success": False, "error": "region required", "error_code": "INVALID_INPUT"}
                
                with mss.mss() as sct:
                    monitor = {
                        "left": region["left"],
                        "top": region["top"],
                        "width": region["width"],
                        "height": region["height"],
                    }
                    screenshot = sct.grab(monitor)
                    from PIL import Image
                    pil_image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                    cv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            else:  # screen
                with mss.mss() as sct:
                    monitor = sct.monitors[0]
                    screenshot = sct.grab(monitor)
                    from PIL import Image
                    pil_image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                    cv_image = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            # Preprocess if requested
            if preprocess and OPENCV_AVAILABLE:
                cv_image = self._preprocess_image(cv_image)
            
            # Run OCR
            loop = asyncio.get_event_loop()
            
            # Get text with confidence
            ocr_data = await loop.run_in_executor(
                None,
                lambda: pytesseract.image_to_data(
                    cv_image,
                    lang=language,
                    output_type=pytesseract.Output.DICT
                )
            )
            
            # Extract text and confidence
            text_parts = []
            word_data = []
            confidences = []
            
            for i in range(len(ocr_data['text'])):
                text = ocr_data['text'][i].strip()
                conf = int(ocr_data['conf'][i])
                
                if text and conf > 0:
                    text_parts.append(text)
                    confidences.append(conf)
                    word_data.append({
                        "text": text,
                        "confidence": conf / 100.0,
                        "bbox": {
                            "left": ocr_data['left'][i],
                            "top": ocr_data['top'][i],
                            "width": ocr_data['width'][i],
                            "height": ocr_data['height'][i],
                        }
                    })
            
            full_text = " ".join(text_parts)
            avg_confidence = sum(confidences) / len(confidences) / 100.0 if confidences else 0.0
            
            return {
                "success": True,
                "result": {
                    "text": full_text,
                    "confidence": avg_confidence,
                    "words": word_data,
                },
                "error": None,
            }
        
        except Exception as e:
            logger.error(f"OCR failed: {e}")
            return {"success": False, "error": f"OCR failed: {str(e)}", "error_code": "OCR_FAILED"}
    
    def _get_windows(self) -> List[Dict[str, Any]]:
        """Get all visible windows."""
        _ensure_default_desktop()
        windows = []
        if not WIN32_AVAILABLE:
            return windows
        
        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    title = win32gui.GetWindowText(hwnd)
                    if not title:
                        return True
                    windows.append({"hwnd": hwnd, "title": title})
                except Exception:
                    pass
            return True
        
        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            logger.warning(f"Failed to enumerate windows: {e}")
        
        return windows


class VisualFindTool(BaseTool):
    """Find UI elements on screen using template matching or text search."""
    
    manifest = {
        "name": "visual_find",
        "display_name": "Visual Find",
        "description": "Find UI elements on screen by template image, text, or visual properties.",
        "category": "visual",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["template", "text", "color", "shape"],
                    "default": "template",
                    "description": "Search method"
                },
                "template_image": {
                    "type": "string",
                    "description": "Path to template image (for template method)"
                },
                "search_text": {
                    "type": "string",
                    "description": "Text to search for (for text method)"
                },
                "color": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "RGB color to find (for color method)"
                },
                "threshold": {
                    "type": "number",
                    "default": 0.8,
                    "description": "Match threshold (0-1)"
                },
                "region": {
                    "type": "object",
                    "description": "Region to search in",
                    "properties": {
                        "left": {"type": "integer"},
                        "top": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["left", "top", "width", "height"],
                },
                "window_title": {
                    "type": "string",
                    "description": "Window title to search in (for text method)"
                },
                "return_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return all matches vs best match"
                },
            },
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        },
        "required_permissions": ["screen:find"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Searches screen for visual elements.",
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "find", "template", "match"],
    }
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not OPENCV_AVAILABLE or not MSS_AVAILABLE:
            return {"success": False, "error": "OpenCV or mss not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        method = args.get("method", "template")
        template_path = args.get("template_image")
        search_text = args.get("search_text")
        color = args.get("color")
        threshold = args.get("threshold", 0.8)
        region = args.get("region")
        window_title = args.get("window_title")
        return_all = args.get("return_all", False)
        
        try:
            # Capture screen region or window
            with mss.mss() as sct:
                if window_title:
                    # Find window and capture it
                    found_hwnd = None
                    if WIN32_AVAILABLE:
                        def enum_cb(hwnd, _):
                            nonlocal found_hwnd
                            try:
                                if win32gui.IsWindowVisible(hwnd):
                                    title = win32gui.GetWindowText(hwnd)
                                    if window_title.lower() in title.lower():
                                        found_hwnd = hwnd
                                        return False
                            except Exception:
                                pass
                            return True
                        try:
                            win32gui.EnumWindows(enum_cb, None)
                        except Exception as e:
                            return {"success": False, "error": f"Failed to enumerate windows: {e}", "error_code": "ENUM_FAILED"}
                    
                    if not found_hwnd:
                        return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
                    
                    hwnd = found_hwnd
                    try:
                        rect = win32gui.GetWindowRect(hwnd)
                        monitor = {"left": rect[0], "top": rect[1], 
                                  "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
                    except Exception as e:
                        return {"success": False, "error": f"Failed to get window rect: {e}", "error_code": "WINDOW_ERROR"}
                elif region:
                    monitor = {
                        "left": region["left"],
                        "top": region["top"],
                        "width": region["width"],
                        "height": region["height"],
                    }
                else:
                    monitor = sct.monitors[0]
                
                screenshot = sct.grab(monitor)
                from PIL import Image
                pil_image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                screen = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            matches = []
            
            if method == "template":
                if not template_path or not Path(template_path).exists():
                    return {"success": False, "error": "Template image not found", "error_code": "NOT_FOUND"}
                
                template = cv2.imread(template_path)
                if template is None:
                    return {"success": False, "error": "Failed to load template", "error_code": "INVALID_INPUT"}
                
                result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
                locations = np.where(result >= threshold)
                
                h, w = template.shape[:2]
                for pt in zip(*locations[::-1]):
                    matches.append({
                        "x": int(pt[0]),
                        "y": int(pt[1]),
                        "width": w,
                        "height": h,
                        "confidence": float(result[pt[1], pt[0]]),
                    })
            
            elif method == "text":
                if not search_text:
                    return {"success": False, "error": "search_text required for text method", "error_code": "INVALID_INPUT"}
                
                # Use OCR to find text
                if not PYTESSERACT_AVAILABLE:
                    return {"success": False, "error": "pytesseract not available", "error_code": "DEPENDENCY_MISSING"}
                
                ocr_data = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pytesseract.image_to_data(
                        screen,
                        output_type=pytesseract.Output.DICT
                    )
                )
                
                for i in range(len(ocr_data['text'])):
                    text = ocr_data['text'][i].strip()
                    try:
                        conf = float(ocr_data['conf'][i])
                    except (ValueError, TypeError):
                        conf = -1.0
                    
                    if text and conf > 20 and search_text.lower() in text.lower():
                        matches.append({
                            "x": ocr_data['left'][i],
                            "y": ocr_data['top'][i],
                            "width": ocr_data['width'][i],
                            "height": ocr_data['height'][i],
                            "confidence": max(0.0, min(1.0, conf / 100.0)),
                        })
            
            elif method == "color":
                if not color or len(color) != 3:
                    return {"success": False, "error": "color must be [R, G, B]", "error_code": "INVALID_INPUT"}
                
                # Convert input RGB color to BGR to match OpenCV screen representation
                target = np.array([int(color[2]), int(color[1]), int(color[0])], dtype=np.uint8)
                diff = np.abs(screen.astype(np.int16) - target.astype(np.int16))
                mask = np.all(diff <= 10, axis=2)  # Tolerance of 10
                
                # Find contours
                mask_uint8 = mask.astype(np.uint8) * 255
                contours, _ = cv2.findContours(mask_uint8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                
                for cnt in contours:
                    x, y, w, h = cv2.boundingRect(cnt)
                    if w > 5 and h > 5:  # Filter tiny noise
                        matches.append({
                            "x": x,
                            "y": y,
                            "width": w,
                            "height": h,
                            "confidence": 0.9,
                        })
            
            # Sort by confidence and offset to absolute virtual-screen coordinates
            origin_x = int(monitor.get("left", 0))
            origin_y = int(monitor.get("top", 0))
            for m in matches:
                m["x"] = int(m["x"]) + origin_x
                m["y"] = int(m["y"]) + origin_y
                m["center_x"] = m["x"] + int(m.get("width", 0)) // 2
                m["center_y"] = m["y"] + int(m.get("height", 0)) // 2

            matches.sort(key=lambda m: m["confidence"], reverse=True)
            
            if not return_all and matches:
                matches = [matches[0]]
            
            return {
                "success": True,
                "result": {"matches": matches},
                "error": None,
            }
        
        except Exception as e:
            logger.error(f"Visual find failed: {e}")
            return {"success": False, "error": f"Visual find failed: {str(e)}", "error_code": "FIND_FAILED"}


class MouseControlTool(BaseTool):
    """Control mouse: move, click, drag, scroll."""
    
    manifest = {
        "name": "mouse_control",
        "display_name": "Mouse Control",
        "description": "Control mouse: move, click, double-click, right-click, drag, scroll.",
        "category": "visual",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "click", "double_click", "right_click", "drag", "scroll", "position"],
                    "description": "Mouse action to perform"
                },
                "x": {"type": "integer", "description": "Target X coordinate"},
                "y": {"type": "integer", "description": "Target Y coordinate"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "default": "left",
                    "description": "Mouse button"
                },
                "clicks": {"type": "integer", "default": 1, "description": "Number of clicks"},
                "duration": {"type": "number", "default": 0.5, "description": "Move duration in seconds"},
                "scroll_amount": {"type": "integer", "default": 3, "description": "Scroll amount (positive up, negative down)"},
                "drag_to_x": {"type": "integer", "description": "Drag destination X"},
                "drag_to_y": {"type": "integer", "description": "Drag destination Y"},
                "approval_token": {"type": "string", "description": "Approval token for automated approval"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "position": {"type": "object"},
            },
        },
        "required_permissions": ["automation:input"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Controls mouse input. Requires approval for clicks and drags.",
        "requires_approval": True,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "mouse", "click", "drag", "automation"],
    }
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not WIN32_AVAILABLE:
            return {"success": False, "error": "win32 not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        action = args.get("action")
        
        # Consent is checked by BaseTool.execute() before calling _execute_impl
        # No internal approved flag check needed - consent system handles it
        
        try:
            if action == "position":
                x, y = win32gui.GetCursorPos()
                return {"success": True, "result": {"position": {"x": x, "y": y}}, "error": None}
            
            elif action == "move":
                x = args.get("x")
                y = args.get("y")
                duration = args.get("duration", 0.5)
                
                if x is None or y is None:
                    return {"success": False, "error": "x and y required for move", "error_code": "INVALID_INPUT"}
                
                # Smooth move
                start_x, start_y = win32gui.GetCursorPos()
                steps = max(1, int(duration * 60))
                for i in range(steps + 1):
                    t = i / steps
                    eased = t * t * (3 - 2 * t)  # Smooth step
                    cur_x = int(start_x + (x - start_x) * eased)
                    cur_y = int(start_y + (y - start_y) * eased)
                    win32api.SetCursorPos((cur_x, cur_y))
                    await asyncio.sleep(0.01)
                
                return {"success": True, "result": {"position": {"x": x, "y": y}}, "error": None}
            
            elif action in ["click", "double_click", "right_click"]:
                x = args.get("x")
                y = args.get("y")
                clicks = args.get("clicks", 2 if action == "double_click" else 1)
                button = args.get("button", "left")
                
                if x is not None and y is not None:
                    win32api.SetCursorPos((x, y))
                    await asyncio.sleep(0.05)
                
                # Map button to win32 constants
                button_map = {
                    "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
                    "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
                    "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
                }
                
                down, up = button_map.get(button, button_map["left"])
                
                for _ in range(clicks):
                    win32api.mouse_event(down, 0, 0, 0, 0)
                    await asyncio.sleep(0.02)
                    win32api.mouse_event(up, 0, 0, 0, 0)
                    if clicks > 1:
                        await asyncio.sleep(0.1)
                
                pos = win32gui.GetCursorPos()
                return {"success": True, "result": {"position": {"x": pos[0], "y": pos[1]}}, "error": None}
            
            elif action == "drag":
                x = args.get("x")
                y = args.get("y")
                drag_to_x = args.get("drag_to_x")
                drag_to_y = args.get("drag_to_y")
                duration = args.get("duration", 0.5)
                button = args.get("button", "left")
                
                if x is None or y is None or drag_to_x is None or drag_to_y is None:
                    return {"success": False, "error": "x, y, drag_to_x, drag_to_y required for drag", "error_code": "INVALID_INPUT"}
                
                # Move to start
                win32api.SetCursorPos((x, y))
                await asyncio.sleep(0.05)
                
                # Mouse down
                button_map = {
                    "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
                    "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
                }
                down, up = button_map.get(button, button_map["left"])
                
                win32api.mouse_event(down, 0, 0, 0, 0)
                await asyncio.sleep(0.05)
                
                # Drag to destination
                steps = max(1, int(duration * 60))
                for i in range(steps + 1):
                    t = i / steps
                    cur_x = int(x + (drag_to_x - x) * t)
                    cur_y = int(y + (drag_to_y - y) * t)
                    win32api.SetCursorPos((cur_x, cur_y))
                    await asyncio.sleep(0.01)
                
                # Mouse up
                win32api.mouse_event(up, 0, 0, 0, 0)
                
                pos = win32gui.GetCursorPos()
                return {"success": True, "result": {"position": {"x": pos[0], "y": pos[1]}}, "error": None}
            
            elif action == "scroll":
                amount = args.get("scroll_amount", 3)
                # WHEEL_DELTA = 120
                delta = amount * 120
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
                
                pos = win32gui.GetCursorPos()
                return {"success": True, "result": {"position": {"x": pos[0], "y": pos[1]}}, "error": None}
            
            return {"success": False, "error": f"Unknown action: {action}", "error_code": "UNKNOWN_ACTION"}
        
        except Exception as e:
            logger.error(f"Mouse control failed: {e}")
            return {"success": False, "error": f"Mouse control failed: {str(e)}", "error_code": "CONTROL_FAILED"}


class KeyboardInputTool(BaseTool):
    """Send keyboard input: type text, press keys, hotkeys."""
    
    manifest = {
        "name": "keyboard_input",
        "display_name": "Keyboard Input",
        "description": "Send keyboard input: type text, press keys, send hotkeys.",
        "category": "visual",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["type", "press", "hotkey", "hold", "release"],
                    "description": "Keyboard action"
                },
                "text": {"type": "string", "description": "Text to type (for type action)"},
                "key": {"type": "string", "description": "Key to press (e.g., 'enter', 'tab', 'escape', 'a')"},
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Keys for hotkey (e.g., ['ctrl', 'c'])"},
                "interval": {"type": "number", "default": 0.05, "description": "Interval between keystrokes"},
                "approval_token": {"type": "string", "description": "Approval token for automated approval"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
            },
        },
        "required_permissions": ["automation:input"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Sends keyboard input. Requires approval.",
        "requires_approval": True,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "keyboard", "type", "hotkey", "automation"],
    }
    
    # Virtual key codes
    VK_MAP = {
        "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "space": 0x20,
        "backspace": 0x08, "delete": 0x2E, "home": 0x24, "end": 0x23,
        "page_up": 0x21, "page_down": 0x22, "up": 0x26, "down": 0x28,
        "left": 0x25, "right": 0x27, "f1": 0x70, "f2": 0x71, "f3": 0x72,
        "f4": 0x73, "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
        "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
        "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
        "caps_lock": 0x14, "num_lock": 0x90, "scroll_lock": 0x91,
    }
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not WIN32_AVAILABLE:
            return {"success": False, "error": "win32 not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        action = args.get("action")
        
        # Consent is checked by BaseTool.execute() before calling _execute_impl
        # No internal approved flag check needed - consent system handles it
        
        try:
            def key_down(vk):
                win32api.keybd_event(vk, 0, 0, 0)
            
            def key_up(vk):
                win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
            
            async def press_key(vk):
                key_down(vk)
                await asyncio.sleep(0.02)
                key_up(vk)

            def resolve_vk(key_name: str) -> Optional[int]:
                k = (key_name or "").strip().lower()
                if not k:
                    return None
                if k in self.VK_MAP:
                    return self.VK_MAP[k]
                if len(k) == 1:
                    try:
                        import ctypes
                        vk_res = ctypes.windll.user32.VkKeyScanW(ord(key_name))
                        if vk_res != -1:
                            return vk_res & 0xFF
                    except Exception:
                        pass
                    return ord(k.upper())
                return None

            def send_unicode_char(ch: str) -> bool:
                """Send a Unicode character using Win32 SendInput for universal app compatibility."""
                try:
                    import ctypes
                    from ctypes import wintypes

                    KEYEVENTF_UNICODE = 0x0004
                    KEYEVENTF_KEYUP = 0x0002
                    INPUT_KEYBOARD = 1
                    ULONG_PTR = ctypes.POINTER(wintypes.ULONG)

                    class KEYBDINPUT(ctypes.Structure):
                        _fields_ = [
                            ("wVk", wintypes.WORD),
                            ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD),
                            ("time", wintypes.DWORD),
                            ("dwExtraInfo", ULONG_PTR),
                        ]

                    class _INPUTUNION(ctypes.Union):
                        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

                    class INPUT(ctypes.Structure):
                        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

                    user32 = ctypes.windll.user32
                    for code_unit in ch.encode("utf-16-le"):
                        pass
                    raw = ch.encode("utf-16-le")
                    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
                    for unit in units:
                        down = INPUT(type=INPUT_KEYBOARD)
                        down.union.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=None)
                        up = INPUT(type=INPUT_KEYBOARD)
                        up.union.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=None)
                        arr = (INPUT * 2)(down, up)
                        sent = user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(INPUT))
                        if sent != 2:
                            return False
                    return True
                except Exception:
                    return False

            if action == "type":
                text = args.get("text", "")
                interval = args.get("interval", 0.02)

                for char in text:
                    if char == "\r":
                        continue
                    if char == "\n":
                        await press_key(0x0D)
                    elif char == "\t":
                        await press_key(0x09)
                    elif send_unicode_char(char):
                        pass
                    else:
                        vk_full = -1
                        try:
                            import ctypes
                            vk_full = ctypes.windll.user32.VkKeyScanW(ord(char))
                        except Exception:
                            pass
                        if vk_full != -1:
                            vk = vk_full & 0xFF
                            shift_state = (vk_full >> 8) & 0xFF
                            if shift_state & 1:
                                key_down(0x10)
                            await press_key(vk)
                            if shift_state & 1:
                                key_up(0x10)
                        else:
                            vk = ord(char.upper())
                            await press_key(vk)

                    if interval > 0:
                        await asyncio.sleep(min(interval, 0.05))

                return {"success": True, "result": {}, "error": None}
            
            elif action == "press":
                key = args.get("key", "")
                vk = resolve_vk(key)
                if vk is None:
                    return {"success": False, "error": f"Unknown key: {key}", "error_code": "INVALID_INPUT"}
                
                await press_key(vk)
                return {"success": True, "result": {}, "error": None}
            
            elif action == "hotkey":
                keys = args.get("keys", [])
                if not keys:
                    return {"success": False, "error": "keys array required for hotkey", "error_code": "INVALID_INPUT"}
                
                vks = []
                for k in keys:
                    vk = resolve_vk(k)
                    if vk is None:
                        return {"success": False, "error": f"Unknown key: {k}", "error_code": "INVALID_INPUT"}
                    vks.append(vk)
                
                # Press all down
                for vk in vks:
                    key_down(vk)
                    await asyncio.sleep(0.01)
                
                # Release in reverse order
                for vk in reversed(vks):
                    key_up(vk)
                    await asyncio.sleep(0.01)
                
                return {"success": True, "result": {}, "error": None}
            
            elif action == "hold":
                key = args.get("key", "")
                vk = resolve_vk(key)
                if vk is None:
                    return {"success": False, "error": f"Unknown key: {key}", "error_code": "INVALID_INPUT"}
                key_down(vk)
                return {"success": True, "result": {}, "error": None}
            
            elif action == "release":
                key = args.get("key", "")
                vk = resolve_vk(key)
                if vk is None:
                    return {"success": False, "error": f"Unknown key: {key}", "error_code": "INVALID_INPUT"}
                key_up(vk)
                return {"success": True, "result": {}, "error": None}
            
            return {"success": False, "error": f"Unknown action: {action}", "error_code": "UNKNOWN_ACTION"}
        
        except Exception as e:
            logger.error(f"Keyboard input failed: {e}")
            return {"success": False, "error": f"Keyboard input failed: {str(e)}", "error_code": "CONTROL_FAILED"}


class WindowInteractTool(BaseTool):
    """Interact with windows: find controls, click buttons, fill inputs, select menus."""
    
    manifest = {
        "name": "window_interact",
        "display_name": "Window Interaction",
        "description": "Interact with window controls: click buttons, fill inputs, select dropdowns, navigate menus.",
        "category": "visual",
        "risk_level": "medium",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["find_control", "click", "type", "select", "get_text", "set_focus", "list_controls"],
                    "description": "Interaction action"
                },
                "window_title": {"type": "string", "description": "Target window title (partial match)"},
                "window_handle": {"type": "integer", "description": "Target window handle"},
                "control_identifier": {
                    "type": "object",
                    "description": "Control to interact with",
                    "properties": {
                        "type": {"type": "string", "description": "Control type (Button, Edit, ComboBox, etc.)"},
                        "title": {"type": "string", "description": "Control title/text"},
                        "auto_id": {"type": "string", "description": "Automation ID"},
                        "control_id": {"type": "integer", "description": "Control ID"},
                        "class_name": {"type": "string", "description": "Window class name"},
                        "index": {"type": "integer", "default": 0, "description": "Index if multiple matches"},
                    },
                },
                "text": {"type": "string", "description": "Text to type (for type action)"},
                "value": {"type": "string", "description": "Value to select (for select action)"},
                "approval_token": {"type": "string", "description": "Approval token for automated approval"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "controls": {"type": "array"},
                "text": {"type": "string"},
                "success": {"type": "boolean"},
            },
        },
        "required_permissions": ["automation:window"],
        "optional_permissions": [],
        "risk_level": "medium",
        "risk_description": "Interacts with window controls. Requires approval for modifications.",
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "window", "control", "ui", "automation"],
    }
    
    def _connect_window(self, title: str = None, handle: int = None):
        """Connect to a window."""
        if not PYWINAUTO_AVAILABLE:
            return None
        
        try:
            if handle:
                return Desktop(backend="uia").window(handle=handle)
            elif title:
                # First try UIA backend with exact/substring match
                windows = Desktop(backend="uia").windows()
                for w in windows:
                    wtitle = w.window_text()
                    if title.lower() in wtitle.lower():
                        return w
                
                # If not found, try win32gui to find the HWND and connect via UIA
                if WIN32_AVAILABLE:
                    def enum_cb(hwnd, results):
                        if win32gui.IsWindowVisible(hwnd):
                            wtitle = win32gui.GetWindowText(hwnd)
                            if title.lower() in wtitle.lower():
                                results.append(hwnd)
                        return True
                    
                    results = []
                    win32gui.EnumWindows(enum_cb, results)
                    for hwnd in results:
                        try:
                            return Desktop(backend="uia").window(handle=hwnd)
                        except Exception:
                            continue
                
                # Last resort: try partial match on any window
                windows = Desktop(backend="uia").windows()
                for w in windows:
                    wtitle = w.window_text()
                    # Try matching key words from title
                    title_words = [w for w in title.split() if len(w) > 2]
                    if title_words and all(word.lower() in wtitle.lower() for word in title_words):
                        return w
            return None
        except Exception as e:
            logger.error(f"Failed to connect to window: {e}")
            return None
    
    def _find_control(self, window, identifier: dict):
        """Find a control within a window."""
        try:
            # Try child_window first for specific lookups (supports auto_id)
            if identifier.get("auto_id") or identifier.get("automation_id"):
                auto_id = identifier.get("auto_id") or identifier.get("automation_id")
                ctrl_type = identifier.get("type", "Edit")
                try:
                    return window.child_window(auto_id=auto_id, control_type=ctrl_type)
                except Exception:
                    pass
            
            if identifier.get("title") and identifier.get("type"):
                try:
                    return window.child_window(title=identifier["title"], control_type=identifier["type"])
                except Exception:
                    pass
            
            if identifier.get("class_name") and identifier.get("type"):
                try:
                    return window.child_window(class_name=identifier["class_name"], control_type=identifier["type"])
                except Exception:
                    pass
            
            # Fall back to descendants for more general searches
            criteria = {}
            if identifier.get("type"):
                criteria["control_type"] = identifier["type"]
            if identifier.get("title"):
                criteria["title"] = identifier["title"]
            if identifier.get("class_name"):
                criteria["class_name"] = identifier["class_name"]
            if identifier.get("control_id"):
                criteria["control_id"] = identifier["control_id"]
            
            controls = window.descendants(**criteria)
            index = identifier.get("index", 0)
            if controls and index < len(controls):
                return controls[index]
            return None
        except Exception as e:
            logger.error(f"Failed to find control: {e}")
            return None
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not PYWINAUTO_AVAILABLE:
            return {"success": False, "error": "pywinauto not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        action = args.get("action")
        window_title = args.get("window_title")
        window_handle = args.get("window_handle")
        identifier = args.get("control_identifier", {})
        text = args.get("text", "")
        value = args.get("value", "")
        
        # Consent is checked by BaseTool.execute() before calling _execute_impl
        # No internal approved flag check needed - consent system handles it
        
        # Connect to window
        window = self._connect_window(window_title, window_handle)
        if not window:
            return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
        
        try:
            if action == "list_controls":
                controls = []
                for ctrl in window.descendants():
                    try:
                        controls.append({
                            "type": ctrl.element_info.control_type,
                            "title": ctrl.window_text(),
                            "auto_id": ctrl.element_info.automation_id,
                            "control_id": ctrl.element_info.control_id,
                            "class_name": ctrl.element_info.class_name,
                            "rect": {
                                "left": ctrl.rectangle().left,
                                "top": ctrl.rectangle().top,
                                "width": ctrl.rectangle().width(),
                                "height": ctrl.rectangle().height(),
                            },
                            "enabled": ctrl.is_enabled(),
                            "visible": ctrl.is_visible(),
                        })
                    except Exception:
                        pass
                
                return {"success": True, "result": {"controls": controls}, "error": None}
            
            # Find target control
            control = self._find_control(window, identifier)
            if not control:
                return {"success": False, "error": "Control not found", "error_code": "CONTROL_NOT_FOUND"}
            
            if action == "find_control":
                return {
                    "success": True,
                    "result": {
                        "control": {
                            "type": control.element_info.control_type,
                            "title": control.window_text(),
                            "auto_id": control.element_info.automation_id,
                            "control_id": control.element_info.control_id,
                            "class_name": control.element_info.class_name,
                            "rect": {
                                "left": control.rectangle().left,
                                "top": control.rectangle().top,
                                "width": control.rectangle().width(),
                                "height": control.rectangle().height(),
                            },
                        }
                    },
                    "error": None,
                }
            
            elif action == "click":
                control.click_input()
                await asyncio.sleep(0.1)
                return {"success": True, "result": {}, "error": None}
            
            elif action == "type":
                if not text:
                    return {"success": False, "error": "text required for type action", "error_code": "INVALID_INPUT"}
                ctrl_type = control.element_info.control_type
                ctrl_class = control.element_info.class_name
                if ctrl_type == "Document" and ctrl_class == "RichEditD2DPT":
                    try:
                        control.iface_value.SetValue(text)
                    except Exception:
                        control.click_input()
                        await asyncio.sleep(0.05)
                        control.type_keys(text, with_spaces=True, with_newlines=True)
                else:
                    try:
                        control.set_edit_text(text)
                    except Exception:
                        control.click_input()
                        await asyncio.sleep(0.05)
                        control.type_keys(text, with_spaces=True, with_newlines=True)
                await asyncio.sleep(0.1)
                return {"success": True, "result": {}, "error": None}
            
            elif action == "select":
                if not value:
                    return {"success": False, "error": "value required for select action", "error_code": "INVALID_INPUT"}
                # Try select by text
                try:
                    control.select(value)
                except Exception:
                    # Try typing
                    control.set_edit_text(value)
                await asyncio.sleep(0.1)
                return {"success": True, "result": {}, "error": None}
            
            elif action == "get_text":
                txt = control.window_text()
                return {"success": True, "result": {"text": txt}, "error": None}
            
            elif action == "set_focus":
                control.set_focus()
                await asyncio.sleep(0.05)
                return {"success": True, "result": {}, "error": None}
            
            return {"success": False, "error": f"Unknown action: {action}", "error_code": "UNKNOWN_ACTION"}
        
        except Exception as e:
            logger.error(f"Window interaction failed: {e}")
            return {"success": False, "error": f"Window interaction failed: {str(e)}", "error_code": "INTERACTION_FAILED"}


class VisualVerifyTool(BaseTool):
    """Verify visual state: check if element exists, text matches, image matches."""
    
    manifest = {
        "name": "visual_verify",
        "display_name": "Visual Verification",
        "description": "Verify visual state: check element exists, text matches, image matches, or condition met.",
        "category": "visual",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "check_type": {
                    "type": "string",
                    "enum": ["element_exists", "text_present", "text_matches", "image_matches", "pixel_color"],
                    "description": "Type of verification"
                },
                "window_title": {"type": "string", "description": "Window to check"},
                "region": {
                    "type": "object",
                    "description": "Region to check",
                    "properties": {
                        "left": {"type": "integer"},
                        "top": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["left", "top", "width", "height"],
                },
                "expected_text": {"type": "string", "description": "Expected text (for text_matches)"},
                "search_text": {"type": "string", "description": "Text to find (for text_present)"},
                "template_image": {"type": "string", "description": "Template image path (for image_matches)"},
                "threshold": {"type": "number", "default": 0.8, "description": "Match threshold"},
                "expected_color": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Expected RGB color (for pixel_color)"
                },
                "coordinates": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                },
            },
            "required": ["check_type"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "actual": {"type": "string"},
                "expected": {"type": "string"},
                "confidence": {"type": "number"},
            },
        },
        "required_permissions": ["screen:verify"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Verifies visual state on screen.",
        "requires_approval": False,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["visual", "verify", "assert", "test"],
    }
    
    async def _execute_impl(self, context) -> dict:
        _ensure_default_desktop()
        if not OPENCV_AVAILABLE or not MSS_AVAILABLE or not WIN32_AVAILABLE:
            return {"success": False, "error": "Required dependencies not available", "error_code": "DEPENDENCY_MISSING"}
        
        args = context.arguments
        check_type = args.get("check_type")
        window_title = args.get("window_title")
        region = args.get("region")
        expected_text = args.get("expected_text")
        search_text = args.get("search_text")
        template_path = args.get("template_image")
        threshold = args.get("threshold", 0.8)
        expected_color = args.get("expected_color")
        coordinates = args.get("coordinates")
        
        try:
            # Capture screen/window
            with mss.mss() as sct:
                if window_title:
                    found_hwnd = None
                    def enum_cb(hwnd, _):
                        nonlocal found_hwnd
                        try:
                            if win32gui.IsWindowVisible(hwnd):
                                title = win32gui.GetWindowText(hwnd)
                                if window_title.lower() in title.lower():
                                    found_hwnd = hwnd
                                    return False
                        except Exception:
                            pass
                        return True
                    try:
                        win32gui.EnumWindows(enum_cb, None)
                    except Exception as e:
                        return {"success": False, "error": f"Failed to enumerate windows: {e}", "error_code": "ENUM_FAILED"}
                    
                    if not found_hwnd:
                        return {"success": False, "error": "Window not found", "error_code": "WINDOW_NOT_FOUND"}
                    
                    try:
                        rect = win32gui.GetWindowRect(found_hwnd)
                        monitor = {"left": rect[0], "top": rect[1], 
                                  "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
                    except Exception as e:
                        return {"success": False, "error": f"Failed to get window rect: {e}", "error_code": "WINDOW_ERROR"}
                elif region:
                    monitor = {
                        "left": region["left"],
                        "top": region["top"],
                        "width": region["width"],
                        "height": region["height"],
                    }
                else:
                    monitor = sct.monitors[0]
                
                screenshot = sct.grab(monitor)
                from PIL import Image
                pil_image = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                screen = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)
            
            passed = False
            actual = ""
            confidence = 0.0
            
            if check_type == "text_present":
                if not search_text:
                    return {"success": False, "error": "search_text required", "error_code": "INVALID_INPUT"}
                
                if not PYTESSERACT_AVAILABLE:
                    return {"success": False, "error": "pytesseract not available", "error_code": "DEPENDENCY_MISSING"}
                
                ocr_text = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pytesseract.image_to_string(screen, lang="eng")
                )
                
                # Normalize spaces and check
                norm_search = " ".join(search_text.lower().split())
                norm_ocr = " ".join(ocr_text.lower().split())
                passed = norm_search in norm_ocr
                if not passed:
                    # Token/word-based check: if all words appear in order or within proximity
                    words = norm_search.split()
                    if words and all(w in norm_ocr for w in words):
                        passed = True
                
                actual = "Text found" if passed else f"Text not found in OCR: {norm_ocr[:120]}"
                confidence = 1.0 if passed else 0.0
            
            elif check_type == "text_matches":
                if not expected_text:
                    return {"success": False, "error": "expected_text required", "error_code": "INVALID_INPUT"}
                
                if not PYTESSERACT_AVAILABLE:
                    return {"success": False, "error": "pytesseract not available", "error_code": "DEPENDENCY_MISSING"}
                
                ocr_text = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: pytesseract.image_to_string(screen, lang="eng")
                )
                
                passed = expected_text.lower() in ocr_text.lower()
                actual = f"Found: {ocr_text[:100]}..."
                confidence = 1.0 if passed else 0.0
            
            elif check_type == "image_matches":
                if not template_path or not Path(template_path).exists():
                    return {"success": False, "error": "Template image not found", "error_code": "NOT_FOUND"}
                
                template = cv2.imread(template_path)
                if template is None:
                    return {"success": False, "error": "Failed to load template", "error_code": "INVALID_INPUT"}
                
                result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                
                passed = max_val >= threshold
                actual = f"Match confidence: {max_val:.2f}"
                confidence = float(max_val)
            
            elif check_type == "pixel_color":
                if not expected_color or len(expected_color) != 3:
                    return {"success": False, "error": "expected_color must be [R,G,B]", "error_code": "INVALID_INPUT"}
                if not coordinates:
                    return {"success": False, "error": "coordinates required", "error_code": "INVALID_INPUT"}
                
                x, y = coordinates["x"], coordinates["y"]
                if 0 <= y < screen.shape[0] and 0 <= x < screen.shape[1]:
                    pixel = screen[y, x]
                    actual_color = [int(pixel[2]), int(pixel[1]), int(pixel[0])]  # BGR to RGB
                    diff = sum(abs(a - b) for a, b in zip(actual_color, expected_color))
                    passed = diff <= 30  # Tolerance
                    actual = f"RGB: {actual_color}"
                    confidence = 1.0 - min(diff / 30.0, 1.0)
                else:
                    return {"success": False, "error": "Coordinates out of bounds", "error_code": "OUT_OF_BOUNDS"}
            
            elif check_type == "element_exists":
                # Use OCR to check for text or template matching
                if search_text and PYTESSERACT_AVAILABLE:
                    ocr_text = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: pytesseract.image_to_string(screen, lang="eng")
                    )
                    passed = search_text.lower() in ocr_text.lower()
                    actual = "Element text found" if passed else "Element text not found"
                    confidence = 1.0 if passed else 0.0
                elif template_path and Path(template_path).exists():
                    template = cv2.imread(template_path)
                    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, _ = cv2.minMaxLoc(result)
                    passed = max_val >= threshold
                    actual = f"Match: {max_val:.2f}"
                    confidence = float(max_val)
                else:
                    return {"success": False, "error": "search_text or template_image required", "error_code": "INVALID_INPUT"}
            
            return {
                "success": True,
                "result": {
                    "passed": passed,
                    "actual": actual,
                    "expected": expected_text or search_text or "N/A",
                    "confidence": confidence,
                },
                "error": None,
            }
        
        except Exception as e:
            logger.error(f"Visual verify failed: {e}")
            return {"success": False, "error": f"Verification failed: {str(e)}", "error_code": "VERIFY_FAILED"}


# Tool registration
VISUAL_TOOLS = [
    "ScreenCaptureTool",
    "ScreenOCRTool",
    "VisualFindTool",
    "MouseControlTool",
    "KeyboardInputTool",
    "WindowInteractTool",
    "VisualVerifyTool",
]

__all__ = [
    "ScreenCaptureTool",
    "ScreenOCRTool",
    "VisualFindTool",
    "MouseControlTool",
    "KeyboardInputTool",
    "WindowInteractTool",
    "VisualVerifyTool",
    "VISUAL_TOOLS",
    "MSS_AVAILABLE",
    "PYTESSERACT_AVAILABLE",
    "OPENCV_AVAILABLE",
    "PYWINAUTO_AVAILABLE",
    "WIN32_AVAILABLE",
]