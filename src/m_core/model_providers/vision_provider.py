"""Vision model provider abstraction for GUI understanding and visual grounding.

Supports multiple vision providers with automatic fallback:
- PRIMARY: NVIDIA NIM/API vision models (NVLM, Nemotron-3V, etc.)
- FALLBACK: Google Gemini API with image input

Provides structured JSON output for:
- Screen analysis with element detection
- Visual grounding (finding elements by description)
- Action planning (next action recommendations)
- Action verification (before/after comparison)
"""
import asyncio
import base64
import io
import json
import structlog
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum

import httpx

from m_core.common_schemas import (
    CompletionRequest, ChatMessage, MessageRole, ToolSchema, ToolFunction,
    CompletionResponse, StreamChunk, ModelCapability, ModelInfo,
    EmbeddingRequest, EmbeddingResponse, ProviderHealth,
)
from .base import ModelProvider
from ..config import settings

logger = structlog.get_logger(__name__)


class VisionProviderType(str, Enum):
    """Supported vision provider types."""
    NVIDIA_NIM = "nvidia_nim"
    GEMINI = "gemini"


@dataclass
class VisionConfig:
    """Configuration for vision model provider."""
    # Provider selection
    primary_provider: VisionProviderType = VisionProviderType.NVIDIA_NIM
    fallback_providers: List[VisionProviderType] = field(default_factory=lambda: [VisionProviderType.GEMINI])
    
    # NVIDIA NIM settings
    nvidia_api_key: Optional[str] = None
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_vision_model: str = "nvidia/nvlm-d-72b"  # NVLM-D-72B vision model
    
    # Google Gemini settings
    gemini_api_key: Optional[str] = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_vision_model: str = "gemini-2.5-flash"
    
    # Common settings
    confidence_threshold: float = 0.3
    max_elements: int = 50
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    
    # Rate limiting
    requests_per_minute: int = 30
    
    def __post_init__(self):
        """Populate from settings if not explicitly set."""
        # Only populate from settings if the field wasn't explicitly provided
        # (We can't easily detect this, so we'll always use settings as fallback)
        if self.nvidia_api_key is None:
            self.nvidia_api_key = settings.nvidia_api_key
        if self.nvidia_base_url == "https://integrate.api.nvidia.com/v1" and settings.nvidia_base_url:
            self.nvidia_base_url = settings.nvidia_base_url
        if self.nvidia_vision_model == "nvidia/nvlm-d-72b" and settings.nvidia_vision_model:
            self.nvidia_vision_model = settings.nvidia_vision_model
            
        if self.gemini_api_key is None:
            self.gemini_api_key = settings.gemini_api_key
        if self.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta" and settings.gemini_base_url:
            self.gemini_base_url = settings.gemini_base_url
        if self.gemini_vision_model in ("gemini-1.5-flash", "gemini-2.5-flash") and settings.gemini_vision_model:
            self.gemini_vision_model = settings.gemini_vision_model


@dataclass
class VisionAction:
    """Structured action returned by vision model."""
    action: str  # click, type, hotkey, wait, scroll, navigate, complete, error
    target: str  # element description or coordinates
    params: Dict[str, Any]  # {"text": "...", "keys": [...], "x": 0, "y": 0}
    reasoning: str
    confidence: float
    is_complete: bool = False
    expected_result: Optional[str] = None
    screen_state: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "params": self.params,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "is_complete": self.is_complete,
            "expected_result": self.expected_result,
            "screen_state": self.screen_state,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VisionAction":
        return cls(
            action=data.get("action", "error"),
            target=data.get("target", ""),
            params=data.get("params", {}),
            reasoning=data.get("reasoning", ""),
            confidence=float(data.get("confidence", 0.0)),
            is_complete=data.get("is_complete", False),
            expected_result=data.get("expected_result"),
            screen_state=data.get("screen_state"),
        )


@dataclass
class ScreenAnalysis:
    """Structured screen analysis from vision model."""
    description: str
    elements: List[Dict[str, Any]]
    text_content: str
    suggested_actions: List[str]
    app_type: str
    model_type: str
    screenshot_dimensions: Dict[str, int]
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "elements": self.elements,
            "text_content": self.text_content,
            "suggested_actions": self.suggested_actions,
            "app_type": self.app_type,
            "model_type": self.model_type,
            "screenshot_dimensions": self.screenshot_dimensions,
            "timestamp": self.timestamp,
        }


