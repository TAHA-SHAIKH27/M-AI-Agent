"""Tests for memory endpoints."""
import pytest
from httpx import AsyncClient


class TestMemoryEndpoints:
    """Test memory search and retrieval endpoints."""

    @pytest.mark.asyncio
    async def test_memory_search(self, async_client: AsyncClient, grant_consents):
        """Test memory search."""
        response = await async_client.post(
            "/api/v1/memory/search",
            json={"query": "test", "limit": 5}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "entries" in data
        assert "total_count" in data
        assert "query_time_ms" in data
        assert isinstance(data["entries"], list)

    @pytest.mark.asyncio
    async def test_memory_search_with_filters(self, async_client: AsyncClient, grant_consents):
        """Test memory search with tier filters."""
        response = await async_client.post(
            "/api/v1/memory/search",
            json={
                "query": "test",
                "tiers": ["episodic", "semantic"],
                "limit": 10,
                "threshold": 0.5
            }
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["total_count"] >= 0

    @pytest.mark.asyncio
    async def test_get_memory_entry(self, async_client: AsyncClient, grant_consents):
        """Test getting a specific memory entry."""
        # First search to get an entry ID
        search_resp = await async_client.post(
            "/api/v1/memory/search",
            json={"query": "test", "limit": 1}
        )
        
        if search_resp.json()["total_count"] > 0:
            entry_id = search_resp.json()["entries"][0]["id"]
            response = await async_client.get(f"/api/v1/memory/entry/{entry_id}")
            assert response.status_code == 200
            
            data = response.json()
            assert data["id"] == entry_id
            assert "tier" in data
            assert "content" in data

    @pytest.mark.asyncio
    async def test_get_nonexistent_entry(self, async_client: AsyncClient, grant_consents):
        """Test getting non-existent entry returns 404."""
        response = await async_client.get("/api/v1/memory/entry/nonexistent-id")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_conversation_history(self, async_client: AsyncClient, grant_consents):
        """Test getting conversation history."""
        response = await async_client.get(
            "/api/v1/memory/conversation/test-conv-123",
            params={"limit": 10}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_memory_stats(self, async_client: AsyncClient, grant_consents):
        """Test memory statistics endpoint."""
        response = await async_client.get("/api/v1/memory/stats")
        assert response.status_code == 200
        
        data = response.json()
        assert "stm" in data
        assert "episodic" in data
        assert "semantic" in data
        assert "procedural" in data
        assert "config" in data


class TestMemoryValidation:
    """Test memory endpoint validation."""

    @pytest.mark.asyncio
    async def test_search_empty_query(self, async_client: AsyncClient, grant_consents):
        """Test empty query is handled."""
        response = await async_client.post(
            "/api/v1/memory/search",
            json={"query": ""}
        )
        assert response.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_search_invalid_tier(self, async_client: AsyncClient, grant_consents):
        """Test invalid tier is rejected."""
        response = await async_client.post(
            "/api/v1/memory/search",
            json={"query": "test", "tiers": ["invalid"]}
        )
        assert response.status_code == 422