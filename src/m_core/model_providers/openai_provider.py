"""OpenAI/Azure OpenAI provider implementation."""
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


class OpenAIProvider(ModelProvider):
    """OpenAI / Azure OpenAI provider."""
    
    id = "openai"
    name = "OpenAI"
    type = "cloud"
    
    capabilities = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=True,
        vision=True,
        reasoning=False,
        max_context_tokens=128000,
        supported_languages=["en", "multi"],
    )
    
    limits = {
        "max_tokens_per_request": 4096,
        "max_concurrent_requests": 20,
    }
    
    pricing = {
        "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
        "gpt-4o": {"input_per_1m": 5.00, "output_per_1m": 15.00},
    }
    
    def __init__(self):
        super().__init__()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = settings.openai_model
        self._client: Optional[httpx.AsyncClient] = None
        self._is_azure = "azure" in self.base_url.lower()
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self.api_key:
                raise ProviderAuthenticationError(
                    "OpenAI API key not configured",
                    provider=self.id,
                )
            
            headers = {"Content-Type": "application/json"}
            if self._is_azure:
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
        return self._client
    
    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _build_payload(self, request: CompletionRequest, stream: bool) -> Dict[str, Any]:
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
        if request.tools:
            payload["tools"] = [msgspec.to_builtins(tool) for tool in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice
        
        return payload
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = self._build_payload(request, stream=False)
        
        start = time.time()
        try:
            endpoint = "/chat/completions" if not self._is_azure else "/openai/deployments/{model}/chat/completions?api-version=2024-02-15-preview"
            endpoint = endpoint.format(model=request.model or self.default_model)
            
            response = await self.client.post(endpoint, json=payload)
            latency = (time.time() - start) * 1000
            
            if response.status_code == 401:
                raise ProviderAuthenticationError("Invalid OpenAI API key", provider=self.id)
            elif response.status_code == 429:
                raise ProviderRateLimitError("OpenAI rate limit exceeded", provider=self.id)
            elif response.status_code >= 500:
                raise ProviderUnavailableError(f"OpenAI error: {response.status_code}", provider=self.id)
            
            response.raise_for_status()
            data = response.json()
            
            return self._parse_completion(data, latency)
            
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"OpenAI request timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI HTTP error: {e}", provider=self.id)
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request, stream=True)
        
        try:
            endpoint = "/chat/completions" if not self._is_azure else "/openai/deployments/{model}/chat/completions?api-version=2024-02-15-preview"
            endpoint = endpoint.format(model=request.model or self.default_model)
            
            async with self.client.stream("POST", endpoint, json=payload) as response:
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
            raise ProviderTimeoutError(f"OpenAI stream timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"OpenAI HTTP error: {e}", provider=self.id)
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        try:
            endpoint = "/embeddings" if not self._is_azure else "/openai/deployments/{model}/embeddings?api-version=2024-02-15-preview"
            endpoint = endpoint.format(model=request.model or self.default_model)
            
            response = await self.client.post(
                endpoint,
                json={"model": request.model or self.default_model, "input": request.input}
            )
            response.raise_for_status()
            data = response.json()
            
            return EmbeddingResponse(
                model=request.model or self.default_model,
                data=data["data"],
                usage=data.get("usage"),
            )
        except Exception as e:
            raise ProviderError(f"OpenAI embedding error: {e}", provider=self.id)
    
    async def list_models(self) -> List[ModelInfo]:
        try:
            response = await self.client.get("/models")
            response.raise_for_status()
            data = response.json()
            
            models = []
            for m in data.get("data", []):
                models.append(ModelInfo(
                    id=m["id"],
                    name=m["id"],
                    provider=self.id,
                    capabilities=self.capabilities,
                    context_window=self.capabilities.max_context_tokens,
                ))
            return models
        except Exception:
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
            start = time.time()
            response = await self.client.get("/models")
            latency = (time.time() - start) * 1000
            
            if response.status_code == 200:
                models = response.json().get("data", [])
                return ProviderHealth(healthy=True, latency_ms=latency, model_count=len(models))
            elif response.status_code == 401:
                return ProviderHealth(healthy=False, error="Invalid API key")
            return ProviderHealth(healthy=False, error=f"HTTP {response.status_code}")
        except Exception as e:
            return ProviderHealth(healthy=False, error=str(e))
    
    def _parse_completion(self, data: Dict[str, Any], latency_ms: float) -> CompletionResponse:
        choice = data["choices"][0]
        message = choice["message"]
        
        return self._create_completion_response(
            id=data["id"],
            model=data["model"],
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            latency_ms=latency_ms,
        )
    
    def _parse_stream_chunk(self, data: Dict[str, Any]) -> StreamChunk:
        choice = data["choices"][0]
        delta = choice.get("delta", {})
        
        return self._create_stream_chunk(
            id=data["id"],
            content=delta.get("content"),
            tool_calls=delta.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            is_final=choice.get("finish_reason") is not None,
        )