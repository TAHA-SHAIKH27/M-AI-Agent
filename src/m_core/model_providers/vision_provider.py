"""Vision model provider for GUI understanding and visual grounding.

This provider uses NVIDIA's cloud vision API (Nemotron or NVLM models) for:
- GUI element detection with bounding boxes
- Visual grounding (finding elements by description)
- Screen understanding (describing what's on screen)
- Element classification (button, input, text, etc.)
- Action verification (before/after screenshot comparison)
- Next action recommendations for automation
"""
import asyncio
import base64
import io
import structlog
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass

import httpx

from m_core.common_schemas import (
    CompletionRequest, ChatMessage, MessageRole, ToolSchema, ToolFunction,
    CompletionResponse, StreamChunk, ModelCapability, ModelInfo,
    EmbeddingRequest, EmbeddingResponse, ProviderHealth,
)
from .base import ModelProvider
from ..config import settings

logger = structlog.get_logger(__name__)


@dataclass
class VisionConfig:
    """Configuration for vision model provider."""
    model_id: str = "nvidia/nv-embed-v1"  # Will use vision model from NVIDIA
    device: str = "auto"  # cloud only
    confidence_threshold: float = 0.3
    max_elements: int = 50
    screenshot_scale: float = 1.0
    # NVIDIA API specific
    nvidia_api_key: Optional[str] = None
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    vision_model: str = "nvidia/nv-embed-v1"  # Vision-capable model


