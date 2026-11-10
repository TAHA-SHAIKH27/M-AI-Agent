"""Provider factory with fallback chain."""
import structlog
from typing import List, Optional, AsyncIterator, Dict, Any

from .base import ModelProvider
from .schemas import CompletionRequest, CompletionResponse, StreamChunk, ModelInfo
from .nvidia_nemotron import NVIDIANemotronProvider
from .ollama import OllamaProvider
from .lm_studio import LMStudioProvider
from .openai_provider import OpenAIProvider
from .anthropic import AnthropicProvider
from .vision_provider import VisionModelProvider, VisionConfig
from ..config import settings
from ..core.exceptions import ProviderError
from ..utils.logging import get_logger

logger = get_logger(__name__)


class ProviderFactory:
    """Manages provider chain with automatic fallback."""
    
    def __init__(self):
        self._providers: List[ModelProvider] = []
        self._vision_provider: Optional[VisionModelProvider] = None
        self._initialize_providers()
    
    def _initialize_providers(self) -> None:
        """Initialize providers in priority order."""
        
        # 1. Primary: NVIDIA Nemotron 3 Ultra
        try:
            nemotron = NVIDIANemotronProvider()
            if settings.nvidia_api_key:
                self._providers.append(nemotron)
                logger.info("Registered NVIDIA Nemotron 3 Ultra as primary provider")
            else:
                logger.warning("NVIDIA API key not configured - Nemotron unavailable")
        except Exception as e:
            logger.warning(f"Failed to initialize Nemotron provider: {e}")
        
        # 2. Vision Provider (for GUI understanding)
        try:
            self._vision_provider = VisionModelProvider()
            logger.info("Registered Vision Provider for GUI understanding")
        except Exception as e:
            logger.warning(f"Failed to initialize Vision provider: {e}")
        
        # 3. Fallback: Ollama (local)
        try:
            ollama = OllamaProvider()
            self._providers.append(ollama)
            logger.info("Registered Ollama as fallback provider")
        except Exception as e:
            logger.warning(f"Failed to initialize Ollama provider: {e}")
        
        # 4. Fallback: LM Studio (local)
        try:
            lm_studio = LMStudioProvider()
            self._providers.append(lm_studio)
            logger.info("Registered LM Studio as fallback provider")
        except Exception as e:
            logger.warning(f"Failed to initialize LM Studio provider: {e}")
        
        # 5. Fallback: OpenAI (cloud)
        try:
            if settings.openai_api_key:
                openai = OpenAIProvider()
                self._providers.append(openai)
                logger.info("Registered OpenAI as fallback provider")
        except Exception as e:
            logger.warning(f"Failed to initialize OpenAI provider: {e}")
        
        # 6. Fallback: Anthropic (cloud)
        try:
            if settings.anthropic_api_key:
                anthropic = AnthropicProvider()
                self._providers.append(anthropic)
                logger.info("Registered Anthropic as fallback provider")
        except Exception as e:
            logger.warning(f"Failed to initialize Anthropic provider: {e}")
    
    @property
    def providers(self) -> List[ModelProvider]:
        return self._providers.copy()
    
    def get_primary(self) -> Optional[ModelProvider]:
        return self._providers[0] if self._providers else None
    
    def get_provider(self, provider_id: str) -> Optional[ModelProvider]:
        # Check chat providers first
        provider = next((p for p in self._providers if p.id == provider_id), None)
        if provider:
            return provider
        # Check vision provider
        if self._vision_provider and self._vision_provider.id == provider_id:
            return self._vision_provider
        return None
    
    def get_vision_provider(self) -> Optional[VisionModelProvider]:
        """Get the vision provider for GUI understanding."""
        return self._vision_provider
    
    def get_all_providers(self) -> List[ModelProvider]:
        all_providers = self._providers.copy()
        if self._vision_provider:
            all_providers.append(self._vision_provider)
        return all_providers
    
    async def complete_with_fallback(self, request: CompletionRequest) -> CompletionResponse:
        """Try providers in order until one succeeds."""
        last_error = None
        
        for provider in self._providers:
            try:
                logger.debug(f"Trying provider: {provider.id}")
                response = await provider.complete(request)
                logger.info(f"Success with {provider.id} ({response.latency_ms:.0f}ms)")
                return response
            except Exception as e:
                last_error = e
                logger.warning(f"Provider {provider.id} failed: {e}")
                continue
        
        raise ProviderError(f"All providers failed. Last error: {last_error}")
    
    async def stream_with_fallback(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        """Stream with fallback - tries each provider."""
        for provider in self._providers:
            try:
                logger.debug(f"Streaming with provider: {provider.id}")
                async for chunk in provider.stream(request):
                    yield chunk
                return  # Success
            except Exception as e:
                logger.warning(f"Stream failed with {provider.id}: {e}")
                continue
        
        raise ProviderError("All providers failed for streaming")
    
    async def health_check_all(self) -> Dict[str, Dict[str, Any]]:
        """Check health of all providers."""
        results = {}
        for provider in self._providers:
            try:
                health = await provider.health_check_cached(ttl_seconds=30.0)
                results[provider.id] = {
                    "healthy": health.healthy,
                    "latency_ms": health.latency_ms,
                    "error": health.error,
                    "model_count": health.model_count,
                    "name": provider.name,
                    "type": provider.type,
                }
            except Exception as e:
                results[provider.id] = {
                    "healthy": False,
                    "error": str(e),
                    "name": provider.name,
                    "type": provider.type,
                }
        # Also check vision provider
        if self._vision_provider:
            try:
                health = await self._vision_provider.health_check_cached(ttl_seconds=30.0)
                results[self._vision_provider.id] = {
                    "healthy": health.healthy,
                    "latency_ms": health.latency_ms,
                    "error": health.error,
                    "model_count": health.model_count,
                    "name": self._vision_provider.name,
                    "type": self._vision_provider.type,
                }
            except Exception as e:
                results[self._vision_provider.id] = {
                    "healthy": False,
                    "error": str(e),
                    "name": self._vision_provider.name,
                    "type": self._vision_provider.type,
                }
        return results
    
    async def get_available_models(self) -> Dict[str, List[ModelInfo]]:
        """Get available models from all healthy providers."""
        models = {}
        for provider in self._providers:
            try:
                health = await provider.health_check_cached(ttl_seconds=300)
                if health.healthy:
                    model_list = await provider.list_models()
                    models[provider.id] = model_list
            except Exception as e:
                logger.warning(f"Failed to get models from {provider.id}: {e}")
        return models
    
    def replace_providers(self, providers: List[ModelProvider]) -> None:
        """Replace provider chain for testing."""
        self._providers = providers
        logger.info(f"Replaced provider chain with {len(providers)} test providers")


# Global factory instance
provider_factory = ProviderFactory()