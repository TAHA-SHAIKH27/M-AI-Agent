"""NVIDIA Nemotron 3 Ultra provider implementation."""
import httpx
import time
import json
import msgspec
import structlog
from typing import AsyncIterator, List, Optional, Dict, Any

from .base import ModelProvider
from .schemas import (
    CompletionRequest, CompletionResponse, StreamChunk,
    ModelInfo, ModelCapability, EmbeddingRequest, EmbeddingResponse,
    ProviderHealth, ToolSchema, ChatMessage, FinishReason,
)
from ..config import settings
from ..core.exceptions import (
    ProviderError, ProviderUnavailableError, ProviderAuthenticationError,
    ProviderRateLimitError, ProviderTimeoutError,
)

logger = structlog.get_logger(__name__)


class NVIDIANemotronProvider(ModelProvider):
    """NVIDIA Nemotron 3 Ultra provider via OpenAI-compatible API."""
    
    id = "nvidia-nemotron-3-ultra"
    name = "NVIDIA Nemotron 3 Ultra"
    type = "cloud"
    
    capabilities = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=True,
        vision=False,  # Nemotron 3 Ultra is text-only
        reasoning=True,
        max_context_tokens=1_000_000,
        supported_languages=["en", "multi"],
    )
    
    limits = {
        "max_tokens_per_request": 16384,
        "max_concurrent_requests": 10,
        "rate_limit_rpm": 60,
    }
    
    pricing = {
        "input_per_1m": 0.50,
        "output_per_1m": 2.20,
    }
    
    def __init__(self):
        super().__init__()
        self.api_key = settings.nvidia_api_key
        # Handle both https://integrate.api.nvidia.com and https://integrate.api.nvidia.com/v1
        base = settings.nvidia_base_url.rstrip("/")
        if base.endswith("/v1"):
            self.base_url = base
        else:
            self.base_url = base + "/v1"
        # Try model without nvidia/ prefix if it has one
        model = settings.nvidia_model
        if model.startswith("nvidia/"):
            self.default_model = model
        else:
            self.default_model = "nvidia/" + model
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self.api_key:
                raise ProviderAuthenticationError(
                    "NVIDIA API key not configured",
                    provider=self.id,
                )
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(120.0, connect=10.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def get_default_model(self) -> str:
        """Return the actual model name for API calls."""
        return self.default_model
    
    def _build_payload(self, request: CompletionRequest, stream: bool) -> Dict[str, Any]:
        # Convert msgspec Struct objects to dicts using msgspec.to_builtins
        messages = [msgspec.to_builtins(msg) for msg in request.messages]
        
        payload = {
            "model": request.model or self.default_model,
            "messages": messages,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        
        if request.stop:
            payload["stop"] = request.stop
        
        if request.presence_penalty != 0:
            payload["presence_penalty"] = request.presence_penalty
        
        if request.frequency_penalty != 0:
            payload["frequency_penalty"] = request.frequency_penalty
        
        # Tool calling support
        if request.tools and self.capabilities.function_calling:
            payload["tools"] = [msgspec.to_builtins(tool) for tool in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice
        
        # Nemotron-specific: enable thinking mode via model parameter
        # Note: NVIDIA API may not support extra_body, so we skip it
        
        return payload
    
    def _parse_message(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Parse message from API response."""
        return {
            "role": msg.get("role", "assistant"),
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": msg.get("tool_calls"),
        }
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Non-streaming completion."""
        payload = self._build_payload(request, stream=False)
        
        start = time.time()
        try:
            logger.debug(f"NVIDIA API request to {self.base_url}/chat/completions", model=payload.get("model"))
            response = await self.client.post("/chat/completions", json=payload)
            latency = (time.time() - start) * 1000
            
            logger.debug(f"NVIDIA API response: {response.status_code}")
            
            if response.status_code == 401:
                raise ProviderAuthenticationError(
                    "Invalid NVIDIA API key",
                    provider=self.id,
                )
            elif response.status_code == 403:
                raise ProviderAuthenticationError(
                    "NVIDIA API access forbidden. Ensure 'Public API Endpoints' entitlement is enabled for your account at https://build.nvidia.com",
                    provider=self.id,
                )
            elif response.status_code == 400:
                error_text = response.text
                logger.error(f"NVIDIA API 400 error: {error_text}")
                raise ProviderError(
                    f"NVIDIA API bad request (400): {error_text}",
                    provider=self.id,
                )
            elif response.status_code == 429:
                raise ProviderRateLimitError(
                    "NVIDIA API rate limit exceeded",
                    provider=self.id,
                )
            elif response.status_code >= 500:
                raise ProviderUnavailableError(
                    f"NVIDIA API error: {response.status_code}",
                    provider=self.id,
                )
            
            response.raise_for_status()
            data = response.json()
            
            return self._parse_completion(data, latency)
            
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(
                f"NVIDIA API request timed out: {e}",
                provider=self.id,
            )
        except httpx.HTTPError as e:
            raise ProviderError(
                f"NVIDIA API HTTP error: {e}",
                provider=self.id,
            )
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Streaming completion."""
        payload = self._build_payload(request, stream=True)
        
        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code == 401:
                    raise ProviderAuthenticationError(
                        "Invalid NVIDIA API key",
                        provider=self.id,
                    )
                elif response.status_code == 429:
                    raise ProviderRateLimitError(
                        "NVIDIA API rate limit exceeded",
                        provider=self.id,
                    )
                elif response.status_code >= 500:
                    raise ProviderUnavailableError(
                        f"NVIDIA API error: {response.status_code}",
                        provider=self.id,
                    )
                
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if line.strip() == "data: [DONE]":
                        break
                    
                    try:
                        chunk_data = json.loads(line[6:])
                        yield self._parse_stream_chunk(chunk_data)
                    except json.JSONDecodeError:
                        continue
                        
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(
                f"NVIDIA API stream timed out: {e}",
                provider=self.id,
            )
        except httpx.HTTPError as e:
            raise ProviderError(
                f"NVIDIA API HTTP error: {e}",
                provider=self.id,
            )
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Nemotron 3 Ultra doesn't support embeddings."""
        raise NotImplementedError(
            "Nemotron 3 Ultra doesn't support embeddings. Use sentence-transformers or another provider."
        )
    
    async def list_models(self) -> List[ModelInfo]:
        """List available models."""
        return [
            ModelInfo(
                id=self.default_model,
                name="Nemotron 3 Ultra",
                provider=self.id,
                capabilities=self.capabilities,
                context_window=self.capabilities.max_context_tokens,
            )
        ]
    
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        if not self.api_key:
            return ProviderHealth(
                healthy=False,
                error="No API key configured",
            )
        
        try:
            start = time.time()
            response = await self.client.get("/models")
            latency = (time.time() - start) * 1000
            
            if response.status_code == 200:
                models = response.json().get("data", [])
                return ProviderHealth(
                    healthy=True,
                    latency_ms=latency,
                    model_count=len(models),
                )
            elif response.status_code == 401:
                return ProviderHealth(
                    healthy=False,
                    error="Invalid API key",
                )
            else:
                return ProviderHealth(
                    healthy=False,
                    error=f"HTTP {response.status_code}",
                )
        except httpx.TimeoutException:
            return ProviderHealth(
                healthy=False,
                error="Health check timed out",
            )
        except Exception as e:
            return ProviderHealth(
                healthy=False,
                error=str(e),
            )
    
    def _parse_completion(self, data: Dict[str, Any], latency_ms: float) -> CompletionResponse:
        """Parse completion response."""
        choice = data["choices"][0]
        message = choice["message"]
        
        return self._create_completion_response(
            id=data["id"],
            model=data["model"],
            content=message.get("content"),
            reasoning_content=message.get("reasoning_content"),
            tool_calls=message.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            latency_ms=latency_ms,
        )
    
    def _parse_stream_chunk(self, data: Dict[str, Any]) -> StreamChunk:
        """Parse stream chunk."""
        choice = data["choices"][0]
        delta = choice.get("delta", {})
        
        return self._create_stream_chunk(
            id=data["id"],
            content=delta.get("content"),
            reasoning_content=delta.get("reasoning_content"),
            tool_calls=delta.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            is_final=choice.get("finish_reason") is not None,
        )