"""Anthropic provider implementation."""
import httpx
import time
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


class AnthropicProvider(ModelProvider):
    """Anthropic Claude provider."""
    
    id = "anthropic"
    name = "Anthropic"
    type = "cloud"
    
    capabilities = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=True,
        vision=True,
        reasoning=False,
        max_context_tokens=200000,
        supported_languages=["en", "multi"],
    )
    
    limits = {
        "max_tokens_per_request": 8192,
        "max_concurrent_requests": 10,
    }
    
    pricing = {
        "claude-3-5-sonnet-20241022": {"input_per_1m": 3.00, "output_per_1m": 15.00},
        "claude-3-haiku-20240307": {"input_per_1m": 0.25, "output_per_1m": 1.25},
    }
    
    def __init__(self):
        super().__init__()
        self.api_key = settings.anthropic_api_key
        self.base_url = settings.anthropic_base_url.rstrip("/")
        self.default_model = settings.anthropic_model
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self.api_key:
                raise ProviderAuthenticationError(
                    "Anthropic API key not configured",
                    provider=self.id,
                )
            
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        return self._client
    
    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _convert_messages(self, messages: List[ChatMessage]) -> tuple:
        """Convert messages to Anthropic format (system + user/assistant)."""
        system = None
        anthropic_messages = []
        
        for msg in messages:
            if msg.role.value == "system":
                system = msg.content
            else:
                anthropic_messages.append({
                    "role": msg.role.value,
                    "content": msg.content or "",
                })
        
        return system, anthropic_messages
    
    def _build_payload(self, request: CompletionRequest, stream: bool) -> Dict[str, Any]:
        system, messages = self._convert_messages(request.messages)
        
        payload = {
            "model": request.model or self.default_model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stream": stream,
        }
        
        if system:
            payload["system"] = system
        
        if request.stop:
            payload["stop_sequences"] = request.stop
        
        # Tool calling
        if request.tools:
            payload["tools"] = [msgspec.to_builtins(tool) for tool in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice
        
        return payload
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = self._build_payload(request, stream=False)
        
        start = time.time()
        try:
            response = await self.client.post("/messages", json=payload)
            latency = (time.time() - start) * 1000
            
            if response.status_code == 401:
                raise ProviderAuthenticationError("Invalid Anthropic API key", provider=self.id)
            elif response.status_code == 429:
                raise ProviderRateLimitError("Anthropic rate limit exceeded", provider=self.id)
            elif response.status_code >= 500:
                raise ProviderUnavailableError(f"Anthropic error: {response.status_code}", provider=self.id)
            
            response.raise_for_status()
            data = response.json()
            
            return self._parse_completion(data, latency)
            
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Anthropic request timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic HTTP error: {e}", provider=self.id)
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request, stream=True)
        
        try:
            async with self.client.stream("POST", "/messages", json=payload) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if line.strip() == "data: [DONE]":
                        break
                    
                    try:
                        import json
                        chunk_data = json.loads(line[6:])
                        yield self._parse_stream_chunk(chunk_data)
                    except json.JSONDecodeError:
                        continue
                        
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Anthropic stream timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"Anthropic HTTP error: {e}", provider=self.id)
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        raise NotImplementedError("Anthropic doesn't provide embeddings API")
    
    async def list_models(self) -> List[ModelInfo]:
        return [ModelInfo(
            id=self.default_model,
            name=self.default_model,
            provider=self.id,
            capabilities=self.capabilities,
            context_window=self.capabilities.max_context_tokens,
        )]
    
    async def health_check(self) -> ProviderHealth:
        if not self.api_key:
            return ProviderHealth(healthy=False, error="No API key configured")
        
        try:
            # Anthropic doesn't have a models endpoint, so we test with a minimal request
            start = time.time()
            response = await self.client.post(
                "/messages",
                json={
                    "model": self.default_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "test"}],
                }
            )
            latency = (time.time() - start) * 1000
            
            if response.status_code in (200, 400):  # 400 = bad request but auth worked
                return ProviderHealth(healthy=True, latency_ms=latency, model_count=1)
            elif response.status_code == 401:
                return ProviderHealth(healthy=False, error="Invalid API key")
            return ProviderHealth(healthy=False, error=f"HTTP {response.status_code}")
        except Exception as e:
            return ProviderHealth(healthy=False, error=str(e))
    
    def _parse_completion(self, data: Dict[str, Any], latency_ms: float) -> CompletionResponse:
        content = ""
        tool_calls = None
        
        for block in data.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": block["input"],
                    },
                })
        
        return self._create_completion_response(
            id=data["id"],
            model=data["model"],
            content=content if content else None,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason"),
            usage={
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0),
            },
            latency_ms=latency_ms,
        )
    
    def _parse_stream_chunk(self, data: Dict[str, Any]) -> StreamChunk:
        chunk_type = data.get("type")
        
        if chunk_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                return self._create_stream_chunk(
                    id=data.get("id", ""),
                    content=delta.get("text"),
                    is_final=False,
                )
        elif chunk_type == "message_delta":
            stop_reason = data.get("delta", {}).get("stop_reason")
            if stop_reason:
                return self._create_stream_chunk(
                    id=data.get("id", ""),
                    finish_reason=stop_reason,
                    is_final=True,
                )
        
        return self._create_stream_chunk(id=data.get("id", ""), is_final=False)