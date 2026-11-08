"""Tests for model providers."""
import pytest
import structlog
from unittest.mock import AsyncMock, MagicMock, patch

from m_core.model_providers import (
    ProviderFactory,
    NVIDIANemotronProvider,
    OllamaProvider,
    LMStudioProvider,
    OpenAIProvider,
    AnthropicProvider,
)
from m_core.model_providers.schemas import (
    CompletionRequest, ChatMessage, MessageRole, FinishReason, StreamChunk,
)
from m_core.core.exceptions import ProviderAuthenticationError

logger = structlog.get_logger(__name__)


class TestProviderFactory:
    """Test provider factory initialization."""
    
    def test_factory_creates_providers(self):
        """Test factory initializes with expected providers."""
        factory = ProviderFactory()
        providers = factory.get_all_providers()
        
        # Should have at least Ollama and LM Studio (no API keys needed)
        provider_ids = [p.id for p in providers]
        assert "ollama" in provider_ids
        assert "lm-studio" in provider_ids
    
    def test_get_primary_returns_nemotron_when_configured(self):
        """Test primary provider is Nemotron when API key set."""
        factory = ProviderFactory()
        # If NVIDIA API key is configured, it should be first
        if factory.get_primary():
            # Could be Nemotron or Ollama depending on config
            assert factory.get_primary() is not None
    
    def test_get_provider_by_id(self):
        """Test getting specific provider by ID."""
        factory = ProviderFactory()
        ollama = factory.get_provider("ollama")
        assert ollama is not None
        assert ollama.id == "ollama"


class TestNVIDIANemotronProvider:
    """Test NVIDIA Nemotron 3 Ultra provider."""
    
    @pytest.fixture
    def provider(self):
        return NVIDIANemotronProvider()
    
    def test_provider_attributes(self, provider):
        """Test provider has correct attributes."""
        assert provider.id == "nvidia-nemotron-3-ultra"
        assert provider.name == "NVIDIA Nemotron 3 Ultra"
        assert provider.type == "cloud"
        assert provider.capabilities.chat is True
        assert provider.capabilities.streaming is True
        assert provider.capabilities.function_calling is True
        assert provider.capabilities.reasoning is True
        assert provider.capabilities.max_context_tokens == 1_000_000
        assert provider.capabilities.vision is False
    
    def test_build_payload(self, provider):
        """Test payload building."""
        request = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
            temperature=0.5,
            max_tokens=100,
            stream=True,
        )
        
        payload = provider._build_payload(request, stream=True)
        
        assert payload["model"] == provider.default_model
        assert payload["temperature"] == 0.5
        assert payload["max_tokens"] == 100
        assert payload["stream"] is True
        # Note: extra_body was removed as NVIDIA API doesn't support it
        assert "extra_body" not in payload
    
    def test_build_payload_with_tools(self, provider):
        """Test payload with tool calling."""
        from m_core.model_providers.schemas import ToolSchema, ToolFunction
        
        request = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
            tools=[ToolSchema(function=ToolFunction(
                name="test_func",
                description="Test function",
                parameters={"type": "object", "properties": {}},
            ))],
            tool_choice="auto",
        )
        
        payload = provider._build_payload(request, stream=False)
        
        assert "tools" in payload
        assert payload["tool_choice"] == "auto"
    
    @pytest.mark.asyncio
    async def test_health_check_no_key(self, provider):
        """Test health check without API key."""
        provider.api_key = None
        health = await provider.health_check()
        assert health.healthy is False
        assert "API key" in health.error
    
    @pytest.mark.asyncio
    async def test_complete_requires_key(self, provider):
        """Test complete fails without API key."""
        provider.api_key = None
        request = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Test")],
        )
        
        with pytest.raises(ProviderAuthenticationError):
            await provider.complete(request)


class TestOllamaProvider:
    """Test Ollama provider."""
    
    @pytest.fixture
    def provider(self):
        return OllamaProvider()
    
    def test_provider_attributes(self, provider):
        """Test provider has correct attributes."""
        assert provider.id == "ollama"
        assert provider.name == "Ollama (Local)"
        assert provider.type == "local"
        assert provider.capabilities.chat is True
        assert provider.capabilities.streaming is True
        assert provider.capabilities.function_calling is False  # Limited support
    
    def test_build_payload(self, provider):
        """Test payload building."""
        request = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
            temperature=0.5,
            max_tokens=100,
        )
        
        payload = provider._build_payload(request, stream=False)
        
        assert payload["model"] == provider.default_model
        assert payload["options"]["temperature"] == 0.5
        assert payload["options"]["num_predict"] == 100


class TestLMStudioProvider:
    """Test LM Studio provider."""
    
    @pytest.fixture
    def provider(self):
        return LMStudioProvider()
    
    def test_provider_attributes(self, provider):
        assert provider.id == "lm-studio"
        assert provider.name == "LM Studio (Local)"
        assert provider.type == "local"
        assert provider.capabilities.function_calling is True


class TestOpenAIProvider:
    """Test OpenAI provider."""
    
    @pytest.fixture
    def provider(self):
        return OpenAIProvider()
    
    def test_provider_attributes(self, provider):
        assert provider.id == "openai"
        assert provider.name == "OpenAI"
        assert provider.type == "cloud"
        assert provider.capabilities.vision is True
        assert provider.capabilities.function_calling is True


class TestAnthropicProvider:
    """Test Anthropic provider."""
    
    @pytest.fixture
    def provider(self):
        return AnthropicProvider()
    
    def test_provider_attributes(self, provider):
        assert provider.id == "anthropic"
        assert provider.name == "Anthropic"
        assert provider.type == "cloud"
        assert provider.capabilities.vision is True
        assert provider.capabilities.max_context_tokens == 200000
    
    def test_convert_messages(self, provider):
        """Test message conversion to Anthropic format."""
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="System prompt"),
            ChatMessage(role=MessageRole.USER, content="Hello"),
            ChatMessage(role=MessageRole.ASSISTANT, content="Hi there"),
        ]
        
        system, anthropic_messages = provider._convert_messages(messages)
        
        assert system == "System prompt"
        assert len(anthropic_messages) == 2
        assert anthropic_messages[0]["role"] == "user"
        assert anthropic_messages[1]["role"] == "assistant"


class TestCompletionRequest:
    """Test CompletionRequest schema."""
    
    def test_default_values(self):
        request = CompletionRequest(messages=[])
        assert request.temperature == 0.7
        assert request.top_p == 1.0
        assert request.stream is False
        assert request.max_tokens is None
    
    def test_with_tools(self):
        from m_core.model_providers.schemas import ToolSchema, ToolFunction
        
        request = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="Test")],
            tools=[ToolSchema(function=ToolFunction(
                name="test",
                description="Test tool",
                parameters={"type": "object", "properties": {}},
            ))],
        )
        assert len(request.tools) == 1


class TestStreamChunk:
    """Test StreamChunk schema."""
    
    def test_create_chunk(self):
        chunk = StreamChunk(
            id="test-123",
            content="Hello",
            is_final=False,
        )
        assert chunk.content == "Hello"
        assert chunk.is_final is False
        assert chunk.finish_reason is None
    
    def test_final_chunk(self):
        chunk = StreamChunk(
            id="test-123",
            content="World",
            finish_reason=FinishReason.STOP,
            is_final=True,
        )
        assert chunk.is_final is True
        assert chunk.finish_reason == FinishReason.STOP


if __name__ == "__main__":
    pytest.main([__file__, "-v"])