class VisionModelProvider(ModelProvider):
    """Vision model provider using NVIDIA Cloud API for GUI understanding.
    
    Supports:
    - GUI element detection with bounding boxes via NVIDIA vision models
    - Visual grounding (finding elements by description)
    - Screen understanding (describing what's on screen)
    - Element classification (button, input, text, etc.)
    - Action verification (before/after screenshot comparison)
    - Next action recommendations for desktop automation
    """
    
    def __init__(self, config: Optional[VisionConfig] = None):
        self.config = config or VisionConfig()
        # Override with settings if available
        if settings.nvidia_api_key:
            self.config.nvidia_api_key = settings.nvidia_api_key
        if settings.nvidia_base_url:
            self.config.nvidia_base_url = settings.nvidia_base_url.rstrip("/")
            if not self.config.nvidia_base_url.endswith("/v1"):
                self.config.nvidia_base_url += "/v1"
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None
        self._model_type = "nvidia-cloud"
    
    @property
    def provider_id(self) -> str:
        return "vision-gui"
    
    @property
    def provider_name(self) -> str:
        return "NVIDIA Vision GUI Understanding"
    
    @property
    def provider_type(self) -> str:
        return "cloud"
    
    @property
    def capabilities(self) -> ModelCapability:
        return ModelCapability(
            chat=True,  # Can do chat with vision
            streaming=False,
            function_calling=True,
            vision=True,
            reasoning=True,
            max_context_tokens=4096,
            supported_languages=["en"],
        )
    
    async def initialize(self) -> None:
        """Initialize the vision model via NVIDIA API."""
        if self._initialized:
            return
        
        if not self.config.nvidia_api_key:
            logger.warning("NVIDIA API key not configured - Vision provider disabled")
            self._initialized = False
            return
        
        try:
            self._client = httpx.AsyncClient(
                base_url=self.config.nvidia_base_url,
                headers={
                    "Authorization": f"Bearer {self.config.nvidia_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
            
            # Test connection with a simple request
            test_response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": [
                        {"role": "user", "content": "test"}
                    ],
                    "max_tokens": 10,
                }
            )
            
            if test_response.status_code == 200:
                self._initialized = True
                logger.info(f"Vision provider initialized with NVIDIA model: {self.config.vision_model}")
            else:
                logger.warning(f"NVIDIA API test failed: {test_response.status_code} - {test_response.text}")
                self._initialized = False
                
        except Exception as e:
            logger.error(f"Failed to initialize vision provider: {e}")
            self._initialized = False
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self._initialized:
            return ProviderHealth(
                healthy=False,
                error="NVIDIA API key not configured or initialization failed",
                model_count=0,
            )
        
        try:
            # Quick health check
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
            )
            
            if response.status_code == 200:
                return ProviderHealth(
                    healthy=True,
                    latency_ms=100.0,
                    model_count=1,
                )
            else:
                return ProviderHealth(
                    healthy=False,
                    error=f"API returned {response.status_code}",
                    model_count=0,
                )
        except Exception as e:
            return ProviderHealth(
                healthy=False,
                error=str(e),
                model_count=0,
            )
    
    async def list_models(self) -> List[ModelInfo]:
        """List available models."""
        if not self._initialized:
            return []
        
        return [ModelInfo(
            id=self.config.vision_model,
            name=f"NVIDIA Vision GUI Detection",
            description="GUI element detection and screen understanding via NVIDIA Cloud API",
            capabilities={"vision": True, "gui_detection": True, "chat": True},
            max_context_length=4096,
        )]
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Complete chat with vision support."""
        if not self._initialized:
            return CompletionResponse(
                id="vision-not-initialized",
                model=self.config.vision_model,
                content="Vision provider not initialized. NVIDIA API key required.",
                finish_reason="error",
            )
        
        try:
            # Build messages with images if present
            messages = []
            for msg in request.messages:
                if msg.role == "user" and msg.content:
                    content = msg.content
                    # Check if content has images (vision)
                    if isinstance(content, list):
                        # Already structured with images
                        messages.append({"role": "user", "content": content})
                    else:
                        messages.append({"role": msg.role, "content": content})
                else:
                    messages.append({"role": msg.role, "content": msg.content})
            
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": messages,
                    "max_tokens": request.max_tokens or 4096,
                    "temperature": request.temperature or 0.7,
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return CompletionResponse(
                    id=data.get("id", "vision-complete"),
                    model=self.config.vision_model,
                    content=content,
                    finish_reason=data["choices"][0].get("finish_reason", "stop"),
                    latency_ms=0,
                )
            else:
                return CompletionResponse(
                    id="vision-error",
                    model=self.config.vision_model,
                    content=f"API error: {response.status_code} - {response.text}",
                    finish_reason="error",
                )
                
        except Exception as e:
            logger.error(f"Vision completion failed: {e}")
            return CompletionResponse(
                id="vision-error",
                model=self.config.vision_model,
                content=f"Error: {str(e)}",
                finish_reason="error",
            )
    
    async def stream(self, request: CompletionRequest):
        """Stream not fully supported for vision provider."""
        response = await self.complete(request)
        yield StreamChunk(
            id=response.id,
            content=response.content,
            finish_reason=response.finish_reason,
            is_final=True,
        )
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Not supported for vision provider."""
        return EmbeddingResponse(
            object="list",
            data=[],
            model=self.config.vision_model,
        )
    
    # ============================================================
    # VISION-SPECIFIC METHODS FOR GUI AUTOMATION
    # ============================================================
    
    def _build_vision_prompt(self, task: str, mode: str = "analyze") -> str:
        """Build specialized prompts for GUI automation tasks."""
        prompts = {
            "analyze": """You are a GUI automation expert. Analyze this screenshot and provide a structured JSON response.

Return ONLY valid JSON with this exact structure:
{
  "description": "Brief description of what's on screen",
  "elements": [
    {"type": "button|input|text|link|menu|checkbox|radio|dropdown|tab|window|dialog|icon|image|unknown", "label": "visible text or aria-label", "description": "what this element does", "bbox": {"left": x, "top": y, "width": w, "height": h}, "confidence": 0.0-1.0}
  ],
  "text_content": "all visible text concatenated",
  "suggested_actions": ["action 1", "action 2"],
  "app_type": "notepad|word|excel|chrome|edge|explorer|terminal|unknown"
}""",
            "find": f"""You are a GUI automation expert. Find elements matching: "{task}"

Return ONLY valid JSON:
{{
  "elements": [
    {{"type": "button|input|text|link|menu|checkbox|radio|dropdown|tab|window|dialog|icon|image|unknown", "label": "visible text", "description": "what this element does", "bbox": {{"left": x, "top": y, "width": w, "height": h}}, "confidence": 0.0-1.0}}
  ]
}}""",
            "verify": f"""You are a GUI automation expert. Compare these two screenshots and verify if the expected change occurred: "{task}"

Return ONLY valid JSON:
{{
  "verified": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "explanation of what changed",
  "before_elements_count": N,
  "after_elements_count": N
}}""",
        }
        return prompts.get(mode, prompts["analyze"])
    
    def _parse_vision_response(self, response_text: str, mode: str = "analyze") -> Dict[str, Any]:
        """Parse the vision model response into structured data."""
        import json
        import re
        
        # Try to extract JSON from response
        try:
            # Find JSON in response (may have markdown code blocks)
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # Try to find JSON object directly
                json_match = re.search(r'(\{.*\})', response_text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(1)
                else:
                    json_str = response_text
            
            data = json.loads(json_str)
            return data
        except Exception as e:
            logger.warning(f"Failed to parse vision response as JSON: {e}")
            return {"error": "Failed to parse response", "raw": response_text}
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Analyze a screenshot and return structured GUI understanding."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {
                "error": "Vision provider not initialized. NVIDIA API key required.",
                "elements": [],
                "model_type": self._model_type,
                "description": "Cannot analyze screen: Vision provider not available"
            }
        
        try:
            # Build prompt with task context
            prompt = self._build_vision_prompt(task_description, "analyze")
            
            # Prepare message with image
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_base64}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ]
            
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": messages,
                    "max_tokens": 4096,
                    "temperature": 0.1,  # Low temp for consistent structured output
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_vision_response(content, "analyze")
                
                if "error" not in parsed:
                    parsed["model_type"] = self._model_type
                return parsed
            else:
                return {"error": f"API error: {response.status_code} - {response.text}", "elements": [], "model_type": self._model_type}
                
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {"error": str(e), "elements": [], "model_type": self._model_type}
    
    async def find_elements(
        self,
        screenshot_base64: str,
        description: str,
        confidence_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Find GUI elements matching a natural language description."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return []
        
        try:
            prompt = self._build_vision_prompt(description, "find")
            
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_base64}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ]
            
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": messages,
                    "max_tokens": 2048,
                    "temperature": 0.1,
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_vision_response(content, "find")
                
                elements = parsed.get("elements", [])
                threshold = confidence_threshold or self.config.confidence_threshold
                
                # Filter by confidence
                filtered = [e for e in elements if e.get("confidence", 0) >= threshold]
                return filtered[:self.config.max_elements]
            else:
                return []
                
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
        # Find all elements and check which one contains the coordinates
        elements = await self.find_elements(screenshot_base64, "all interactive elements")
        
        for elem in elements:
            bbox = elem.get("bbox", {})
            if (bbox.get("left", 0) <= x <= bbox.get("left", 0) + bbox.get("width", 0) and
                bbox.get("top", 0) <= y <= bbox.get("top", 0) + bbox.get("height", 0)):
                return elem
        
        return None
    
    async def describe_screen(self, screenshot_base64: str) -> str:
        """Get a natural language description of the screen."""
        result = await self.analyze_screen(screenshot_base64, "Describe this screen in detail for automation")
        return result.get("description", "Unable to describe screen")
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
    ) -> Dict[str, Any]:
        """Verify that an action produced the expected visual change."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"verified": False, "confidence": 0.0, "error": "Vision provider not initialized"}
        
        try:
            prompt = self._build_vision_prompt(expected_change, "verify")
            
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"BEFORE screenshot: {prompt}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{before_screenshot}", "detail": "high"}},
                        {"type": "text", "text": "AFTER screenshot:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{after_screenshot}", "detail": "high"}},
                    ]
                }
            ]
            
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": messages,
                    "max_tokens": 1024,
                    "temperature": 0.1,
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_vision_response(content, "verify")
                return parsed
            else:
                return {"verified": False, "confidence": 0.0, "error": f"API error: {response.status_code}"}
                
        except Exception as e:
            logger.error(f"Action verification failed: {e}")
            return {"verified": False, "confidence": 0.0, "error": str(e)}
    
    async def get_next_action(
        self,
        screenshot_base64: str,
        current_task: str,
        previous_actions: List[str] = None,
    ) -> Dict[str, Any]:
        """Get the next recommended action for automation based on current screen state.
        
        This is the key method for the 'vision-guided automation' approach:
        1. Take screenshot
        2. Send to vision model with task context
        3. Model returns specific action to take
        4. Execute action
        5. Repeat until task complete
        """
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"action": "error", "reason": "Vision provider not initialized"}
        
        try:
            prev = "\n".join(previous_actions) if previous_actions else "None"
            
            prompt = f"""You are a GUI automation agent. Given the current screen and task, decide the next action.

Task: {current_task}
Previous actions: {prev}

Return ONLY valid JSON:
{{
  "action": "click|type|hotkey|wait|scroll|navigate|complete|error",
  "target": "element description or coordinates",
  "params": {{"text": "...", "keys": [...], "x": 0, "y": 0}},
  "reasoning": "why this action",
  "confidence": 0.0-1.0,
  "is_complete": true/false
}}"""
            
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_base64}",
                                "detail": "high"
                            }
                        }
                    ]
                }
            ]
            
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.vision_model,
                    "messages": messages,
                    "max_tokens": 1024,
                    "temperature": 0.1,
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = self._parse_vision_response(content, "analyze")
                return parsed
            else:
                return {"action": "error", "reason": f"API error: {response.status_code}"}
                
        except Exception as e:
            logger.error(f"Next action prediction failed: {e}")
            return {"action": "error", "reason": str(e)}


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