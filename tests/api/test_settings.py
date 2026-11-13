"""Tests for settings endpoints."""
import pytest
from httpx import AsyncClient
from m_core.model_providers import provider_factory


class TestSettingsEndpoints:
    """Test settings and configuration endpoints."""

    @pytest.mark.asyncio
    async def test_get_settings(self, async_client: AsyncClient, grant_consents):
        """Test getting safe settings."""
        response = await async_client.get("/api/v1/settings")
        assert response.status_code == 200
        
        data = response.json()
        required = [
            "app_name", "app_version", "debug", "api_host", "api_port",
            "nvidia_model", "ollama_model", "openai_model", "anthropic_model",
            "available_providers"
        ]
        for field in required:
            assert field in data, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_no_secrets_in_settings(self, async_client: AsyncClient, grant_consents):
        """Test no API keys or secrets in settings response."""
        response = await async_client.get("/api/v1/settings")
        data = response.json()
        
        # Convert to string to search
        import json
        response_str = json.dumps(data).lower()
        
        forbidden = ["api_key", "secret", "password", "token", "private"]
        for term in forbidden:
            assert term not in response_str, f"Found {term} in settings response"

    @pytest.mark.asyncio
    async def test_settings_values(self, async_client: AsyncClient, grant_consents):
        """Test settings have expected values."""
        response = await async_client.get("/api/v1/settings")
        data = response.json()
        
        assert data["app_name"] == "M"
        assert data["api_host"] == "127.0.0.1"
        assert data["api_port"] == 8080
        assert "nvidia/nemotron-3-ultra" in data["nvidia_model"]
        assert isinstance(data["available_providers"], list)
        expected_provider_ids = [provider.id for provider in provider_factory.get_all_providers()]
        assert data["available_providers"] == expected_provider_ids


class TestProvidersEndpoints:
    """Test provider information endpoints."""

    @pytest.mark.asyncio
    async def test_get_providers(self, async_client: AsyncClient, grant_consents):
        """Test getting provider information."""
        response = await async_client.get("/api/v1/settings/providers")
        assert response.status_code == 200
        
        data = response.json()
        assert "providers" in data
        assert "primary" in data
        assert isinstance(data["providers"], list)

    @pytest.mark.asyncio
    async def test_provider_structure(self, async_client: AsyncClient, grant_consents):
        """Test provider info has required fields."""
        response = await async_client.get("/api/v1/settings/providers")
        data = response.json()
        
        for provider in data["providers"]:
            assert "id" in provider
            assert "name" in provider
            assert "type" in provider
            assert "is_primary" in provider
            assert "capabilities" in provider
            assert "default_model" in provider

    @pytest.mark.asyncio
    async def test_provider_health(self, async_client: AsyncClient, grant_consents):
        """Test provider health check."""
        response = await async_client.get("/api/v1/settings/health")
        assert response.status_code == 200
        
        data = response.json()
        assert "providers" in data
        
        for pid, health in data["providers"].items():
            assert "healthy" in health
            assert "latency_ms" in health
            assert "model_count" in health