class BaseVisionProvider(ModelProvider):
    """Base class for vision providers."""
    
    def __init__(self, config: VisionConfig):
        self.config = config
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request_time = 0.0
        self._request_count = 0
        self._rate_limit_window_start = time.time()
    
    @property
    def provider_type_name(self) -> VisionProviderType:
        raise NotImplementedError
    
    @property
    def provider_id(self) -> str:
        return f"vision-{self.provider_type_name.value}"
    
    @property
    def provider_name(self) -> str:
        return f"{self.provider_type_name.value.title()} Vision"
    
    @property
    def provider_type(self) -> str:
        return "cloud"

    @property
    def id(self) -> str:
        return self.provider_id

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def type(self) -> str:
        return self.provider_type
    
    @property
    def capabilities(self) -> ModelCapability:
        return ModelCapability(
            chat=True,
            streaming=False,
            function_calling=True,
            vision=True,
            reasoning=True,
            max_context_tokens=4096,
            supported_languages=["en"],
        )
    
    async def initialize(self) -> None:
        """Initialize the vision provider."""
        raise NotImplementedError
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def _rate_limit(self) -> None:
        """Enforce rate limiting."""
        now = time.time()
        if now - self._rate_limit_window_start >= 60:
            self._request_count = 0
            self._rate_limit_window_start = now
        
        if self._request_count >= self.config.requests_per_minute:
            wait_time = 60 - (now - self._rate_limit_window_start)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self._request_count = 0
            self._rate_limit_window_start = time.time()
        
        # Minimum delay between requests
        time_since_last = now - self._last_request_time
        min_delay = 60.0 / self.config.requests_per_minute
        if time_since_last < min_delay:
            await asyncio.sleep(min_delay - time_since_last)
        
        self._last_request_time = time.time()
        self._request_count += 1
    
    async def _make_request_with_retry(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Dict[str, str],
    ) -> Optional[httpx.Response]:
        """Make HTTP request with retry logic."""
        for attempt in range(self.config.max_retries):
            await self._rate_limit()
            
            try:
                response = await self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
                )
                
                if response.status_code == 429:  # Rate limited
                    retry_after = int(response.headers.get("Retry-After", self.config.retry_delay_seconds * (attempt + 1)))
                    logger.warning(f"{self.provider_name} rate limited, waiting {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                
                if response.status_code >= 500:  # Server error
                    if attempt < self.config.max_retries - 1:
                        await asyncio.sleep(self.config.retry_delay_seconds * (attempt + 1))
                        continue
                
                return response
                
            except httpx.TimeoutException:
                logger.warning(f"{self.provider_name} request timeout (attempt {attempt + 1})")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay_seconds * (attempt + 1))
                    continue
            except Exception as e:
                logger.error(f"{self.provider_name} request error: {e}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay_seconds * (attempt + 1))
                    continue
        
        return None
    
    def _get_image_dimensions(self, screenshot_base64: str) -> tuple[int, int]:
        """Decode image dimensions from base64 payload safely."""
        try:
            from PIL import Image
            image_data = base64.b64decode(screenshot_base64)
            image = Image.open(io.BytesIO(image_data))
            return image.size
        except Exception:
            return (1920, 1080)

    def _normalize_bbox(
        self,
        bbox: Dict[str, Any],
        img_width: int,
        img_height: int,
        coordinate_space: str = "auto",
    ) -> Dict[str, int]:
        """Normalize bounding box dict to clamped image pixel coordinates."""
        if not isinstance(bbox, dict) or not bbox:
            return {}
        try:
            left = float(bbox.get("left", bbox.get("x", 0)))
            top = float(bbox.get("top", bbox.get("y", 0)))
            width = float(bbox.get("width", bbox.get("w", 0)))
            height = float(bbox.get("height", bbox.get("h", 0)))
            right = float(bbox.get("right", left + width)) if "right" in bbox else left + width
            bottom = float(bbox.get("bottom", top + height)) if "bottom" in bbox else top + height
        except (TypeError, ValueError):
            return {}

        if width <= 0 and right > left:
            width = right - left
        if height <= 0 and bottom > top:
            height = bottom - top

        vals = [left, top, right, bottom]
        is_unit_fraction = all(0.0 <= v <= 1.0 for v in vals) and (max(vals) <= 1.0)
        is_norm_1000 = coordinate_space == "normalized_1000"

        if is_unit_fraction:
            left *= img_width
            top *= img_height
            width *= img_width
            height *= img_height
        elif is_norm_1000:
            left = left * img_width / 1000.0
            top = top * img_height / 1000.0
            width = width * img_width / 1000.0
            height = height * img_height / 1000.0

        left_i = max(0, min(img_width - 1, int(round(left))))
        top_i = max(0, min(img_height - 1, int(round(top))))
        width_i = max(1, min(img_width - left_i, int(round(width))))
        height_i = max(1, min(img_height - top_i, int(round(height))))
        return {
            "left": left_i,
            "top": top_i,
            "width": width_i,
            "height": height_i,
            "right": left_i + width_i,
            "bottom": top_i + height_i,
        }

    def _normalize_elements(
        self,
        elements: List[Dict[str, Any]],
        img_width: int,
        img_height: int,
        coordinate_space: str = "auto",
    ) -> List[Dict[str, Any]]:
        """Normalize element bounding boxes in analysis output."""
        if not elements:
            return []
        normalized: List[Dict[str, Any]] = []
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            item = dict(elem)
            bbox = item.get("bbox")
            if isinstance(bbox, dict) and bbox:
                item["bbox"] = self._normalize_bbox(bbox, img_width, img_height, coordinate_space)
            normalized.append(item)
        return normalized

    def _normalize_action(
        self,
        parsed: Dict[str, Any],
        img_width: int,
        img_height: int,
    ) -> VisionAction:
        """Convert parsed model output into a VisionAction with clamped pixel coordinates."""
        action = VisionAction.from_dict(parsed)
        params = dict(action.params or {})
        coord_space = str(parsed.get("coordinate_space", "auto")).lower()

        if "x" in params and "y" in params:
            try:
                x = float(params["x"])
                y = float(params["y"])
                if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and (x < 1.0 or y < 1.0):
                    x *= img_width
                    y *= img_height
                elif coord_space == "normalized_1000":
                    x = x * img_width / 1000.0
                    y = y * img_height / 1000.0
                params["x"] = max(0, min(img_width - 1, int(round(x))))
                params["y"] = max(0, min(img_height - 1, int(round(y))))
            except (TypeError, ValueError):
                pass

        action.params = params
        return action

    def _build_analysis_prompt(self, task: str, img_width: int = 1920, img_height: int = 1080) -> str:
        """Build prompt for screen analysis."""
        return f"""You are a GUI automation expert. Analyze this screenshot ({img_width}x{img_height} pixels) and provide a structured JSON response.

Task context: {task}

IMPORTANT: Return bounding boxes in ACTUAL PIXEL coordinates of this image (width={img_width}, height={img_height}). Set "coordinate_space": "pixels".

Return ONLY valid JSON with this exact structure:
{{
  "coordinate_space": "pixels",
  "description": "Brief description of what's on screen",
  "elements": [
    {{"type": "button|input|text|link|menu|checkbox|radio|dropdown|tab|window|dialog|icon|image|unknown", "label": "visible text or aria-label", "description": "what this element does", "bbox": {{"left": x, "top": y, "width": w, "height": h}}, "confidence": 0.0-1.0}}
  ],
  "text_content": "all visible text concatenated",
  "suggested_actions": ["action 1", "action 2"],
  "app_type": "notepad|word|excel|chrome|edge|explorer|terminal|steam|discord|vscode|unknown"
}}"""
    
    def _build_action_prompt(
        self,
        task: str,
        previous_actions: List[str],
        img_width: int = 1920,
        img_height: int = 1080,
    ) -> str:
        """Build prompt for next action planning."""
        prev = "\n".join(previous_actions) if previous_actions else "None"
        return f"""You are a GUI automation agent. Given the current screen ({img_width}x{img_height} pixels) and task, decide the next action.

Task: {task}
Previous actions: {prev}

IMPORTANT: Return pixel coordinates relative to this image (0 <= x < {img_width}, 0 <= y < {img_height}) when specifying x,y.

Return ONLY valid JSON:
{{
  "coordinate_space": "pixels",
  "action": "click|double_click|right_click|type|hotkey|wait|scroll|navigate|complete|error",
  "target": "element description or visible label",
  "params": {{"text": "...", "keys": [...], "x": 0, "y": 0}},
  "reasoning": "why this action",
  "confidence": 0.0-1.0,
  "is_complete": true/false,
  "expected_result": "what should happen after this action",
  "screen_state": "current screen description"
}}"""
    
    def _build_verification_prompt(self, expected_change: str) -> str:
        """Build prompt for action verification."""
        return f"""You are a GUI automation expert. Compare these two screenshots and verify if the expected change occurred: "{expected_change}"

Return ONLY valid JSON:
{{
  "verified": true/false,
  "confidence": 0.0-1.0,
  "reasoning": "explanation of what changed",
  "before_elements_count": N,
  "after_elements_count": N
}}"""
    
    def _parse_vision_json(self, response_text: str) -> Dict[str, Any]:
        """Parse vision model response into structured data."""
        import re
        
        try:
            # Strategy 1: Find JSON in markdown code blocks
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                return json.loads(json_str)
            
            # Strategy 2: Find JSON object directly (greedy match)
            json_match = re.search(r'(\{.*\})', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    pass
            
            # Strategy 3: Try to extract JSON from common patterns
            # Look for description, elements, etc. patterns
            lines = response_text.strip().split('\n')
            json_lines = []
            in_json = False
            brace_count = 0
            for line in lines:
                if '{' in line and not in_json:
                    in_json = True
                if in_json:
                    json_lines.append(line)
                    brace_count += line.count('{') - line.count('}')
                    if brace_count <= 0 and in_json:
                        break
            
            if json_lines:
                json_str = '\n'.join(json_lines)
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    pass
            
            # If all parsing fails, return error
            return {"error": "Failed to parse response", "raw": response_text[:500]}
            
        except Exception as e:
            logger.warning(f"Failed to parse vision response as JSON: {e}")
            return {"error": "Failed to parse response", "raw": response_text[:500]}
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
    ) -> ScreenAnalysis:
        """Analyze a screenshot and return structured GUI understanding."""
        raise NotImplementedError
    
    async def get_next_action(
        self,
        screenshot_base64: str,
        current_task: str,
        previous_actions: List[str] = None,
    ) -> VisionAction:
        """Get the next recommended action for automation."""
        raise NotImplementedError
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
    ) -> Dict[str, Any]:
        """Verify that an action produced the expected visual change."""
        raise NotImplementedError

    async def find_elements(
        self,
        screenshot_base64: str,
        target_description: str,
        region: Optional[Dict[str, int]] = None,
    ) -> List[Dict[str, Any]]:
        """Locate UI elements matching target_description via screen analysis."""
        analysis = await self.analyze_screen(
            screenshot_base64,
            task_description=f"Locate the exact UI element matching: {target_description}",
            region=region,
        )
        elements = analysis.elements or []
        if not elements:
            return []

        query_terms = [w for w in target_description.lower().split() if len(w) > 2]
        scored: List[tuple[float, Dict[str, Any]]] = []
        for elem in elements:
            if not isinstance(elem, dict) or not elem.get("bbox"):
                continue
            label = str(elem.get("label", "")).lower()
            desc = str(elem.get("description", "")).lower()
            elem_type = str(elem.get("type", "")).lower()
            combined = f"{label} {desc} {elem_type}"
            term_hits = sum(1 for t in query_terms if t in combined)
            exact_bonus = 2.0 if target_description.lower() in combined else 0.0
            conf = float(elem.get("confidence", 0.5) or 0.5)
            score = exact_bonus + term_hits + conf
            scored.append((score, elem))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]
    
    # ModelProvider abstract methods - vision providers are specialized, not general chat
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Not supported for vision providers."""
        return CompletionResponse(
            id="vision-not-supported",
            model=self.config.nvidia_vision_model if hasattr(self.config, 'nvidia_vision_model') else "vision",
            content="Vision provider does not support direct chat completion. Use analyze_screen() or get_next_action() instead.",
            finish_reason="error",
        )
    
    async def stream(self, request: CompletionRequest):
        """Not supported for vision providers."""
        yield StreamChunk(
            id="vision-not-supported",
            content="Vision provider does not support streaming chat.",
            finish_reason="error",
            is_final=True,
        )
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Not supported for vision providers."""
        return EmbeddingResponse(
            object="list",
            data=[],
            model=self.config.nvidia_vision_model if hasattr(self.config, 'nvidia_vision_model') else "vision",
        )


