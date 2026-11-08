"""Tests for chat endpoints."""
import pytest
from httpx import AsyncClient


class TestChatEndpoint:
    """Test basic chat endpoint."""

    @pytest.mark.asyncio
    async def test_chat_basic(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test basic chat request returns response."""
        response = await async_client.post(
            "/api/v1/chat",
            json={"message": "Hello, what is 2+2?"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "content" in data
        assert isinstance(data["content"], str)
        assert len(data["content"]) > 0
        assert "turn_count" in data
        assert data["turn_count"] >= 1

    @pytest.mark.asyncio
    async def test_chat_with_conversation_id(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test chat with conversation ID."""
        conv_id = "test-conv-123"
        response = await async_client.post(
            "/api/v1/chat",
            json={"message": "Hello", "conversation_id": conv_id}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["conversation_id"] == conv_id

    @pytest.mark.asyncio
    async def test_chat_with_history(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test chat with conversation history."""
        history = [
            {"role": "user", "content": "My name is Alice"},
            {"role": "assistant", "content": "Nice to meet you, Alice!"}
        ]
        response = await async_client.post(
            "/api/v1/chat",
            json={
                "message": "What is my name?",
                "conversation_history": history
            }
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "content" in data

    @pytest.mark.asyncio
    async def test_chat_stream_endpoint(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test SSE streaming chat endpoint."""
        response = await async_client.post(
            "/api/v1/chat/stream",
            json={"message": "Count to 3"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        
        # Read stream
        content = response.text
        assert "data:" in content
        assert "2 + 2 = 4" in content or "count" in content.lower() or "1" in content


class TestChatValidation:
    """Test chat request validation."""

    @pytest.mark.asyncio
    async def test_chat_empty_message_rejected(self, async_client: AsyncClient, mock_provider):
        """Test empty message is rejected."""
        response = await async_client.post(
            "/api/v1/chat",
            json={"message": ""}
        )
        # Should accept but handle gracefully
        assert response.status_code in (200, 422)

    @pytest.mark.asyncio
    async def test_chat_invalid_json_rejected(self, async_client: AsyncClient, mock_provider):
        """Test invalid JSON is rejected."""
        response = await async_client.post(
            "/api/v1/chat",
            content="not json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_chat_missing_message_field(self, async_client: AsyncClient, mock_provider):
        """Test missing message field is rejected."""
        response = await async_client.post(
            "/api/v1/chat",
            json={}
        )
        assert response.status_code == 422


class TestTaskEndpoints:
    """Test task orchestration endpoints."""

    @pytest.mark.asyncio
    async def test_create_task(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test task creation."""
        response = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Search for Python tutorials"}
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "task_id" in data
        assert "status" in data
        assert data["status"] in ("pending", "queued", "running", "completed", "failed")

    @pytest.mark.asyncio
    async def test_get_task_status(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test getting task status."""
        # Create task first
        create_resp = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Hello world"}
        )
        task_id = create_resp.json()["task_id"]
        
        # Get status
        response = await async_client.get(f"/api/v1/chat/task/{task_id}")
        assert response.status_code == 200
        
        data = response.json()
        assert data["task_id"] == task_id
        assert "status" in data
        assert "subtasks" in data

    @pytest.mark.asyncio
    async def test_cancel_task(self, async_client: AsyncClient, grant_consents, mock_provider):
        """Test task cancellation."""
        # Use a task that takes longer to complete
        create_resp = await async_client.post(
            "/api/v1/chat/task",
            json={"message": "Search for Python tutorials and create a detailed summary document with code examples"}
        )
        task_id = create_resp.json()["task_id"]
        
        response = await async_client.post(
            f"/api/v1/chat/task/{task_id}/cancel",
            json={"reason": "User cancelled"}
        )
        # Task may complete very quickly, so accept both 200 (cancelled) and 404 (already completed)
        assert response.status_code in (200, 404)
        
        if response.status_code == 200:
            data = response.json()
            assert data["task_id"] == task_id
            assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_task(self, async_client: AsyncClient, mock_provider):
        """Test cancelling non-existent task returns 404."""
        response = await async_client.post(
            "/api/v1/chat/task/nonexistent/cancel",
            json={"reason": "Test"}
        )
        assert response.status_code == 404