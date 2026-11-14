"""Provider factory with fallback chain."""
import asyncio
import structlog
from typing import List, Optional, AsyncIterator, Dict, Any

from .base import ModelProvider
from .schemas import CompletionRequest, CompletionResponse, StreamChunk, ModelInfo
from .nvidia_nemotron import NVIDIANemotronProvider
from .ollama import OllamaProvider
from .lm_studio import LMStudioProvider
from .openai_provider import OpenAIProvider
from .anthropic import AnthropicProvider
from .vision_provider import (
    BaseVisionProvider,
    VisionConfig,
    VisionProviderFactory as VisionProviderFactoryNew,
    get_vision_factory,
)
from ..config import settings
from ..core.exceptions import ProviderError
from ..utils.logging import get_logger

logger = get_logger(__name__)


class ProviderFactory:
    """Manages provider chain with automatic fallback."""
    
    def __init__(self):
        self._providers: List[ModelProvider] = []
        self._vision_factory = None  # New vision provider factory
        self._initialized = False
        # Initialize providers synchronously at creation
        self._initialize_sync()
    
    def _initialize_sync(self) -> None:
        """Synchronously initialize providers for immediate availability."""
        try:
            # 1. Primary: NVIDIA Nemotron 3 Ultra
            nemotron = NVIDIANemotronProvider()
            if settings.nvidia_api_key:
                self._providers.append(nemotron)
                logger.info("Registered NVIDIA Nemotron 3 Ultra as primary provider")
            else:
                logger.warning("NVIDIA API key not configured - Nemotron unavailable")
            
            # 2. Vision Provider Factory (for GUI understanding) - lazy async HTTP client setup
            try:
                self._vision_factory = VisionProviderFactoryNew(VisionConfig())
                logger.info("Registered Vision Provider Factory (lazy async initialization)")
            except Exception as e:
                logger.warning(f"Failed to configure Vision Provider Factory: {e}")
                self._vision_factory = None
            
            # 3. Fallback: Ollama (local)
            ollama = OllamaProvider()
            self._providers.append(ollama)
            logger.info("Registered Ollama as fallback provider")
            
            # 4. Fallback: LM Studio (local)
            lm_studio = LMStudioProvider()
            self._providers.append(lm_studio)
            logger.info("Registered LM Studio as fallback provider")
            
            # 5. Fallback: OpenAI (cloud)
            if settings.openai_api_key:
                openai = OpenAIProvider()
                self._providers.append(openai)
                logger.info("Registered OpenAI as fallback provider")
            
            # 6. Fallback: Anthropic (cloud)
            if settings.anthropic_api_key:
                anthropic = AnthropicProvider()
                self._providers.append(anthropic)
                logger.info("Registered Anthropic as fallback provider")
            
            self._initialized = True
            logger.info(f"Provider factory initialized with {len(self._providers)} providers")
        
        except Exception as e:
            logger.error(f"Failed to initialize providers: {e}")
            self._initialized = True  # Mark as initialized even on error to avoid retry loops
    
    async def _ensure_initialized(self) -> None:
        """Ensure providers are initialized (async version for compatibility)."""
        # Already initialized in __init__
        pass
    
    async def _initialize_providers(self) -> None:
        """Initialize providers in priority order (async version for compatibility)."""
        # Already initialized in __init__
        pass
    
    async def get_vision_factory(self) -> Optional[VisionProviderFactoryNew]:
        """Get the initialized vision provider factory for GUI understanding."""
        if self._vision_factory is None:
            self._vision_factory = await get_vision_factory()
        elif not self._vision_factory._initialized:
            await self._vision_factory.initialize()
        return self._vision_factory

    async def get_vision_provider(self) -> Optional[BaseVisionProvider]:
        """Get the active vision provider asynchronously."""
        factory = await self.get_vision_factory()
        return await factory.get_provider() if factory else None

    async def wait_for_initialization(self) -> None:
        """Wait for all providers to finish initialization."""
        if self._vision_factory and not self._vision_factory._initialized:
            await self._vision_factory.initialize()
    
    @property
    def providers(self) -> List[ModelProvider]:
        return self._providers.copy()
    
    def get_primary(self) -> Optional[ModelProvider]:
        return self._providers[0] if self._providers else None
    
    def get_all_providers(self) -> List[ModelProvider]:
        """Get all providers including active vision provider if initialized."""
        all_providers = self._providers.copy()
        vision_prov = self.get_vision_provider_sync()
        if vision_prov and vision_prov not in all_providers:
            all_providers.append(vision_prov)
        return all_providers
    
    def get_provider(self, provider_id: str) -> Optional[ModelProvider]:
        # Check chat providers first
        provider = next((p for p in self._providers if p.id == provider_id), None)
        if provider:
            return provider
        # Check active/registered vision providers
        if self._vision_factory:
            vision_prov = self._vision_factory._current_provider
            if vision_prov and vision_prov.provider_id == provider_id:
                return vision_prov
            for vprov in self._vision_factory._providers.values():
                if vprov.provider_id == provider_id:
                    return vprov
        return None
    
    def get_vision_provider_sync(self) -> Optional[BaseVisionProvider]:
        """Get the current vision provider synchronously if initialized."""
        if self._vision_factory:
            return self._vision_factory._current_provider
        return None
    
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
        # Also check vision provider factory
        if self._vision_factory:
            try:
                # Check the current active vision provider
                provider = self._vision_factory._current_provider
                if provider:
                    health = await provider.health_check()
                    results[provider.provider_id] = {
                        "healthy": health.healthy,
                        "latency_ms": health.latency_ms,
                        "error": health.error,
                        "model_count": health.model_count,
                        "name": provider.provider_name,
                        "type": provider.provider_type,
                    }
            except Exception as e:
                results["vision-factory"] = {
                    "healthy": False,
                    "error": str(e),
                    "name": "Vision Factory",
                    "type": "cloud",
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

    async def close(self) -> None:
        """Close initialized chat and vision provider clients cleanly."""
        if self._vision_factory is not None:
            try:
                await self._vision_factory.close()
            except Exception as e:
                logger.warning(f"Error closing vision factory: {e}")
            self._vision_factory = None

        for provider in self._providers:
            close_fn = getattr(provider, "close", None)
            if callable(close_fn):
                try:
                    await close_fn()
                except Exception as e:
                    logger.warning(f"Error closing provider {provider.id}: {e}")


# Global factory instance
provider_factory = ProviderFactory()