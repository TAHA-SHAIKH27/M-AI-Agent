"""Tests for health check endpoints."""
import pytest
from httpx import AsyncClient


class TestHealthEndpoints:
    """Test health check endpoints."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, async_client: AsyncClient):
        """Test basic health endpoint returns valid response."""
        response = await async_client.get("/api/v1/health")
        assert response.status_code == 200
        
        data = response.json()
        assert "status" in data
        assert data["status"] in ("healthy", "degraded", "unhealthy")
        assert "timestamp" in data
        assert "version" in data
        assert "components" in data
        assert "uptime_seconds" in data

    @pytest.mark.asyncio
    async def test_health_components_structure(self, async_client: AsyncClient):
        """Test health endpoint includes all expected components."""
        response = await async_client.get("/api/v1/health")
        data = response.json()
        
        components = data["components"]
        expected = ["providers", "memory", "orchestrator", "tools", "agents"]
        for comp in expected:
            assert comp in components, f"Missing component: {comp}"

    @pytest.mark.asyncio
    async def test_readiness_endpoint(self, async_client: AsyncClient):
        """Test readiness probe endpoint."""
        response = await async_client.get("/api/v1/health/ready")
        assert response.status_code == 200
        
        data = response.json()
        assert "ready" in data
        assert isinstance(data["ready"], bool)
        assert "primary_provider_healthy" in data
        assert "memory_ready" in data
        assert "orchestrator_initialized" in data

    @pytest.mark.asyncio
    async def test_liveness_endpoint(self, async_client: AsyncClient):
        """Test liveness probe endpoint."""
        response = await async_client.get("/api/v1/health/live")
        assert response.status_code == 200
        
        data = response.json()
        assert "alive" in data
        assert data["alive"] is True
        assert "timestamp" in data


class TestHealthValidation:
    """Test health endpoint validation and edge cases."""

    @pytest.mark.asyncio
    async def test_health_content_type(self, async_client: AsyncClient):
        """Test health endpoint returns JSON."""
        response = await async_client.get("/api/v1/health")
        assert response.headers["content-type"].startswith("application/json")

    @pytest.mark.asyncio
    async def test_uptime_increases(self, async_client: AsyncClient):
        """Test uptime increases between requests."""
        response1 = await async_client.get("/api/v1/health")
        uptime1 = response1.json()["uptime_seconds"]
        
        import asyncio
        await asyncio.sleep(0.1)
        
        response2 = await async_client.get("/api/v1/health")
        uptime2 = response2.json()["uptime_seconds"]
        
        assert uptime2 >= uptime1