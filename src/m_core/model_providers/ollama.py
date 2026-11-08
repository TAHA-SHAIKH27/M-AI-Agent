"""Ollama local provider implementation."""
import httpx
import time
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


class OllamaProvider(ModelProvider):
    """Ollama local provider."""
    
    id = "ollama"
    name = "Ollama (Local)"
    type = "local"
    
    capabilities = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=False,  # Limited tool calling support
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
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.default_model = settings.ollama_model
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
        messages = []
        for msg in request.messages:
            m = {"role": msg.role.value, "content": msg.content or ""}
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            messages.append(m)
        
        return {
            "model": request.model or self.default_model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": request.temperature,
                "top_p": request.top_p,
                "num_predict": request.max_tokens or -1,
            },
        }
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload = self._build_payload(request, stream=False)
        
        start = time.time()
        try:
            response = await self.client.post("/api/chat", json=payload)
            latency = (time.time() - start) * 1000
            
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    f"Ollama error: {response.status_code}",
                    provider=self.id,
                )
            
            response.raise_for_status()
            data = response.json()
            
            message = data.get("message", {})
            return self._create_completion_response(
                id=data.get("id", "ollama"),
                model=data.get("model", self.default_model),
                content=message.get("content"),
                finish_reason="stop" if data.get("done") else "length",
                latency_ms=latency,
            )
            
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama request timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama HTTP error: {e}", provider=self.id)
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request, stream=True)
        
        try:
            async with self.client.stream("POST", "/api/chat", json=payload) as response:
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        message = data.get("message", {})
                        
                        yield self._create_stream_chunk(
                            id=data.get("id", "ollama"),
                            content=message.get("content"),
                            finish_reason="stop" if data.get("done") else None,
                            is_final=data.get("done", False),
                        )
                    except json.JSONDecodeError:
                        continue
                        
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama stream timed out: {e}", provider=self.id)
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama HTTP error: {e}", provider=self.id)
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Generate embeddings via Ollama."""
        start = time.time()
        try:
            embeddings = []
            for text in request.input:
                response = await self.client.post(
                    "/api/embeddings",
                    json={"model": request.model or self.default_model, "prompt": text}
                )
                response.raise_for_status()
                data = response.json()
                embeddings.append(data.get("embedding", []))
            
            return EmbeddingResponse(
                model=request.model or self.default_model,
                data=[
                    {"object": "embedding", "embedding": emb, "index": i}
                    for i, emb in enumerate(embeddings)
                ],
                usage=CompletionUsage(
                    prompt_tokens=sum(len(t.split()) for t in request.input),
                    completion_tokens=0,
                    total_tokens=sum(len(t.split()) for t in request.input),
                ),
            )
        except Exception as e:
            raise ProviderError(f"Ollama embedding error: {e}", provider=self.id)
    
    async def list_models(self) -> List[ModelInfo]:
        try:
            response = await self.client.get("/api/tags")
            response.raise_for_status()
            data = response.json()
            
            models = []
            for m in data.get("models", []):
                models.append(ModelInfo(
                    id=m["name"],
                    name=m["name"],
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
        try:
            start = time.time()
            response = await self.client.get("/api/tags")
            latency = (time.time() - start) * 1000
            
            if response.status_code == 200:
                models = response.json().get("models", [])
                return ProviderHealth(
                    healthy=True,
                    latency_ms=latency,
                    model_count=len(models),
                )
            return ProviderHealth(healthy=False, error=f"HTTP {response.status_code}")
        except Exception as e:
            return ProviderHealth(healthy=False, error=str(e))