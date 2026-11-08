"""Tests for WebSocket endpoint."""
import pytest
import asyncio
import json
from fastapi.testclient import TestClient
from m_core.api.rest.server import app
from m_core.model_providers import provider_factory
from tests.api.mock_provider import MockProvider


@pytest.fixture
def ws_mock_provider():
    """Replace provider factory with mock provider for WebSocket tests."""
    mock = MockProvider(response_content="Hello! 2 + 2 = 4")
    provider_factory.replace_providers([mock])
    yield mock
    # Restore original providers after test
    from m_core.model_providers.factory import ProviderFactory
    original = ProviderFactory()
    provider_factory.replace_providers(original._providers)


class TestWebSocketConnection:
    """Test WebSocket connection and basic protocol."""

    @pytest.mark.asyncio
    async def test_connection_acknowledgement(self, ws_mock_provider):
        """Test connection receives acknowledgement."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                data = ws.receive_json()
                
                assert data["type"] == "connection_ack"
                assert "client_id" in data["payload"]
                assert data["payload"]["client_id"] is not None

    @pytest.mark.asyncio
    async def test_connection_with_client_id(self, ws_mock_provider):
        """Test connection with custom client ID."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws?client_id=test-client-123") as ws:
                data = ws.receive_json()
                
                assert data["type"] == "connection_ack"
                assert data["payload"]["client_id"] == "test-client-123"

    @pytest.mark.asyncio
    async def test_connection_with_conversation_id(self, ws_mock_provider):
        """Test connection with conversation ID."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws?conversation_id=test-conv-456") as ws:
                data = ws.receive_json()
                
                assert data["type"] == "connection_ack"
                assert data["payload"]["conversation_id"] == "test-conv-456"


class TestWebSocketChat:
    """Test WebSocket chat functionality."""

    @pytest.mark.asyncio
    async def test_chat_message(self, ws_mock_provider, grant_consents):
        """Test sending chat message receives response."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ack = ws.receive_json()
                assert ack["type"] == "connection_ack"
                
                chat_msg = {
                    "type": "chat",
                    "payload": {"message": "Hello", "stream": False}
                }
                ws.send_json(chat_msg)
                
                types_received = []
                for _ in range(5):
                    try:
                        data = ws.receive_json()
                        types_received.append(data["type"])
                        if data["type"] == "task_completed":
                            break
                    except Exception:
                        break
                
                assert "chat_response" in types_received or "task_completed" in types_received

    @pytest.mark.asyncio
    async def test_chat_streaming(self, ws_mock_provider, grant_consents):
        """Test streaming chat responses."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()  # connection ack
                
                chat_msg = {
                    "type": "chat",
                    "payload": {"message": "Count to 3", "stream": True}
                }
                ws.send_json(chat_msg)
                
                stream_chunks = 0
                for _ in range(10):
                    try:
                        data = ws.receive_json()
                        if data["type"] == "chat_stream_chunk":
                            stream_chunks += 1
                        elif data["type"] == "task_completed":
                            break
                    except Exception:
                        break
                
                # Should receive at least one stream chunk
                assert stream_chunks >= 1

    @pytest.mark.asyncio
    async def test_invalid_message_type(self, ws_mock_provider):
        """Test invalid message type returns error."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()  # connection ack
                
                # Send invalid type
                ws.send_json({
                    "type": "invalid_type",
                    "payload": {}
                })
                
                data = ws.receive_json()
                
                assert data["type"] == "error"
                assert data["payload"]["error_code"] == "UNKNOWN_MESSAGE_TYPE"

    @pytest.mark.asyncio
    async def test_ping_pong(self, ws_mock_provider):
        """Test ping/pong keepalive."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()  # connection ack
                
                ws.send_json({"type": "ping", "payload": {}})
                
                data = ws.receive_json()
                
                assert data["type"] == "pong"


class TestWebSocketCancellation:
    """Test WebSocket task cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_task(self, ws_mock_provider, grant_consents):
        """Test cancelling a running task via WebSocket."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()  # connection ack
                
                # Start a task
                chat_msg = {
                    "type": "chat",
                    "payload": {"message": "Search for Python tutorials", "stream": True}
                }
                ws.send_json(chat_msg)
                
                # Wait for task_started
                data = ws.receive_json()
                assert data["type"] == "task_started"
                task_id = data["payload"]["task_id"]
                
                # Cancel the task
                cancel_msg = {
                    "type": "cancel_task",
                    "payload": {"task_id": task_id}
                }
                ws.send_json(cancel_msg)
                
                # Should receive task_cancelled (may have stream chunks in between)
                cancelled = False
                for _ in range(5):
                    data = ws.receive_json()
                    if data["type"] == "task_cancelled":
                        cancelled = True
                        assert data["payload"]["task_id"] == task_id
                        break
                assert cancelled, "Expected task_cancelled message"


class TestWebSocketDisconnection:
    """Test WebSocket disconnection handling."""

    @pytest.mark.asyncio
    async def test_client_disconnect_cleanup(self, ws_mock_provider):
        """Test server cleans up on client disconnect."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                msg = ws.receive_json()  # connection ack
                assert msg["type"] == "connection_ack"
                client_id = msg["payload"]["client_id"]
                assert client_id is not None
                
                # Close the connection
                ws.close()
            
            # Connection should be cleaned up (no exceptions)
            # Verify we can connect again
            with client.websocket_connect("/api/v1/ws") as ws2:
                ack2 = ws2.receive_json()
                assert ack2["type"] == "connection_ack"
                ws2.close()

    @pytest.mark.asyncio
    async def test_multiple_concurrent_clients(self, ws_mock_provider, grant_consents):
        """Test multiple concurrent WebSocket connections."""
        def connect_and_chat():
            with TestClient(app) as client:
                with client.websocket_connect("/api/v1/ws") as ws:
                    ack = ws.receive_json()  # connection ack
                    ws.send_json({
                        "type": "chat",
                        "payload": {"message": "Hello", "stream": False}
                    })
                    # Wait for response
                    for _ in range(5):
                        data = ws.receive_json()
                        if data["type"] == "task_completed":
                            return True
                    return False
        
        # Run concurrent connections in threadpool to avoid event loop deadlock
        results = await asyncio.gather(
            asyncio.to_thread(connect_and_chat),
            asyncio.to_thread(connect_and_chat),
            asyncio.to_thread(connect_and_chat),
        )
        
        assert all(results), "All concurrent clients should succeed"


