"""Model provider package for M AI Assistant."""
from .schemas import (
    ChatMessage,
    ToolSchema,
    ToolFunction,
    ModelCapability,
    ModelInfo,
    CompletionRequest,
    CompletionResponse,
    CompletionUsage,
    StreamChunk,
    EmbeddingRequest,
    EmbeddingResponse,
    ProviderHealth,
    FinishReason,
)
from .base import ModelProvider
from .factory import ProviderFactory, provider_factory
from .nvidia_nemotron import NVIDIANemotronProvider
from .ollama import OllamaProvider
from .lm_studio import LMStudioProvider
from .openai_provider import OpenAIProvider
from .anthropic import AnthropicProvider
from .vision_provider import (
    VisionProviderType,
    VisionConfig,
    VisionAction,
    ScreenAnalysis,
    BaseVisionProvider,
    NVIDIANIMVisionProvider,
    GeminiVisionProvider,
    VisionProviderFactory,
    get_vision_factory,
)

__all__ = [
    # Schemas
    "ChatMessage", "ToolSchema", "ToolFunction", "ModelCapability", "ModelInfo",
    "CompletionRequest", "CompletionResponse", "CompletionUsage", "StreamChunk",
    "EmbeddingRequest", "EmbeddingResponse", "ProviderHealth", "FinishReason",
    # Base
    "ModelProvider",
    # Factory
    "ProviderFactory", "provider_factory",
    # Providers
    "NVIDIANemotronProvider", "OllamaProvider", "LMStudioProvider",
    "OpenAIProvider", "AnthropicProvider",
    # Vision
    "VisionProviderType",
    "VisionConfig",
    "VisionAction",
    "ScreenAnalysis",
    "BaseVisionProvider",
    "NVIDIANIMVisionProvider",
    "GeminiVisionProvider",
    "VisionProviderFactory",
    "get_vision_factory",
]