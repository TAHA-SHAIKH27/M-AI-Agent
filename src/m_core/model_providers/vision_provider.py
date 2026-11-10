"""Vision model provider for GUI understanding and visual grounding."""
import asyncio
import base64
import io
import structlog
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass

from m_core.common_schemas import (
    CompletionRequest, ChatMessage, MessageRole, ToolSchema, ToolFunction,
    CompletionResponse, StreamChunk, ModelCapability, ModelInfo,
    EmbeddingRequest, EmbeddingResponse, ProviderHealth,
)
from .base import ModelProvider

logger = structlog.get_logger(__name__)


@dataclass
class VisionConfig:
    """Configuration for vision model provider."""
    model_id: str = "microsoft/omniparser-v2"
    device: str = "auto"  # auto, cpu, cuda
    confidence_threshold: float = 0.3
    max_elements: int = 50
    screenshot_scale: float = 1.0


class VisionModelProvider(ModelProvider):
    """Vision model provider for GUI understanding and visual grounding.
    
    Supports:
    - OmniParser for GUI element detection
    - Visual grounding (finding elements by description)
    - Screen understanding (describing what's on screen)
    - Element classification (button, input, text, etc.)
    """
    
    def __init__(self, config: Optional[VisionConfig] = None):
        self.config = config or VisionConfig()
        self._model = None
        self._processor = None
        self._initialized = False
        self._device = None
    
    @property
    def provider_id(self) -> str:
        return "vision-gui"
    
    @property
    def provider_name(self) -> str:
        return "Vision GUI Understanding"
    
    @property
    def provider_type(self) -> str:
        return "local"
    
    @property
    def capabilities(self) -> ModelCapability:
        return ModelCapability(
            chat=True,
            streaming=False,
            function_calling=False,
            vision=True,  # Vision capability!
            reasoning=False,
            max_context_tokens=4096,
            supported_languages=["en"],
        )
    
    async def initialize(self) -> None:
        """Initialize the vision model."""
        if self._initialized:
            return
        
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
            
            # Determine device
            if self.config.device == "auto":
                self._device = "cuda" if torch.cuda.is_available() else "cpu"
            else:
                self._device = self.config.device
            
            logger.info(f"Loading vision model on {self._device}")
            
            # Load OmniParser model and processor
            # Note: OmniParser uses a specific model architecture
            # For now, we'll use a compatible vision-language model
            # In production, this would load the actual OmniParser model
            
            try:
                self._model = AutoModel.from_pretrained(
                    self.config.model_id,
                    torch_dtype=torch.float16 if self._device == "cuda" else torch.float32,
                    device_map=self._device,
                    trust_remote_code=True,
                )
                self._processor = AutoProcessor.from_pretrained(
                    self.config.model_id,
                    trust_remote_code=True,
                )
            except Exception as e:
                logger.warning(f"Could not load OmniParser model: {e}. Using fallback.")
                # Fallback: Use a general vision-language model
                await self._load_fallback_model()
            
            self._initialized = True
            logger.info("Vision model provider initialized")
            
        except ImportError:
            logger.warning("Transformers/torch not available. Vision provider disabled.")
            self._initialized = False
        except Exception as e:
            logger.error(f"Failed to initialize vision model: {e}")
            self._initialized = False
    
    async def _load_fallback_model(self) -> None:
        """Load a fallback vision-language model."""
        import torch
        from transformers import AutoModel, AutoProcessor
        
        # Use a lightweight vision-language model as fallback
        fallback_models = [
            "microsoft/Florence-2-base",
            "microsoft/git-base",
            "Salesforce/blip-image-captioning-base",
        ]
        
        for model_id in fallback_models:
            try:
                logger.info(f"Trying fallback model: {model_id}")
                self._model = AutoModel.from_pretrained(
                    model_id,
                    torch_dtype=torch.float16 if self._device == "cuda" else torch.float32,
                    device_map=self._device,
                    trust_remote_code=True,
                )
                self._processor = AutoProcessor.from_pretrained(
                    model_id,
                    trust_remote_code=True,
                )
                self.config.model_id = model_id
                logger.info(f"Loaded fallback model: {model_id}")
                return
            except Exception as e:
                logger.warning(f"Fallback {model_id} failed: {e}")
                continue
        
        raise RuntimeError("Could not load any vision model")
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self._initialized:
            return ProviderHealth(
                healthy=False,
                error="Vision model not initialized",
                model_count=0,
            )
        
        try:
            import torch
            return ProviderHealth(
                healthy=True,
                latency_ms=100.0,
                model_count=1,
            )
        except Exception as e:
            return ProviderHealth(
                healthy=False,
                error=str(e),
                model_count=0,
            )
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Not directly used for chat - use specialized methods instead."""
        return CompletionResponse(
            id="vision-not-supported",
            model=self.config.model_id,
            content="Vision provider does not support direct chat. Use analyze_screen() or find_elements() instead.",
            finish_reason="error",
        )
    
    async def stream(self, request: CompletionRequest):
        """Not supported for vision provider."""
        yield StreamChunk(
            id="vision-not-supported",
            content="Vision provider does not support streaming chat.",
            finish_reason="error",
            is_final=True,
        )
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Not supported for vision provider."""
        return EmbeddingResponse(
            object="list",
            data=[],
            model=self.config.model_id,
        )
    
    # ============================================================
    # VISION-SPECIFIC METHODS
    # ============================================================
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Analyze a screenshot and return structured understanding.
        
        Args:
            screenshot_base64: Base64 encoded screenshot
            task_description: Optional task context for focused analysis
            region: Optional region to analyze (left, top, width, height)
            
        Returns:
            Dict with:
            - description: Natural language description of screen
            - elements: List of detected GUI elements
            - text_content: Extracted text
            - suggested_actions: Recommended next actions
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"error": "Vision model not available", "elements": []}
        
        try:
            import torch
            from PIL import Image
            
            # Decode screenshot
            image_data = base64.b64decode(screenshot_base64)
            image = Image.open(io.BytesIO(image_data))
            
            # Crop to region if specified
            if region:
                image = image.crop((
                    region["left"], region["top"],
                    region["left"] + region["width"],
                    region["top"] + region["height"]
                ))
            
            # Process with vision model
            inputs = self._processor(
                images=image,
                text=task_description or "Describe this screen and identify all interactive elements",
                return_tensors="pt",
            ).to(self._device)
            
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=512,
                    num_beams=3,
                    do_sample=False,
                )
            
            # Parse outputs
            response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
            
            # Parse structured output
            elements = self._parse_elements(response)
            
            return {
                "description": response,
                "elements": elements,
                "text_content": self._extract_text(elements),
                "suggested_actions": self._suggest_actions(elements, task_description),
            }
            
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {"error": str(e), "elements": []}
    
    async def find_elements(
        self,
        screenshot_base64: str,
        description: str,
        confidence_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Find GUI elements matching a natural language description.
        
        Args:
            screenshot_base64: Base64 encoded screenshot
            description: Natural language description (e.g., "the save button", "search input field")
            confidence_threshold: Minimum confidence threshold
            
        Returns:
            List of matching elements with bounding boxes and confidence scores
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return []
        
        threshold = confidence_threshold or self.config.confidence_threshold
        
        try:
            import torch
            from PIL import Image
            
            image_data = base64.b64decode(screenshot_base64)
            image = Image.open(io.BytesIO(image_data))
            
            # Use visual grounding prompt
            prompt = f"Find the element: {description}. Return bounding box and confidence."
            
            inputs = self._processor(
                images=image,
                text=prompt,
                return_tensors="pt",
            ).to(self._device)
            
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=256,
                    num_beams=3,
                )
            
            response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
            
            elements = self._parse_elements(response)
            
            # Filter by confidence and relevance
            filtered = []
            for elem in elements:
                if elem.get("confidence", 0) >= threshold:
                    # Check if description matches element
                    if self._matches_description(elem, description):
                        filtered.append(elem)
            
            return filtered[:self.config.max_elements]
            
        except Exception as e:
            logger.error(f"Element finding failed: {e}")
            return []
    
    async def get_element_at(
        self,
        screenshot_base64: str,
        x: int,
        y: int,
    ) -> Optional[Dict[str, Any]]:
        """Get the GUI element at specific screen coordinates.
        
        Args:
            screenshot_base64: Base64 encoded screenshot
            x: X coordinate
            y: Y coordinate
            
        Returns:
            Element info at that position or None
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return None
        
        # First get all elements, then find one at coordinates
        elements = await self.find_elements(screenshot_base64, "all interactive elements")
        
        for elem in elements:
            bbox = elem.get("bbox", {})
            if (bbox.get("left", 0) <= x <= bbox.get("right", 0) and
                bbox.get("top", 0) <= y <= bbox.get("bottom", 0)):
                return elem
        
        return None
    
    async def describe_screen(self, screenshot_base64: str) -> str:
        """Get a natural language description of the screen."""
        result = await self.analyze_screen(screenshot_base64, "Describe this screen in detail")
        return result.get("description", "Unable to describe screen")
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
    ) -> Dict[str, Any]:
        """Verify that an action produced the expected visual change.
        
        Args:
            before_screenshot: Screenshot before action
            after_screenshot: Screenshot after action
            expected_change: Description of expected change
            
        Returns:
            Verification result with confidence
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"verified": False, "confidence": 0.0, "error": "Vision model not available"}
        
        try:
            import torch
            from PIL import Image
            
            before_img = Image.open(io.BytesIO(base64.b64decode(before_screenshot)))
            after_img = Image.open(io.BytesIO(base64.b64decode(after_screenshot)))
            
            # Compare screenshots
            prompt = f"Compare these two screenshots. Expected change: {expected_change}. Did the change occur? Answer YES/NO with confidence."
            
            # For multi-image comparison, we need to combine or process sequentially
            # This is a simplified implementation
            inputs = self._processor(
                images=[before_img, after_img],
                text=prompt,
                return_tensors="pt",
            ).to(self._device)
            
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=128,
                )
            
            response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
            
            # Parse response
            verified = "YES" in response.upper()
            confidence = 0.8 if verified else 0.3
            
            return {
                "verified": verified,
                "confidence": confidence,
                "reasoning": response,
            }
            
        except Exception as e:
            logger.error(f"Action verification failed: {e}")
            return {"verified": False, "confidence": 0.0, "error": str(e)}
    
    # ============================================================
    # HELPER METHODS
    # ============================================================
    
    def _parse_elements(self, response: str) -> List[Dict[str, Any]]:
        """Parse model response into structured elements."""
        elements = []
        
        # Try to parse structured output (JSON-like or formatted text)
        # This is a simplified parser - production would use more robust parsing
        lines = response.split("\n")
        current_element = {}
        
        for line in lines:
            line = line.strip()
            if not line:
                if current_element:
                    elements.append(current_element)
                    current_element = {}
                continue
            
            # Look for element indicators
            if any(kw in line.lower() for kw in ["button", "input", "text", "link", "menu", "icon", "element"]):
                if current_element:
                    elements.append(current_element)
                current_element = {
                    "type": self._classify_element(line),
                    "description": line,
                    "confidence": 0.7,
                }
            elif "bbox" in line.lower() or "bounding box" in line.lower():
                # Try to extract coordinates
                bbox = self._extract_bbox(line)
                if bbox:
                    current_element["bbox"] = bbox
            elif "confidence" in line.lower():
                try:
                    conf = float("".join(c for c in line if c.isdigit() or c == "."))
                    current_element["confidence"] = conf
                except:
                    pass
            else:
                if current_element:
                    current_element["description"] += " " + line
        
        if current_element:
            elements.append(current_element)
        
        # If no structured elements found, create from raw response
        if not elements:
            elements = [{
                "type": "unknown",
                "description": response[:200],
                "confidence": 0.5,
            }]
        
        return elements[:self.config.max_elements]
    
    def _classify_element(self, description: str) -> str:
        """Classify element type from description."""
        desc = description.lower()
        if "button" in desc:
            return "button"
        elif "input" in desc or "text field" in desc or "edit" in desc:
            return "input"
        elif "link" in desc or "hyperlink" in desc:
            return "link"
        elif "menu" in desc:
            return "menu"
        elif "icon" in desc:
            return "icon"
        elif "text" in desc and "field" not in desc:
            return "text"
        elif "image" in desc or "picture" in desc:
            return "image"
        elif "scroll" in desc:
            return "scrollbar"
        elif "tab" in desc:
            return "tab"
        elif "checkbox" in desc:
            return "checkbox"
        elif "radio" in desc:
            return "radio"
        elif "dropdown" in desc or "combo" in desc:
            return "dropdown"
        elif "slider" in desc:
            return "slider"
        elif "progress" in desc:
            return "progressbar"
        elif "title" in desc or "header" in desc:
            return "header"
        elif "status" in desc:
            return "status"
        else:
            return "unknown"
    
    def _extract_bbox(self, line: str) -> Optional[Dict[str, int]]:
        """Extract bounding box from text line."""
        import re
        # Look for patterns like [x, y, w, h] or x=100 y=200 w=300 h=400
        patterns = [
            r'\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]',  # [x, y, w, h]
            r'x[=:]\s*(\d+).*?y[=:]\s*(\d+).*?w[=:]\s*(\d+).*?h[=:]\s*(\d+)',
            r'left[=:]\s*(\d+).*?top[=:]\s*(\d+).*?width[=:]\s*(\d+).*?height[=:]\s*(\d+)',
            r'(\d+),\s*(\d+),\s*(\d+),\s*(\d+)',  # x, y, w, h
        ]
        
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                x, y, w, h = map(int, match.groups())
                return {
                    "left": x,
                    "top": y,
                    "width": w,
                    "height": h,
                    "right": x + w,
                    "bottom": y + h,
                }
        return None
    
    def _extract_text(self, elements: List[Dict[str, Any]]) -> str:
        """Extract all text from elements."""
        texts = []
        for elem in elements:
            desc = elem.get("description", "")
            if desc and elem.get("type") in ["text", "button", "link", "header", "input"]:
                texts.append(desc)
        return " ".join(texts)
    
    def _suggest_actions(self, elements: List[Dict[str, Any]], task: str) -> List[str]:
        """Suggest next actions based on elements and task."""
        actions = []
        task_lower = task.lower()
        
        for elem in elements:
            elem_type = elem.get("type", "unknown")
            desc = elem.get("description", "").lower()
            
            if "save" in task_lower and elem_type == "button" and "save" in desc:
                actions.append(f"Click '{elem['description']}' to save")
            elif "click" in task_lower and elem_type == "button":
                actions.append(f"Click '{elem['description']}'")
            elif "type" in task_lower and elem_type == "input":
                actions.append(f"Type in '{elem['description']}'")
            elif "search" in task_lower and elem_type in ["input", "button"] and ("search" in desc or "find" in desc):
                actions.append(f"Use '{elem['description']}' for search")
        
        if not actions:
            actions = ["Analyze screen for next steps"]
        
        return actions
    
    def _matches_description(self, element: Dict[str, Any], description: str) -> bool:
        """Check if element matches description."""
        elem_desc = element.get("description", "").lower()
        elem_type = element.get("type", "").lower()
        target = description.lower()
        
        # Direct text match
        if target in elem_desc:
            return True
        
        # Type-based match
        if target in elem_type:
            return True
        
        # Keyword matching
        target_words = set(target.split())
        elem_words = set(elem_desc.split())
        if target_words & elem_words:
            return True
        
        return False


# Factory function for easy instantiation
async def create_vision_provider(config: Optional[VisionConfig] = None) -> VisionModelProvider:
    """Create and initialize a vision model provider."""
    provider = VisionModelProvider(config)
    await provider.initialize()
    return provider


# Export
__all__ = [
    "VisionModelProvider",
    "VisionConfig",
    "create_vision_provider",
]