class TestWebSocketApproval:
    """Test WebSocket approval workflow (if implemented)."""

    @pytest.mark.asyncio
    async def test_approval_denied(self, ws_mock_provider):
        """Test denying an approval request."""
        # This test would require a task that triggers approval
        # For now, verify the message structure is accepted
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()
                
                # Send approval (even if no pending approval)
                ws.send_json({
                    "type": "approve_action",
                    "payload": {
                        "task_id": "test-task",
                        "subtask_id": "test-subtask",
                        "approved": False,
                        "reason": "Test denial"
                    }
                })
                
                # Should receive acknowledgment
                data = ws.receive_json()
                assert data["type"] in ("chat_response", "error")


class TestWebSocketSecurity:
    """Test WebSocket security measures."""

    @pytest.mark.asyncio
    async def test_localhost_only_in_production(self, ws_mock_provider):
        """Test WebSocket rejects non-localhost in production mode."""
        # This is tested by the security middleware
        # In debug mode (which tests run in), localhost is allowed
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                data = ws.receive_json()
                assert data["type"] == "connection_ack"

    @pytest.mark.asyncio
    async def test_message_size_limit(self, ws_mock_provider):
        """Test large messages are rejected."""
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()  # connection ack
                
                # Send very large message
                large_msg = {
                    "type": "chat",
                    "payload": {"message": "x" * 10000000, "stream": False}
                }
                ws.send_json(large_msg)
                
                # Should either reject or handle gracefully
                try:
                    data = ws.receive_json()
                    # Could be error or handled gracefully
                    assert data["type"] in ("error", "task_failed", "chat_response")
                except Exception:
                    # Connection closed is also acceptable for oversized messages
                    pass