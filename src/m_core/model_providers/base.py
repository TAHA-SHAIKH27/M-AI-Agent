"""Base model provider interface."""
import abc
import structlog
from typing import AsyncIterator, List, Optional, Dict, Any

from .schemas import (
    CompletionRequest, CompletionResponse, StreamChunk,
    ModelInfo, ModelCapability, EmbeddingRequest, EmbeddingResponse,
    ProviderHealth, ToolSchema, ChatMessage, FinishReason,
)

logger = structlog.get_logger(__name__)


class ModelProvider(abc.ABC):
    """Abstract base class for all model providers."""
    
    # Class attributes to be overridden
    id: str = ""
    name: str = ""
    type: str = "cloud"  # "cloud", "local", "hybrid"
    capabilities: ModelCapability = ModelCapability()
    limits: Dict[str, Any] = {}
    pricing: Optional[Dict[str, float]] = None
    
    def __init__(self):
        self._health_cache: Optional[ProviderHealth] = None
    
    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Non-streaming completion."""
        pass
    
    @abc.abstractmethod
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Streaming completion."""
        pass
    
    @abc.abstractmethod
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Generate embeddings."""
        pass
    
    @abc.abstractmethod
    async def list_models(self) -> List[ModelInfo]:
        """List available models."""
        pass
    
    @abc.abstractmethod
    async def health_check(self) -> ProviderHealth:
        """Check provider health."""
        pass
    
    def supports_tool_calling(self) -> bool:
        return self.capabilities.function_calling
    
    def supports_vision(self) -> bool:
        return self.capabilities.vision
    
    def supports_streaming(self) -> bool:
        return self.capabilities.streaming
    
    def supports_reasoning(self) -> bool:
        return self.capabilities.reasoning
    
    def get_max_context(self) -> int:
        return self.capabilities.max_context_tokens
    
    def get_default_model(self) -> str:
        return self.id
    
    async def health_check_cached(self, ttl_seconds: float = 60.0) -> ProviderHealth:
        """Health check with caching."""
        import time
        if (self._health_cache is not None and 
            hasattr(self, '_health_check_time') and
            time.time() - self._health_check_time < ttl_seconds):
            return self._health_cache
        
        health = await self.health_check()
        self._health_cache = health
        self._health_check_time = time.time()
        return health
    
    def _create_completion_response(
        self,
        id: str,
        model: str,
        content: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        finish_reason: Optional[FinishReason] = None,
        usage: Optional[Dict[str, int]] = None,
        latency_ms: float = 0.0,
    ) -> CompletionResponse:
        """Helper to create completion response."""
        from .schemas import CompletionUsage
        
        usage_obj = None
        if usage:
            usage_obj = CompletionUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
        
        return CompletionResponse(
            id=id,
            model=model,
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage_obj,
            latency_ms=latency_ms,
            provider=self.id,
        )
    
    def _create_stream_chunk(
        self,
        id: str,
        content: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        finish_reason: Optional[FinishReason] = None,
        is_final: bool = False,
    ) -> StreamChunk:
        """Helper to create stream chunk."""
        return StreamChunk(
            id=id,
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            is_final=is_final,
            provider=self.id,
        )