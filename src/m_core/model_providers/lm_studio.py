"""LM Studio local provider implementation."""
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
from ..core.exceptions import ProviderError, ProviderUnavailableError

logger = structlog.get_logger(__name__)


class LMStudioProvider(ModelProvider):
    """LM Studio local provider (OpenAI-compatible)."""
    
    id = "lm-studio"
    name = "LM Studio (Local)"
    type = "local"
    
    capabilities = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=True,
        vision=False,
        reasoning=False,
        max_context_tokens=32768,
        supported_languages=["en", "multi"],
    )
    
    limits = {
        "max_tokens_per_request": 8192,
        "max_concurrent_requests": 5,
    }
    
    pricing = {
        "input_per_1m": 0.0,
        "output_per_1m": 0.0,
    }
    
    def __init__(self):
        super().__init__()
        self.base_url = settings.lm_studio_base_url.rstrip("/")
        self.default_model = "local-model"  # LM Studio uses loaded model
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(120.0, connect=5.0),
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
        
        if request.tools:
            payload["tools"] = [msgspec.to_builtins(tool) for tool in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice
        
        return payload
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = self._build_payload(request, stream=False)
        
        start = time.time()
        try:
            response = await self.client.post("/chat/completions", json=payload)
            latency = (time.time() - start) * 1000
            
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    f"LM Studio error: {response.status_code}",
                    provider=self.id,
                )
            
            response.raise_for_status()
            data = response.json()
            
            return self._parse_completion(data, latency)
            
        except httpx.TimeoutException as e:
            raise ProviderError(f"LM Studio request timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"LM Studio HTTP error: {e}", provider=self.id)
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request, stream=True)
        
        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
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
            raise ProviderError(f"LM Studio stream timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"LM Studio HTTP error: {e}", provider=self.id)
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        try:
            embeddings = []
            for text in request.input:
                response = await self.client.post(
                    "/embeddings",
                    json={"model": request.model or self.default_model, "input": text}
                )
                response.raise_for_status()
                data = response.json()
                embeddings.append(data["data"][0]["embedding"])
            
            return EmbeddingResponse(
                model=request.model or self.default_model,
                data=[
                    {"object": "embedding", "embedding": emb, "index": i}
                    for i, emb in enumerate(embeddings)
                ],
            )
        except Exception as e:
            raise ProviderError(f"LM Studio embedding error: {e}", provider=self.id)
    
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
                name="Local Model (LM Studio)",
                provider=self.id,
                capabilities=self.capabilities,
                context_window=self.capabilities.max_context_tokens,
            )]
    
    async def health_check(self) -> ProviderHealth:
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
            reasoning_content=message.get("reasoning_content"),
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
            reasoning_content=delta.get("reasoning_content"),
            tool_calls=delta.get("tool_calls"),
            finish_reason=choice.get("finish_reason"),
            is_final=choice.get("finish_reason") is not None,
        )