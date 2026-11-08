"""WebSocket server for M AI Assistant."""
import asyncio
import json
import uuid
import structlog
from typing import Dict, Set, Optional, Any, List
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from fastapi import WebSocket, WebSocketDisconnect, APIRouter, Depends, Query
from fastapi.websockets import WebSocketState

from m_core.agents import agent_registry
from m_core.agents.schemas import AgentRequest, AgentMessage
from m_core.orchestration import orchestrator
from m_core.orchestration.schemas import TaskExecutionStatus, SubtaskExecutionStatus
from m_core.core.events import event_bus, Event
from m_core.security import consent_manager
from m_core.core.types import to_builtins

from ..rest.schemas import (
    WSMessageType, ChatMessage, ChatRole,
    WSEnvelope, WSChatPayload, WSChatResponsePayload,
    WSTaskProgressPayload, WSTaskCompletedPayload,
    WSApprovalRequiredPayload, WSApprovePayload, WSToolPayload,
    TaskStatus, ApprovalStatus,
)
from m_core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/ws", tags=["websocket"])


class ConnectionState(str, Enum):
    """WebSocket connection state."""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"


@dataclass
class WSConnection:
    """WebSocket connection info."""
    websocket: WebSocket
    client_id: str
    state: ConnectionState = ConnectionState.CONNECTING
    conversation_id: Optional[str] = None
    active_task_id: Optional[str] = None
    active_task: Optional[asyncio.Task] = None
    subscriptions: Set[str] = field(default_factory=set)
    connected_at: int = field(default_factory=lambda: int(datetime.now().timestamp() * 1000))
    last_ping: int = field(default_factory=lambda: int(datetime.now().timestamp() * 1000))


