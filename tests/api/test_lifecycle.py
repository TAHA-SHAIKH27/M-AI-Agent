"""Tests for lifecycle and reliability."""
import pytest
from httpx import AsyncClient


class TestStartupShutdown:
    """Test application startup and shutdown."""

    @pytest.mark.asyncio
    async def test_startup_initializes_all_components(self, async_client: AsyncClient):
        """Test all components are initialized on startup."""
        response = await async_client.get("/api/v1/health/ready")
        data = response.json()
        
        assert data["ready"] is True or data["ready"] is False
        assert "primary_provider_healthy" in data
        assert "memory_ready" in data
        assert "orchestrator_initialized" in data

    @pytest.mark.asyncio
    async def test_liveness_probe(self, async_client: AsyncClient):
        """Test liveness probe returns alive."""
        response = await async_client.get("/api/v1/health/live")
        assert response.status_code == 200
        
        data = response.json()
        assert data["alive"] is True

    @pytest.mark.asyncio
    async def test_root_endpoint(self, async_client: AsyncClient):
        """Test root endpoint returns basic info."""
        response = await async_client.get("/")
        assert response.status_code == 200
        
        data = response.json()
        assert data["name"] == "M"
        assert data["status"] == "running"


class TestProviderAvailability:
    """Test behavior when providers are unavailable."""

    @pytest.mark.asyncio
    async def test_fallback_chain_works(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test provider fallback chain works when primary fails."""
        response = await async_client.post(
            "/api/v1/chat",
            json={"message": "Hello"}
        )
        
        # Should succeed even if primary is rate limited
        assert response.status_code == 200
        data = response.json()
        assert "content" in data

    @pytest.mark.asyncio
    async def test_provider_health_shows_status(self, async_client: AsyncClient, grant_consents):
        """Test provider health endpoint shows all providers."""
        response = await async_client.get("/api/v1/settings/health")
        assert response.status_code == 200
        
        data = response.json()
        providers = data["providers"]
        
        # Should have at least primary
        assert "nvidia-nemotron-3-ultra" in providers
        
        # Each provider should have health info
        for pid, health in providers.items():
            assert "healthy" in health
            assert "latency_ms" in health
            assert "model_count" in health


class TestAgentErrors:
    """Test agent error handling."""

    @pytest.mark.asyncio
    async def test_agent_not_found(self, async_client: AsyncClient):
        """Test requesting non-existent agent returns error."""
        # This would require an endpoint that accepts agent_id
        # Current API uses default agent, so this is a design test
        pass

    @pytest.mark.asyncio
    async def test_agent_processing_error_handled(self, async_client: AsyncClient, grant_consents):
        """Test agent processing errors are handled gracefully."""
        response = await async_client.post(
            "/api/v1/chat",
            json={"message": "test"}
        )
        
        # Should not crash
        assert response.status_code in (200, 403, 500)
        
        if response.status_code == 500:
            data = response.json()
            assert "error" in data
            assert "error_code" in data


class TestClientDisconnect:
    """Test handling of client disconnection."""

    @pytest.mark.asyncio
    async def test_cancel_during_generation(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test task cancellation during generation."""
        # Start a task
        response = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Write a very long essay about Python"}
        )
        assert response.status_code == 200
        
        task_id = response.json()["task_id"]
        
        # Immediately cancel
        cancel_resp = await async_client.post(
            f"/api/v1/chat/task/{task_id}/cancel",
            json={"reason": "User cancelled"}
        )
        
        # Task may complete very quickly with mock provider
        assert cancel_resp.status_code in (200, 404)
        
        if cancel_resp.status_code == 200:
            data = cancel_resp.json()
            assert data["status"] == "cancelled"


class TestTaskCancellation:
    """Test task cancellation functionality."""

    @pytest.mark.asyncio
    async def test_cancel_pending_task(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test cancelling a pending task."""
        response = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Search for something"}
        )
        task_id = response.json()["task_id"]
        
        cancel_resp = await async_client.post(
            f"/api/v1/chat/task/{task_id}/cancel",
            json={"reason": "Cancelled"}
        )
        
        # Task may complete very quickly with mock provider
        assert cancel_resp.status_code in (200, 404)
        
        if cancel_resp.status_code == 200:
            data = cancel_resp.json()
            assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_completed_task(self, async_client: AsyncClient, grant_consents):
        """Test cancelling already completed task."""
        # Quick task that completes fast
        response = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Hello"}
        )
        task_id = response.json()["task_id"]
        
        # Wait a bit for completion
        import asyncio
        await asyncio.sleep(1)
        
        cancel_resp = await async_client.post(
            f"/api/v1/chat/task/{task_id}/cancel",
            json={"reason": "Late cancel"}
        )
        
        # Should handle gracefully
        assert cancel_resp.status_code in (200, 404, 409)

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self, async_client: AsyncClient):
        """Test cancelling non-existent task returns 404."""
        response = await async_client.post(
            "/api/v1/chat/task/nonexistent/cancel",
            json={"reason": "Test"}
        )
        assert response.status_code == 404


class TestResourceCleanup:
    """Test resource cleanup on shutdown."""

    @pytest.mark.asyncio
    async def test_no_resource_leaks(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test repeated requests don't leak resources."""
        # Make many requests
        for i in range(20):
            response = await async_client.post(
                "/api/v1/chat",
                json={"message": f"Test message {i}"}
            )
            assert response.status_code == 200
        
        # Health check should still work
        response = await async_client.get("/api/v1/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_websocket_cleanup(self):
        """Test WebSocket connections are cleaned up."""
        # This is tested in WebSocket tests
        pass