class NVIDIANIMVisionProvider(BaseVisionProvider):
    """NVIDIA NIM/API Vision Provider using vision-capable models like NVLM."""
    
    @property
    def provider_type_name(self) -> VisionProviderType:
        return VisionProviderType.NVIDIA_NIM
    
    async def initialize(self) -> None:
        """Initialize the NVIDIA vision provider."""
        if self._initialized:
            return
        
        # Use NVIDIA API key from config or settings
        api_key = self.config.nvidia_api_key or settings.nvidia_api_key
        if not api_key:
            logger.warning("NVIDIA API key not configured - NVIDIA vision provider disabled")
            self._initialized = False
            return
        
        base_url = self.config.nvidia_base_url or settings.nvidia_base_url
        if not base_url.endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        
        try:
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            )
            self._initialized = True
            logger.info(f"NVIDIA vision provider configured with model: {self.config.nvidia_vision_model}")
                
        except Exception as e:
            logger.error(f"Failed to initialize NVIDIA vision provider: {e}")
            self._initialized = False
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self._initialized:
            return ProviderHealth(
                healthy=False,
                error="NVIDIA API key not configured or initialization failed",
                model_count=0,
            )
        
        try:
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": self.config.nvidia_vision_model,
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
            id=self.config.nvidia_vision_model,
            name=f"NVIDIA Vision ({self.config.nvidia_vision_model})",
            provider=self.provider_id,
            capabilities=self.capabilities,
            context_window=4096,
        )]
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
    ) -> ScreenAnalysis:
        """Analyze a screenshot using NVIDIA vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return ScreenAnalysis(
                description="Cannot analyze screen: NVIDIA vision provider not available",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="nvidia_nim",
                screenshot_dimensions={},
            )
        
        try:
            img_width, img_height = self._get_image_dimensions(screenshot_base64)
            prompt = self._build_analysis_prompt(task_description, img_width, img_height)
            
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
            
            response = await self._make_request_with_retry(
                "/chat/completions",
                {
                    "model": self.config.nvidia_vision_model,
                    "messages": messages,
                    "max_tokens": 8192,
                    "temperature": 0.1,
                },
                self._client.headers,
            )
            
            if response and response.status_code == 200:
                data = response.json()
                message = data["choices"][0]["message"]
                # Handle reasoning models where content is in reasoning_content
                content = message.get("content") or message.get("reasoning_content") or ""
                parsed = self._parse_vision_json(content)
                
                if "error" not in parsed:
                    coord_space = str(parsed.get("coordinate_space", "auto")).lower()
                    elements = self._normalize_elements(
                        parsed.get("elements", []),
                        img_width,
                        img_height,
                        coord_space,
                    )
                    
                    return ScreenAnalysis(
                        description=parsed.get("description", "No description"),
                        elements=elements,
                        text_content=parsed.get("text_content", ""),
                        suggested_actions=parsed.get("suggested_actions", []),
                        app_type=parsed.get("app_type", "unknown"),
                        model_type="nvidia_nim",
                        screenshot_dimensions={"width": img_width, "height": img_height},
                    )
            
            return ScreenAnalysis(
                description=f"Analysis failed: {response.text if response else 'No response'}",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="nvidia_nim",
                screenshot_dimensions={},
            )
                
        except Exception as e:
            logger.error(f"NVIDIA screen analysis failed: {e}")
            return ScreenAnalysis(
                description=f"Error: {str(e)}",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="nvidia_nim",
                screenshot_dimensions={},
            )
    
    async def get_next_action(
        self,
        screenshot_base64: str,
        current_task: str,
        previous_actions: List[str] = None,
    ) -> VisionAction:
        """Get next action from NVIDIA vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning="NVIDIA vision provider not initialized",
                confidence=0.0,
            )
        
        try:
            img_width, img_height = self._get_image_dimensions(screenshot_base64)
            prompt = self._build_action_prompt(current_task, previous_actions or [], img_width, img_height)
            
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
            
            response = await self._make_request_with_retry(
                "/chat/completions",
                {
                    "model": self.config.nvidia_vision_model,
                    "messages": messages,
                    "max_tokens": 2048,
                    "temperature": 0.1,
                },
                self._client.headers,
            )
            
            if response and response.status_code == 200:
                data = response.json()
                message = data["choices"][0]["message"]
                # Handle reasoning models where content is in reasoning_content
                content = message.get("content") or message.get("reasoning_content") or ""
                parsed = self._parse_vision_json(content)
                
                if "error" not in parsed:
                    return self._normalize_action(parsed, img_width, img_height)
            
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning=f"API error: {response.text if response else 'No response'}",
                confidence=0.0,
            )
                
        except Exception as e:
            logger.error(f"NVIDIA next action prediction failed: {e}")
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning=str(e),
                confidence=0.0,
            )
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
    ) -> Dict[str, Any]:
        """Verify action using NVIDIA vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"verified": False, "confidence": 0.0, "error": "NVIDIA vision provider not initialized"}
        
        try:
            prompt = self._build_verification_prompt(expected_change)
            
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
            
            response = await self._make_request_with_retry(
                "/chat/completions",
                {
                    "model": self.config.nvidia_vision_model,
                    "messages": messages,
                    "max_tokens": 1024,
                    "temperature": 0.1,
                },
                self._client.headers,
            )
            
            if response and response.status_code == 200:
                data = response.json()
                message = data["choices"][0]["message"]
                content = message.get("content") or message.get("reasoning_content") or ""
                parsed = self._parse_vision_json(content)
                return parsed
            
            return {"verified": False, "confidence": 0.0, "error": f"API error: {response.text if response else 'No response'}"}
                
        except Exception as e:
            logger.error(f"NVIDIA action verification failed: {e}")
            return {"verified": False, "confidence": 0.0, "error": str(e)}


class GeminiVisionProvider(BaseVisionProvider):
    """Google Gemini Vision Provider for image understanding."""
    
    @property
    def provider_type_name(self) -> VisionProviderType:
        return VisionProviderType.GEMINI
    
    async def initialize(self) -> None:
        """Initialize the Gemini vision provider."""
        if self._initialized:
            return
        
        api_key = self.config.gemini_api_key or settings.gemini_api_key
        if not api_key:
            logger.warning("Gemini API key not configured - Gemini vision provider disabled")
            self._initialized = False
            return
        
        base_url = self.config.gemini_base_url or settings.gemini_base_url
        if not base_url.endswith("/v1beta"):
            base_url = base_url.rstrip("/") + "/v1beta"
        
        try:
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            )
            self._initialized = True
            logger.info(f"Gemini vision provider configured with model: {self.config.gemini_vision_model}")
                
        except Exception as e:
            logger.error(f"Failed to initialize Gemini vision provider: {e}")
            self._initialized = False
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self._initialized:
            return ProviderHealth(
                healthy=False,
                error="Gemini API key not configured or initialization failed",
                model_count=0,
            )
        
        try:
            response = await self._client.post(
                f"/models/{self.config.gemini_vision_model}:generateContent",
                json={
                    "contents": [{"parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 5},
                }
            )
            
            if response.status_code == 200:
                return ProviderHealth(
                    healthy=True,
                    latency_ms=150.0,
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
            id=self.config.gemini_vision_model,
            name=f"Gemini Vision ({self.config.gemini_vision_model})",
            provider=self.provider_id,
            capabilities=self.capabilities,
            context_window=4096,
        )]
    
    def _gemini_payload(self, prompt: str, image_base64: str, max_tokens: int = 4096) -> Dict[str, Any]:
        """Build Gemini API payload with image."""
        return {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": image_base64
                        }
                    }
                ]
            }],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.1,
            }
        }
    
    async def _gemini_request(self, endpoint: str, payload: Dict[str, Any]) -> Optional[httpx.Response]:
        """Make request to Gemini API."""
        return await self._make_request_with_retry(endpoint, payload, self._client.headers)
    
    def _parse_gemini_response(self, response: httpx.Response) -> Dict[str, Any]:
        """Parse Gemini response."""
        try:
            data = response.json()
            candidates = data.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                if parts:
                    text = parts[0].get("text", "")
                    return self._parse_vision_json(text)
            return {"error": "No content in response"}
        except Exception as e:
            logger.error(f"Failed to parse Gemini response: {e}")
            return {"error": str(e)}
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
    ) -> ScreenAnalysis:
        """Analyze a screenshot using Gemini vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return ScreenAnalysis(
                description="Cannot analyze screen: Gemini vision provider not available",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="gemini",
                screenshot_dimensions={},
            )
        
        try:
            img_width, img_height = self._get_image_dimensions(screenshot_base64)
            prompt = self._build_analysis_prompt(task_description, img_width, img_height)
            payload = self._gemini_payload(prompt, screenshot_base64, max_tokens=8192)
            
            response = await self._gemini_request(
                f"/models/{self.config.gemini_vision_model}:generateContent",
                payload,
            )
            
            if response and response.status_code == 200:
                parsed = self._parse_gemini_response(response)
                
                if "error" not in parsed:
                    coord_space = str(parsed.get("coordinate_space", "auto")).lower()
                    elements = self._normalize_elements(
                        parsed.get("elements", []),
                        img_width,
                        img_height,
                        coord_space,
                    )
                    return ScreenAnalysis(
                        description=parsed.get("description", "No description"),
                        elements=elements,
                        text_content=parsed.get("text_content", ""),
                        suggested_actions=parsed.get("suggested_actions", []),
                        app_type=parsed.get("app_type", "unknown"),
                        model_type="gemini",
                        screenshot_dimensions={"width": img_width, "height": img_height},
                    )
            
            return ScreenAnalysis(
                description=f"Analysis failed: {response.text if response else 'No response'}",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="gemini",
                screenshot_dimensions={},
            )
                
        except Exception as e:
            logger.error(f"Gemini screen analysis failed: {e}")
            return ScreenAnalysis(
                description=f"Error: {str(e)}",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="gemini",
                screenshot_dimensions={},
            )
    
    async def get_next_action(
        self,
        screenshot_base64: str,
        current_task: str,
        previous_actions: List[str] = None,
    ) -> VisionAction:
        """Get next action from Gemini vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning="Gemini vision provider not initialized",
                confidence=0.0,
            )
        
        try:
            img_width, img_height = self._get_image_dimensions(screenshot_base64)
            prompt = self._build_action_prompt(current_task, previous_actions or [], img_width, img_height)
            payload = self._gemini_payload(prompt, screenshot_base64, max_tokens=2048)
            
            response = await self._gemini_request(
                f"/models/{self.config.gemini_vision_model}:generateContent",
                payload,
            )
            
            if response and response.status_code == 200:
                parsed = self._parse_gemini_response(response)
                
                if "error" not in parsed:
                    return self._normalize_action(parsed, img_width, img_height)
            
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning=f"API error: {response.text if response else 'No response'}",
                confidence=0.0,
            )
                
        except Exception as e:
            logger.error(f"Gemini next action prediction failed: {e}")
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning=str(e),
                confidence=0.0,
            )
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
    ) -> Dict[str, Any]:
        """Verify action using Gemini vision model."""
        if not self._initialized:
            await self.initialize()
        
        if not self._initialized:
            return {"verified": False, "confidence": 0.0, "error": "Gemini vision provider not initialized"}
        
        try:
            prompt = self._build_verification_prompt(expected_change)
            
            # For verification, we need to send both images
            # Gemini supports multiple inline_data parts
            payload = {
                "contents": [{
                    "parts": [
                        {"text": f"BEFORE screenshot: {prompt}"},
                        {"inline_data": {"mime_type": "image/png", "data": before_screenshot}},
                        {"text": "AFTER screenshot:"},
                        {"inline_data": {"mime_type": "image/png", "data": after_screenshot}},
                    ]
                }],
                "generationConfig": {
                    "maxOutputTokens": 1024,
                    "temperature": 0.1,
                }
            }
            
            response = await self._gemini_request(
                f"/models/{self.config.gemini_vision_model}:generateContent",
                payload,
            )
            
            if response and response.status_code == 200:
                parsed = self._parse_gemini_response(response)
                return parsed
            
            return {"verified": False, "confidence": 0.0, "error": f"API error: {response.text if response else 'No response'}"}
                
        except Exception as e:
            logger.error(f"Gemini action verification failed: {e}")
            return {"verified": False, "confidence": 0.0, "error": str(e)}


class VisionProviderFactory:
    """Factory for creating and managing vision providers with fallback."""
    
    def __init__(self, config: Optional[VisionConfig] = None):
        self.config = config or VisionConfig()
        self._providers: Dict[VisionProviderType, BaseVisionProvider] = {}
        self._current_provider: Optional[BaseVisionProvider] = None
        self._initialized = False
    
    def _create_provider(self, provider_type: VisionProviderType) -> BaseVisionProvider:
        """Create a vision provider instance."""
        if provider_type == VisionProviderType.NVIDIA_NIM:
            return NVIDIANIMVisionProvider(self.config)
        elif provider_type == VisionProviderType.GEMINI:
            return GeminiVisionProvider(self.config)
        else:
            raise ValueError(f"Unknown vision provider type: {provider_type}")
    
    async def initialize(self) -> None:
        """Initialize all providers and select the best available one."""
        if self._initialized:
            return
        
        # Create providers in priority order
        provider_order = [self.config.primary_provider] + self.config.fallback_providers
        seen = set()
        
        for ptype in provider_order:
            if ptype in seen:
                continue
            seen.add(ptype)
            
            provider = self._create_provider(ptype)
            self._providers[ptype] = provider
            await provider.initialize()
            
            if provider._initialized:
                if self._current_provider is None:
                    self._current_provider = provider
                    logger.info(f"Selected vision provider: {provider.provider_name}")
        
        if self._current_provider is None:
            logger.warning("No vision providers available - visual intelligence disabled")
        else:
            logger.info(f"Active vision provider: {self._current_provider.provider_name}")
        
        self._initialized = True
    
    async def get_provider(self, provider_type: Optional[VisionProviderType] = None) -> Optional[BaseVisionProvider]:
        """Get a specific provider or the current active one."""
        if not self._initialized:
            await self.initialize()
        
        if provider_type:
            return self._providers.get(provider_type)
        
        return self._current_provider
    
    async def switch_provider(self, provider_type: VisionProviderType) -> bool:
        """Switch to a different vision provider."""
        if provider_type not in self._providers:
            return False
        
        provider = self._providers[provider_type]
        if not provider._initialized:
            await provider.initialize()
        
        if provider._initialized:
            self._current_provider = provider
            logger.info(f"Switched to vision provider: {provider.provider_name}")
            return True
        
        return False
    
    async def analyze_screen(
        self,
        screenshot_base64: str,
        task_description: str = "",
        region: Optional[Dict[str, int]] = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> ScreenAnalysis:
        """Analyze screen using current or specified provider with automatic fallback."""
        if not self._initialized:
            await self.initialize()
        
        if self._current_provider is None:
            return ScreenAnalysis(
                description="No vision providers available",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="none",
                screenshot_dimensions={},
            )
        
        # Try specified provider or current provider
        providers_to_try = []
        if provider_type and provider_type in self._providers:
            providers_to_try.append(self._providers[provider_type])
        
        if self._current_provider and self._current_provider not in providers_to_try:
            providers_to_try.append(self._current_provider)
        
        # Add fallback providers
        for ptype in self.config.fallback_providers:
            if ptype in self._providers and self._providers[ptype] not in providers_to_try:
                providers_to_try.append(self._providers[ptype])
        
        last_error = None
        for provider in providers_to_try:
            try:
                result = await provider.analyze_screen(screenshot_base64, task_description, region)
                desc_lower = (result.description or "").lower()
                is_failure = (
                    not result
                    or desc_lower.startswith("analysis failed:")
                    or desc_lower.startswith("cannot analyze screen:")
                    or desc_lower.startswith("error:")
                )
                if not is_failure:
                    if provider != self._current_provider:
                        self._current_provider = provider
                        logger.info(f"Fallback to vision provider: {provider.provider_name}")
                    return result
                last_error = result.description if result else "Empty result"
            except Exception as e:
                last_error = e
                logger.warning(f"Vision provider {provider.provider_name} failed: {e}")
        
        return ScreenAnalysis(
            description=f"All vision providers failed: {last_error}",
            elements=[],
            text_content="",
            suggested_actions=[],
            app_type="unknown",
            model_type="none",
            screenshot_dimensions={},
        )
    
    async def get_next_action(
        self,
        screenshot_base64: str,
        current_task: str,
        previous_actions: List[str] = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> VisionAction:
        """Get next action using current or specified provider with fallback."""
        if not self._initialized:
            await self.initialize()
        
        if self._current_provider is None:
            return VisionAction(
                action="error",
                target="",
                params={},
                reasoning="No vision providers available",
                confidence=0.0,
            )
        
        providers_to_try = []
        if provider_type and provider_type in self._providers:
            providers_to_try.append(self._providers[provider_type])
        
        if self._current_provider and self._current_provider not in providers_to_try:
            providers_to_try.append(self._current_provider)
        
        for ptype in self.config.fallback_providers:
            if ptype in self._providers and self._providers[ptype] not in providers_to_try:
                providers_to_try.append(self._providers[ptype])
        
        last_error = None
        for provider in providers_to_try:
            try:
                result = await provider.get_next_action(screenshot_base64, current_task, previous_actions)
                if result and result.action != "error":
                    if provider != self._current_provider:
                        self._current_provider = provider
                    return result
            except Exception as e:
                last_error = e
                logger.warning(f"Vision provider {provider.provider_name} action failed: {e}")
        
        return VisionAction(
            action="error",
            target="",
            params={},
            reasoning=f"All vision providers failed: {last_error}",
            confidence=0.0,
        )
    
    async def verify_action(
        self,
        before_screenshot: str,
        after_screenshot: str,
        expected_change: str,
        provider_type: Optional[VisionProviderType] = None,
    ) -> Dict[str, Any]:
        """Verify action using current or specified provider with fallback."""
        if not self._initialized:
            await self.initialize()
        
        if self._current_provider is None:
            return {"verified": False, "confidence": 0.0, "error": "No vision providers available"}
        
        providers_to_try = []
        if provider_type and provider_type in self._providers:
            providers_to_try.append(self._providers[provider_type])
        
        if self._current_provider and self._current_provider not in providers_to_try:
            providers_to_try.append(self._current_provider)
        
        for ptype in self.config.fallback_providers:
            if ptype in self._providers and self._providers[ptype] not in providers_to_try:
                providers_to_try.append(self._providers[ptype])
        
        for provider in providers_to_try:
            try:
                result = await provider.verify_action(before_screenshot, after_screenshot, expected_change)
                if result and result.get("verified") is not None:
                    return result
            except Exception as e:
                logger.warning(f"Vision provider {provider.provider_name} verification failed: {e}")
        
        return {"verified": False, "confidence": 0.0, "error": "All vision providers failed"}

    async def find_elements(
        self,
        screenshot_base64: str,
        target_description: str,
        region: Optional[Dict[str, int]] = None,
        provider_type: Optional[VisionProviderType] = None,
    ) -> List[Dict[str, Any]]:
        """Locate elements matching description with automatic provider fallback."""
        analysis = await self.analyze_screen(
            screenshot_base64,
            task_description=f"Locate the exact UI element matching: {target_description}",
            region=region,
            provider_type=provider_type,
        )
        elements = analysis.elements or []
        if not elements:
            return []
        query_terms = [w for w in target_description.lower().split() if len(w) > 2]
        scored = []
        for elem in elements:
            if not isinstance(elem, dict) or not elem.get("bbox"):
                continue
            combined = f"{elem.get('label', '')} {elem.get('description', '')} {elem.get('type', '')}".lower()
            term_hits = sum(1 for t in query_terms if t in combined)
            exact_bonus = 2.0 if target_description.lower() in combined else 0.0
            conf = float(elem.get("confidence", 0.5) or 0.5)
            scored.append((exact_bonus + term_hits + conf, elem))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored]

    async def close(self) -> None:
        """Close all initialized vision provider clients."""
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception as e:
                logger.warning(f"Error closing vision provider {provider.provider_name}: {e}")


# Global factory instance
_vision_factory: Optional[VisionProviderFactory] = None


async def get_vision_factory(config: Optional[VisionConfig] = None) -> VisionProviderFactory:
    """Get or create the global vision provider factory."""
    global _vision_factory
    if _vision_factory is None:
        _vision_factory = VisionProviderFactory(config)
        await _vision_factory.initialize()
    return _vision_factory


async def close_vision_factory() -> None:
    """Close and reset the global vision provider factory if initialized."""
    global _vision_factory
    if _vision_factory is not None:
        await _vision_factory.close()
        _vision_factory = None


# Export
__all__ = [
    "VisionProviderType",
    "VisionConfig",
    "VisionAction",
    "ScreenAnalysis",
    "BaseVisionProvider",
    "NVIDIANIMVisionProvider",
    "GeminiVisionProvider",
    "VisionProviderFactory",
    "get_vision_factory",
    "close_vision_factory",
]