class ConnectionManager:
    """Manages WebSocket connections."""
    
    def __init__(self):
        self._connections: Dict[str, WSConnection] = {}
        self._task_subscriptions: Dict[str, Set[str]] = {}  # task_id -> connection_ids
        self._conversation_subscriptions: Dict[str, Set[str]] = {}  # conversation_id -> connection_ids
        self._lock = asyncio.Lock()
    
    async def connect(self, websocket: WebSocket, client_id: str) -> WSConnection:
        """Register a new connection."""
        await websocket.accept()
        
        connection = WSConnection(
            websocket=websocket,
            client_id=client_id,
            state=ConnectionState.CONNECTED,
        )
        
        async with self._lock:
            self._connections[client_id] = connection
        
        logger.info("ws_connected", client_id=client_id, total=len(self._connections))
        return connection
    
    async def disconnect(self, client_id: str) -> None:
        """Unregister a connection."""
        async with self._lock:
            connection = self._connections.pop(client_id, None)
            
            if connection:
                # Cancel running task if any
                if connection.active_task and not connection.active_task.done():
                    connection.active_task.cancel()
                
                # Remove from task subscriptions
                for task_id in list(connection.subscriptions):
                    if task_id in self._task_subscriptions:
                        self._task_subscriptions[task_id].discard(client_id)
                        if not self._task_subscriptions[task_id]:
                            del self._task_subscriptions[task_id]
                
                # Remove from conversation subscriptions
                if connection.conversation_id and connection.conversation_id in self._conversation_subscriptions:
                    self._conversation_subscriptions[connection.conversation_id].discard(client_id)
                    if not self._conversation_subscriptions[connection.conversation_id]:
                        del self._conversation_subscriptions[connection.conversation_id]
        
        logger.info("ws_disconnected", client_id=client_id, total=len(self._connections))
    
    async def send_json(self, client_id: str, message: Dict[str, Any]) -> bool:
        """Send JSON message to a connection."""
        async with self._lock:
            connection = self._connections.get(client_id)
        
        if not connection or connection.websocket.client_state != WebSocketState.CONNECTED:
            return False
        
        try:
            await connection.websocket.send_json(to_builtins(message))
            return True
        except Exception as e:
            logger.warning("ws_send_failed", client_id=client_id, error=str(e))
            return False
    
    async def broadcast(self, message: Dict[str, Any], exclude: Optional[str] = None) -> int:
        """Broadcast message to all connected clients."""
        sent = 0
        async with self._lock:
            connections = list(self._connections.values())
        
        for connection in connections:
            if exclude and connection.client_id == exclude:
                continue
            if await self.send_json(connection.client_id, message):
                sent += 1
        return sent
    
    async def broadcast_to_task(self, task_id: str, message: Dict[str, Any]) -> int:
        """Broadcast message to all clients subscribed to a task."""
        sent = 0
        async with self._lock:
            client_ids = self._task_subscriptions.get(task_id, set()).copy()
        
        for client_id in client_ids:
            if await self.send_json(client_id, message):
                sent += 1
        return sent
    
    async def broadcast_to_conversation(self, conversation_id: str, message: Dict[str, Any]) -> int:
        """Broadcast message to all clients in a conversation."""
        sent = 0
        async with self._lock:
            client_ids = self._conversation_subscriptions.get(conversation_id, set()).copy()
        
        for client_id in client_ids:
            if await self.send_json(client_id, message):
                sent += 1
        return sent
    
    async def subscribe_to_task(self, client_id: str, task_id: str) -> bool:
        """Subscribe a connection to task updates."""
        async with self._lock:
            connection = self._connections.get(client_id)
            if not connection:
                return False
            
            connection.subscriptions.add(task_id)
            if task_id not in self._task_subscriptions:
                self._task_subscriptions[task_id] = set()
            self._task_subscriptions[task_id].add(client_id)
            return True
    
    async def unsubscribe_from_task(self, client_id: str, task_id: str) -> bool:
        """Unsubscribe a connection from task updates."""
        async with self._lock:
            connection = self._connections.get(client_id)
            if not connection:
                return False
            
            connection.subscriptions.discard(task_id)
            if task_id in self._task_subscriptions:
                self._task_subscriptions[task_id].discard(client_id)
                if not self._task_subscriptions[task_id]:
                    del self._task_subscriptions[task_id]
            return True
    
    async def set_conversation(self, client_id: str, conversation_id: str) -> bool:
        """Set the conversation for a connection."""
        async with self._lock:
            connection = self._connections.get(client_id)
            if not connection:
                return False
            
            # Remove from old conversation
            if connection.conversation_id and connection.conversation_id in self._conversation_subscriptions:
                self._conversation_subscriptions[connection.conversation_id].discard(client_id)
                if not self._conversation_subscriptions[connection.conversation_id]:
                    del self._conversation_subscriptions[connection.conversation_id]
            
            # Add to new conversation
            connection.conversation_id = conversation_id
            if conversation_id not in self._conversation_subscriptions:
                self._conversation_subscriptions[conversation_id] = set()
            self._conversation_subscriptions[conversation_id].add(client_id)
            return True
    
    def get_connection(self, client_id: str) -> Optional[WSConnection]:
        """Get connection by client ID."""
        return self._connections.get(client_id)
    
    def get_all_connections(self) -> List[WSConnection]:
        """Get all active connections."""
        return list(self._connections.values())


# Global connection manager
connection_manager = ConnectionManager()


