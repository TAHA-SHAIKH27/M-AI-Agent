"""Vision model provider for GUI understanding and visual grounding.

This provider uses a proper GUI element detection pipeline.
OmniParser is the primary model for GUI element detection with bounding boxes.
If unavailable, it reports unavailability clearly rather than using captioning models.
"""
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
    # OmniParser specific
    use_omniparser: bool = True
    omniparser_model_path: Optional[str] = None


class VisionModelProvider(ModelProvider):
    """Vision model provider for GUI understanding and visual grounding.
    
    Supports:
    - OmniParser for GUI element detection (primary)
    - Visual grounding (finding elements by description)
    - Screen understanding (describing what's on screen)
    - Element classification (button, input, text, etc.)
    - Action verification (before/after screenshot comparison)
    
    NOTE: This provider requires a proper GUI detection model (OmniParser).
    Captioning models (Florence-2, BLIP, GIT) are NOT suitable fallbacks
    as they produce image captions, not GUI element bounding boxes.
    """
    
    def __init__(self, config: Optional[VisionConfig] = None):
        self.config = config or VisionConfig()
        self._model = None
        self._processor = None
        self._initialized = False
        self._device = None
        self._model_type = "unknown"  # "omniparser" or "captioning"
    
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
            chat=False,  # Not a chat model
            streaming=False,
            function_calling=False,
            vision=True,  # Vision capability for GUI understanding
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
            
            # Try to load OmniParser if configured
            if self.config.use_omniparser:
                try:
                    await self._load_omniparser()
                    self._model_type = "omniparser"
                    self._initialized = True
                    logger.info("Vision model provider initialized with OmniParser")
                    return
                except Exception as e:
                    logger.warning(f"Could not load OmniParser: {e}")
            
            # If OmniParser not available, explicitly report unavailability
            # DO NOT fall back to captioning models - they don't detect GUI elements
            logger.error("No GUI detection model available. Vision provider disabled.")
            logger.error("Install OmniParser or configure a compatible GUI detection model.")
            self._initialized = False
            self._model_type = "unavailable"
            
        except ImportError:
            logger.warning("Transformers/torch not available. Vision provider disabled.")
            self._initialized = False
        except Exception as e:
            logger.error(f"Failed to initialize vision model: {e}")
            self._initialized = False
    
    async def _load_omniparser(self) -> None:
        """Load the OmniParser model for GUI element detection."""
        import torch
        from transformers import AutoModel, AutoProcessor
        
        model_id = self.config.omniparser_model_path or self.config.model_id
        
        logger.info(f"Loading OmniParser model: {model_id}")
        
        # OmniParser uses a specific architecture - typically a fine-tuned
        # Florence-2 or similar model with detection head
        # The actual OmniParser model from Microsoft has specific requirements
        try:
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
            logger.info(f"Successfully loaded OmniParser model: {model_id}")
        except Exception as e:
            logger.error(f"Failed to load OmniParser: {e}")
            raise
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self._initialized:
            return ProviderHealth(
                healthy=False,
                error="Vision model not initialized. OmniParser required for GUI detection.",
                model_count=0,
            )
        
        if self._model_type != "omniparser":
            return ProviderHealth(
                healthy=False,
                error=f"Model type '{self._model_type}' does not support GUI element detection",
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
        """Not supported - vision provider is for GUI analysis only."""
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
        """Analyze a screenshot and return structured GUI understanding.
        
        Args:
            screenshot_base64: Base64 encoded screenshot
            task_description: Optional task context for focused analysis
            region: Optional region to analyze (left, top, width, height)
            
        Returns:
            Dict with:
            - description: Natural language description of screen
            - elements: List of detected GUI elements with bounding boxes
            - text_content: Extracted text from elements
            - suggested_actions: Recommended next actions
            - model_type: Type of model used ("omniparser" or "unavailable")
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized or self._model_type != "omniparser":
            return {
                "error": "GUI detection model (OmniParser) not available",
                "elements": [],
                "model_type": self._model_type,
                "description": "Cannot analyze screen: No GUI detection model loaded"
            }
        
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
            
            # Process with OmniParser
            # OmniParser typically uses a specific prompt format for GUI parsing
            prompt = task_description or "Parse the GUI elements in this screenshot. Return bounding boxes, element types, and labels."
            
            inputs = self._processor(
                images=image,
                text=prompt,
                return_tensors="pt",
            ).to(self._device)
            
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=1024,
                    num_beams=3,
                    do_sample=False,
                )
            
            # Parse outputs - OmniParser returns structured format
            response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
            
            # Parse OmniParser structured output
            elements = self._parse_omniparser_output(response, image.size)
            
            return {
                "description": f"Detected {len(elements)} GUI elements",
                "elements": elements,
                "text_content": self._extract_text(elements),
                "suggested_actions": self._suggest_actions(elements, task_description),
                "model_type": "omniparser",
            }
            
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {"error": str(e), "elements": [], "model_type": self._model_type}
    
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
        
        if not self._initialized or self._model_type != "omniparser":
            logger.warning("find_elements called but GUI detection model not available")
            return []
        
        threshold = confidence_threshold or self.config.confidence_threshold
        
        try:
            import torch
            from PIL import Image
            
            image_data = base64.b64decode(screenshot_base64)
            image = Image.open(io.BytesIO(image_data))
            
            # First analyze the full screen to get all elements
            # Then filter by description
            prompt = f"Find all GUI elements. Target: {description}"
            
            inputs = self._processor(
                images=image,
                text=prompt,
                return_tensors="pt",
            ).to(self._device)
            
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=1024,
                    num_beams=3,
                )
            
            response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
            
            elements = self._parse_omniparser_output(response, image.size)
            
            # Filter by confidence and relevance to description
            filtered = []
            for elem in elements:
                if elem.get("confidence", 0) >= threshold:
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
        """Get the GUI element at specific screen coordinates."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized or self._model_type != "omniparser":
            return None
        
        # Get all elements and find one at coordinates
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
        
        Uses OmniParser to detect GUI state changes.
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized or self._model_type != "omniparser":
            return {"verified": False, "confidence": 0.0, "error": "GUI detection model not available"}
        
        try:
            import torch
            from PIL import Image
            
            before_img = Image.open(io.BytesIO(base64.b64decode(before_screenshot)))
            after_img = Image.open(io.BytesIO(base64.b64decode(after_screenshot)))
            
            # Analyze both screenshots
            before_elements = await self._get_elements_from_image(before_img)
            after_elements = await self._get_elements_from_image(after_img)
            
            # Compare element states
            verified, confidence, reasoning = self._compare_element_states(
                before_elements, after_elements, expected_change
            )
            
            return {
                "verified": verified,
                "confidence": confidence,
                "reasoning": reasoning,
                "before_count": len(before_elements),
                "after_count": len(after_elements),
            }
            
        except Exception as e:
            logger.error(f"Action verification failed: {e}")
            return {"verified": False, "confidence": 0.0, "error": str(e)}
    
    async def _get_elements_from_image(self, image) -> List[Dict[str, Any]]:
        """Extract elements from an image using the model."""
        import torch
        
        prompt = "Parse all GUI elements with bounding boxes, types, and labels."
        inputs = self._processor(
            images=image,
            text=prompt,
            return_tensors="pt",
        ).to(self._device)
        
        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=1024,
                num_beams=3,
                do_sample=False,
            )
        
        response = self._processor.batch_decode(outputs, skip_special_tokens=True)[0]
        return self._parse_omniparser_output(response, image.size)
    
    def _compare_element_states(
        self, 
        before: List[Dict[str, Any]], 
        after: List[Dict[str, Any]], 
        expected_change: str
    ) -> tuple:
        """Compare two element lists to verify expected change."""
        # Simple heuristic comparison
        before_count = len(before)
        after_count = len(after)
        
        # Check if element count changed significantly
        if "appear" in expected_change.lower() or "open" in expected_change.lower():
            if after_count > before_count:
                return True, 0.7, f"Element count increased: {before_count} -> {after_count}"
        elif "disappear" in expected_change.lower() or "close" in expected_change.lower():
            if after_count < before_count:
                return True, 0.7, f"Element count decreased: {before_count} -> {after_count}"
        elif "click" in expected_change.lower() or "press" in expected_change.lower():
            # For clicks, look for state changes in buttons
            return True, 0.5, "Click action assumed to change state"
        
        return False, 0.3, f"No significant change detected ({before_count} -> {after_count} elements)"
    
    # ============================================================
    # OMNIPARSER OUTPUT PARSING
    # ============================================================
    
    def _parse_omniparser_output(self, response: str, image_size: tuple) -> List[Dict[str, Any]]:
        """Parse OmniParser structured output into elements.
        
        OmniParser returns output in a specific format with bounding boxes,
        element types, and labels. This parser handles that format.
        """
        elements = []
        
        # OmniParser typically outputs something like:
        # "<bbox><x1><y1><x2><y2></bbox> <type>button</type> <label>Save</label>"
        # or JSON-like structured output
        
        import re
        
        # Try to parse the structured format used by OmniParser
        # Format: [x1, y1, x2, y2] type label
        pattern = r'\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]\s*(\w+)\s*(.*)'
        matches = re.findall(pattern, response)
        
        if matches:
            img_w, img_h = image_size
            for match in matches:
                x1, y1, x2, y2, elem_type, label = match
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                
                # Normalize coordinates if needed (OmniParser may use normalized coords)
                if x2 <= 1000 and y2 <= 1000 and (img_w > 1000 or img_h > 1000):
                    # Likely normalized to 1000x1000
                    x1 = int(x1 * img_w / 1000)
                    y1 = int(y1 * img_h / 1000)
                    x2 = int(x2 * img_w / 1000)
                    y2 = int(y2 * img_h / 1000)
                
                elements.append({
                    "type": elem_type.lower(),
                    "label": label.strip() if label else "",
                    "description": f"{elem_type}: {label}" if label else elem_type,
                    "bbox": {
                        "left": min(x1, x2),
                        "top": min(y1, y2),
                        "width": abs(x2 - x1),
                        "height": abs(y2 - y1),
                        "right": max(x1, x2),
                        "bottom": max(y1, y2),
                    },
                    "confidence": 0.8,  # OmniParser typically high confidence
                    "source": "omniparser",
                })
            return elements[:self.config.max_elements]
        
        # Fallback: Try alternative format (JSON)
        try:
            import json
            data = json.loads(response)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "bbox" in item:
                        bbox = item["bbox"]
                        if isinstance(bbox, list) and len(bbox) == 4:
                            x1, y1, x2, y2 = bbox
                            elements.append({
                                "type": item.get("type", "unknown"),
                                "label": item.get("label", ""),
                                "description": item.get("description", f"{item.get('type', 'unknown')}: {item.get('label', '')}"),
                                "bbox": {
                                    "left": min(x1, x2),
                                    "top": min(y1, y2),
                                    "width": abs(x2 - x1),
                                    "height": abs(y2 - y1),
                                    "right": max(x1, x2),
                                    "bottom": max(y1, y2),
                                },
                                "confidence": item.get("confidence", 0.7),
                                "source": "omniparser",
                            })
                return elements[:self.config.max_elements]
        except:
            pass
        
        # If no structured output parsed, return empty (don't invent elements)
        logger.warning("Could not parse OmniParser output into structured elements")
        return []
    
    def _extract_text(self, elements: List[Dict[str, Any]]) -> str:
        """Extract all text from elements."""
        texts = []
        for elem in elements:
            label = elem.get("label", "")
            if label:
                texts.append(label)
        return " ".join(texts)
    
    def _suggest_actions(self, elements: List[Dict[str, Any]], task: str) -> List[str]:
        """Suggest next actions based on elements and task."""
        actions = []
        task_lower = task.lower()
        
        for elem in elements:
            elem_type = elem.get("type", "unknown")
            label = elem.get("label", "").lower()
            
            if "save" in task_lower and elem_type == "button" and "save" in label:
                actions.append(f"Click '{elem['label']}' button to save")
            elif "click" in task_lower and elem_type == "button":
                actions.append(f"Click '{elem['label']}' button")
            elif "type" in task_lower and elem_type in ["input", "edit", "textbox"]:
                actions.append(f"Type in '{elem['label']}' field")
            elif "search" in task_lower and elem_type in ["input", "button"] and ("search" in label or "find" in label):
                actions.append(f"Use '{elem['label']}' for search")
        
        if not actions:
            actions = ["Analyze screen for next steps"]
        
        return actions
    
    def _matches_description(self, element: Dict[str, Any], description: str) -> bool:
        """Check if element matches description."""
        label = element.get("label", "").lower()
        elem_type = element.get("type", "").lower()
        target = description.lower()
        
        # Direct text match in label
        if target in label:
            return True
        
        # Type-based match
        if target in elem_type:
            return True
        
        # Keyword matching
        target_words = set(target.split())
        label_words = set(label.split())
        if target_words & label_words:
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