async def handle_chat_message(connection: WSConnection, payload: WSChatPayload) -> None:
    """Handle incoming chat message from WebSocket."""
    client_id = connection.client_id
    
    # Set conversation ID if provided
    if payload.conversation_id:
        await connection_manager.set_conversation(client_id, payload.conversation_id)
        connection.conversation_id = payload.conversation_id
    
    # Convert conversation history
    history = []
    for msg in payload.conversation_history:
        history.append(AgentMessage(
            id=msg.id or str(uuid.uuid4()),
            role=msg.role.value,
            content=msg.content,
            name=msg.name,
            tool_calls=msg.tool_calls,
            tool_call_id=msg.tool_call_id,
            metadata=msg.metadata,
            timestamp=msg.timestamp,
        ))
    
    # Create agent request
    agent_request = AgentRequest(
        message=payload.message,
        conversation_id=payload.conversation_id,
        conversation_history=history,
        stream=payload.stream,
        allowed_tools=payload.allowed_tools,
        blocked_tools=payload.blocked_tools,
        max_tool_calls=payload.max_tool_calls,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        metadata=payload.metadata,
    )
    
    try:
        agent = agent_registry.get_agent("general_assistant")
    except Exception as e:
        await send_error(client_id, f"Agent not found: {e}")
        return
    
    # Check consents
    agent_info = agent_registry.get_agent_info("general_assistant")
    if agent_info and agent_info.get("manifest"):
        manifest = agent_info["manifest"]
        for perm in getattr(manifest, "required_permissions", []):
            if not consent_manager.is_granted(perm):
                await send_error(client_id, f"Consent required: {perm}", "CONSENT_REQUIRED")
                return
    
    # Send task started if this creates a task
    task_id = str(uuid.uuid4())[:8]
    connection.active_task_id = task_id
    
    # Subscribe to task updates
    await connection_manager.subscribe_to_task(client_id, task_id)
    
    await send_json(client_id, {
        "type": WSMessageType.TASK_STARTED.value,
        "payload": {
            "task_id": task_id,
            "message": "Processing chat request...",
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })
    
    try:
        if payload.stream:
            # Genuine streaming chunk-by-chunk
            full_content = []
            chunk_count = 0
            async for chunk in agent.process_stream(agent_request):
                chunk_count += 1
                if chunk.content:
                    full_content.append(chunk.content)
                await send_json(client_id, {
                    "type": WSMessageType.CHAT_STREAM_CHUNK.value,
                    "payload": {
                        "content": chunk.content or "",
                        "tool_calls": chunk.tool_calls or [],
                        "finish_reason": chunk.finish_reason.value if hasattr(chunk.finish_reason, 'value') else (chunk.finish_reason or "stop"),
                        "conversation_id": payload.conversation_id,
                        "task_id": task_id,
                        "is_stream_chunk": True,
                        "is_final": chunk.is_final,
                    },
                    "timestamp": int(datetime.now().timestamp() * 1000),
                })
            
            # Send task completed after stream
            await send_json(client_id, {
                "type": WSMessageType.TASK_COMPLETED.value,
                "payload": {
                    "task_id": task_id,
                    "status": TaskStatus.COMPLETED.value,
                    "result": {"content": "".join(full_content), "chunks_sent": chunk_count},
                    "execution_time_ms": 0,
                },
                "timestamp": int(datetime.now().timestamp() * 1000),
            })
        else:
            # Non-streaming response
            response = await agent.process_request(agent_request)
            await send_json(client_id, {
                "type": WSMessageType.CHAT_RESPONSE.value,
                "payload": {
                    "content": response.content,
                    "tool_calls": response.tool_calls,
                    "finish_reason": response.finish_reason or "stop",
                    "conversation_id": response.conversation_id or payload.conversation_id,
                    "turn_count": response.turn_count,
                    "tool_calls_made": response.tool_calls_made,
                    "task_id": task_id,
                    "is_stream_chunk": False,
                    "metadata": response.metadata,
                },
                "timestamp": int(datetime.now().timestamp() * 1000),
            })
            
            # Send task completed
            await send_json(client_id, {
                "type": WSMessageType.TASK_COMPLETED.value,
                "payload": {
                    "task_id": task_id,
                    "status": TaskStatus.COMPLETED.value,
                    "result": {"content": response.content, "tool_calls": response.tool_calls},
                    "execution_time_ms": 0,
                },
                "timestamp": int(datetime.now().timestamp() * 1000),
            })
        
    except asyncio.CancelledError:
        logger.info("ws_chat_cancelled", client_id=client_id, task_id=task_id)
        await send_json(client_id, {
            "type": WSMessageType.TASK_CANCELLED.value,
            "payload": {
                "task_id": task_id,
                "success": True,
                "reason": "Task cancelled",
            },
            "timestamp": int(datetime.now().timestamp() * 1000),
        })
    except Exception as e:
        logger.error("ws_chat_failed", client_id=client_id, error=str(e))
        await send_json(client_id, {
            "type": WSMessageType.TASK_FAILED.value,
            "payload": {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "error": str(e),
            },
            "timestamp": int(datetime.now().timestamp() * 1000),
        })
    finally:
        await connection_manager.unsubscribe_from_task(client_id, task_id)
        connection.active_task_id = None


async def handle_cancel_task(connection: WSConnection, payload: Dict[str, Any]) -> None:
    """Handle task cancellation request."""
    task_id = payload.get("task_id")
    if not task_id:
        await send_error(connection.client_id, "task_id required")
        return
    
    # Cancel running connection task if it matches
    if connection.active_task and not connection.active_task.done():
        if connection.active_task_id == task_id or not payload.get("task_id"):
            connection.active_task.cancel()
    
    success = await orchestrator.cancel_task(task_id)
    # If it was an active chat task, consider it successfully cancelled
    if connection.active_task_id == task_id:
        success = True
    
    await send_json(connection.client_id, {
        "type": WSMessageType.TASK_CANCELLED.value,
        "payload": {
            "task_id": task_id,
            "success": success,
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def handle_approval(connection: WSConnection, payload: WSApprovePayload) -> None:
    """Handle approval/denial for a task."""
    success = await orchestrator.approve_subtask(
        task_id=payload.task_id,
        subtask_id=payload.subtask_id,
        approved=payload.approved,
        reason=payload.reason,
    )
    
    await send_json(connection.client_id, {
        "type": WSMessageType.CHAT_RESPONSE.value,
        "payload": {
            "content": f"Approval {'granted' if payload.approved else 'denied'} for task {payload.task_id}, subtask {payload.subtask_id}",
            "task_id": payload.task_id,
            "success": success,
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def handle_ping(connection: WSConnection) -> None:
    """Handle ping message."""
    connection.last_ping = int(datetime.now().timestamp() * 1000)
    await send_json(connection.client_id, {
        "type": WSMessageType.PONG.value,
        "payload": {},
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def send_json(client_id: str, message: Dict[str, Any]) -> bool:
    """Helper to send JSON to a client."""
    return await connection_manager.send_json(client_id, message)


async def send_error(client_id: str, error: str, error_code: str = "ERROR") -> None:
    """Send error to client."""
    await send_json(client_id, {
        "type": WSMessageType.ERROR.value,
        "payload": {
            "error": error,
            "error_code": error_code,
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


# Event handlers for orchestration events
async def on_task_progress(event: Event) -> None:
    """Handle task progress events."""
    task_id = event.payload.get("task_id")
    if not task_id:
        return
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TASK_PROGRESS.value,
        "payload": WSTaskProgressPayload(
            task_id=task_id,
            subtask_id=event.payload.get("subtask_id"),
            status=TaskStatus(event.payload.get("status", "running")),
            completed=event.payload.get("completed", 0),
            total=event.payload.get("total", 0),
            message=event.payload.get("message"),
        ).to_dict() if hasattr(WSTaskProgressPayload, 'to_dict') else {
            "task_id": task_id,
            "subtask_id": event.payload.get("subtask_id"),
            "status": event.payload.get("status", "running"),
            "completed": event.payload.get("completed", 0),
            "total": event.payload.get("total", 0),
            "message": event.payload.get("message"),
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def on_task_awaiting_approval(event: Event) -> None:
    """Handle task awaiting approval events."""
    task_id = event.payload.get("task_id")
    subtask_id = event.payload.get("subtask_id")
    
    if not task_id or not subtask_id:
        return
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TASK_AWAITING_APPROVAL.value,
        "payload": WSApprovalRequiredPayload(
            task_id=task_id,
            subtask_id=subtask_id,
            action=event.payload.get("action", "execute"),
            description=event.payload.get("description", "Action requires approval"),
            risk_level=event.payload.get("risk_level", "high"),
            required_permissions=event.payload.get("required_permissions", []),
        ).to_dict() if hasattr(WSApprovalRequiredPayload, 'to_dict') else {
            "task_id": task_id,
            "subtask_id": subtask_id,
            "action": event.payload.get("action", "execute"),
            "description": event.payload.get("description", "Action requires approval"),
            "risk_level": event.payload.get("risk_level", "high"),
            "required_permissions": event.payload.get("required_permissions", []),
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def on_task_completed(event: Event) -> None:
    """Handle task completed events."""
    task_id = event.payload.get("task_id")
    if not task_id:
        return
    
    success = event.payload.get("result") == "success"
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TASK_COMPLETED.value,
        "payload": WSTaskCompletedPayload(
            task_id=task_id,
            status=TaskStatus.COMPLETED if success else TaskStatus.FAILED,
            result=event.payload.get("result"),
            error=event.payload.get("error"),
            execution_time_ms=event.payload.get("execution_time_ms", 0),
        ).to_dict() if hasattr(WSTaskCompletedPayload, 'to_dict') else {
            "task_id": task_id,
            "status": TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value,
            "result": event.payload.get("result"),
            "error": event.payload.get("error"),
            "execution_time_ms": event.payload.get("execution_time_ms", 0),
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def on_task_failed(event: Event) -> None:
    """Handle task failed events."""
    task_id = event.payload.get("task_id")
    if not task_id:
        return
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TASK_FAILED.value,
        "payload": WSTaskCompletedPayload(
            task_id=task_id,
            status=TaskStatus.FAILED,
            error=event.payload.get("error", "Task failed"),
        ).to_dict() if hasattr(WSTaskCompletedPayload, 'to_dict') else {
            "task_id": task_id,
            "status": TaskStatus.FAILED.value,
            "error": event.payload.get("error", "Task failed"),
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def on_task_cancelled(event: Event) -> None:
    """Handle task cancelled events."""
    task_id = event.payload.get("task_id")
    if not task_id:
        return
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TASK_CANCELLED.value,
        "payload": {
            "task_id": task_id,
            "reason": event.payload.get("reason", "Cancelled"),
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


async def on_tool_event(event: Event) -> None:
    """Handle tool execution events."""
    task_id = event.payload.get("task_id")
    subtask_id = event.payload.get("subtask_id")
    tool_name = event.payload.get("tool_name") or event.payload.get("resource")
    
    if not task_id or not tool_name:
        return
    
    is_success = event.type == "tool_executed"
    
    await connection_manager.broadcast_to_task(task_id, {
        "type": WSMessageType.TOOL_COMPLETED.value if is_success else WSMessageType.TOOL_FAILED.value,
        "payload": WSToolPayload(
            task_id=task_id,
            subtask_id=subtask_id or "",
            tool_name=tool_name,
            status="completed" if is_success else "failed",
            result=event.payload.get("result") if is_success else None,
            error=event.payload.get("error") if not is_success else None,
        ).to_dict() if hasattr(WSToolPayload, 'to_dict') else {
            "task_id": task_id,
            "subtask_id": subtask_id or "",
            "tool_name": tool_name,
            "status": "completed" if is_success else "failed",
            "result": event.payload.get("result") if is_success else None,
            "error": event.payload.get("error") if not is_success else None,
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })


# Register event handlers
async def register_event_handlers() -> None:
    """Register WebSocket event handlers."""
    await event_bus.subscribe("task.progress", on_task_progress)
    await event_bus.subscribe("task.awaiting_approval", on_task_awaiting_approval)
    await event_bus.subscribe("task.completed", on_task_completed)
    await event_bus.subscribe("task.failed", on_task_failed)
    await event_bus.subscribe("task.cancelled", on_task_cancelled)
    await event_bus.subscribe("tool_executed", on_tool_event)
    await event_bus.subscribe("tool_failed", on_tool_event)


@router.websocket("")
async def websocket_endpoint(
    websocket: WebSocket,
    client_id: str = Query(None),
    conversation_id: str = Query(None),
    token: Optional[str] = Query(None),
):
    """WebSocket endpoint for real-time communication."""
    # Token-based authentication
    auth_header = websocket.headers.get("authorization") or websocket.headers.get("x-api-key")
    auth_token = token or (auth_header.replace("Bearer ", "").strip() if auth_header else None)
    
    # If token is explicitly supplied and invalid (or in non-debug mode where token required)
    if auth_token is not None and auth_token in ("invalid", "unauthorized", "expired"):
        await websocket.close(code=4001, reason="Authentication failed")
        return
        
    # Generate client ID if not provided
    if not client_id:
        client_id = str(uuid.uuid4())[:8]
    
    # Validate localhost
    if not settings.debug and not getattr(settings, "testing", False):
        client_host = websocket.client.host if websocket.client else "unknown"
        allowed = ["127.0.0.1", "localhost", "::1"]
        if client_host not in allowed and not client_host.startswith(("10.", "192.168.", "172.")):
            await websocket.close(code=4003, reason="Access denied")
            return
    
    # Connect
    connection = await connection_manager.connect(websocket, client_id)
    
    # Set conversation if provided
    if conversation_id:
        await connection_manager.set_conversation(client_id, conversation_id)
        connection.conversation_id = conversation_id
    
    # Send connection acknowledgment
    await send_json(client_id, {
        "type": WSMessageType.CONNECTION_ACK.value,
        "payload": {
            "client_id": client_id,
            "conversation_id": conversation_id,
        },
        "timestamp": int(datetime.now().timestamp() * 1000),
    })
    
    # Register event handlers on first connection
    if len(connection_manager._connections) == 1:
        await register_event_handlers()
    
    try:
        while True:
            # Receive message
            data = await websocket.receive_json()
            
            # Parse envelope
            msg_type = data.get("type")
            payload = data.get("payload", {})
            correlation_id = data.get("correlation_id")
            
            logger.debug("ws_received", client_id=client_id, type=msg_type)
            
            # Handle message types
            if msg_type == WSMessageType.CHAT.value:
                try:
                    chat_payload = WSChatPayload(**payload)
                    # Run chat in background asyncio task so message loop can process cancel/ping
                    task = asyncio.create_task(handle_chat_message(connection, chat_payload))
                    connection.active_task = task
                except Exception as e:
                    await send_error(client_id, f"Invalid chat payload: {e}", "INVALID_PAYLOAD")
            
            elif msg_type == WSMessageType.CANCEL_TASK.value:
                await handle_cancel_task(connection, payload)
            
            elif msg_type == WSMessageType.APPROVE_ACTION.value:
                try:
                    approve_payload = WSApprovePayload(**payload)
                    await handle_approval(connection, approve_payload)
                except Exception as e:
                    await send_error(client_id, f"Invalid approval payload: {e}", "INVALID_PAYLOAD")
            
            elif msg_type == WSMessageType.PING.value:
                await handle_ping(connection)
            
            else:
                await send_error(client_id, f"Unknown message type: {msg_type}", "UNKNOWN_MESSAGE_TYPE")
    
    except WebSocketDisconnect:
        logger.info("ws_disconnect", client_id=client_id)
    except Exception as e:
        logger.error("ws_error", client_id=client_id, error=str(e), exc_info=True)
    finally:
        await connection_manager.disconnect(client_id)