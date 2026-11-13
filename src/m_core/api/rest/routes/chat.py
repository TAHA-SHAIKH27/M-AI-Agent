"""Chat routes for REST API."""
import uuid
import structlog
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from typing import Optional, List, Dict, Any

from m_core.agents import agent_registry, GeneralAssistantAgent
from m_core.agents.schemas import AgentRequest, AgentMessage
from m_core.orchestration import orchestrator
from m_core.orchestration.schemas import TaskExecutionStatus, SubtaskExecutionStatus
from m_core.core.events import event_bus
from m_core.security import consent_manager
from m_core.utils.logging import get_logger

from ..schemas import (
    ChatRequest, ChatResponse, ChatMessage, ChatRole,
    TaskInfo, TaskStatus, TaskCancelRequest, TaskApproveRequest,
    ErrorResponse,
)
from ..dependencies import (
    get_client_info, validate_localhost, get_orchestrator,
    get_current_agent, get_conversation_id, APIClientInfo,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="Send a chat message",
    description="Submit a user message to the General Assistant Agent and get a response",
)
async def chat(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    conversation_id: Optional[str] = Depends(get_conversation_id),
    agent_id: str = Depends(get_current_agent),
) -> ChatResponse:
    """
    Send a chat message to the General Assistant Agent.
    
    This endpoint uses the orchestrator for complex tasks that require
    planning and tool use, or directly uses the agent for simple conversations.
    """
    # Use header conversation_id if provided, otherwise use request body
    conv_id = conversation_id or request.conversation_id
    
    logger.info(
        "chat_request",
        client=client.host,
        conversation_id=conv_id,
        message_length=len(request.message),
        stream=request.stream,
    )
    
    try:
        # Convert conversation history
        history = []
        for msg in request.conversation_history:
            # Handle both ChatMessage objects and dicts
            if hasattr(msg, 'role'):
                role = msg.role.value if hasattr(msg.role, 'value') else msg.role
                content = msg.content or ""
                msg_id = getattr(msg, 'id', None)
                name = getattr(msg, 'name', None)
                tool_calls = getattr(msg, 'tool_calls', [])
                tool_call_id = getattr(msg, 'tool_call_id', None)
                metadata = getattr(msg, 'metadata', {})
                timestamp = getattr(msg, 'timestamp', None)
            else:
                # Dict
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                msg_id = msg.get('id')
                name = msg.get('name')
                tool_calls = msg.get('tool_calls', [])
                tool_call_id = msg.get('tool_call_id')
                metadata = msg.get('metadata', {})
                timestamp = msg.get('timestamp')
            
            history.append(AgentMessage(
                id=msg_id or str(uuid.uuid4()),
                role=role,
                content=content,
                name=name,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                metadata=metadata,
                timestamp=timestamp or int(datetime.now().timestamp() * 1000),
            ))
        
        # Create agent request
        agent_request = AgentRequest(
            message=request.message,
            conversation_id=conv_id,
            conversation_history=history,
            stream=request.stream,
            allowed_tools=request.allowed_tools,
            blocked_tools=request.blocked_tools,
            max_tool_calls=request.max_tool_calls,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            metadata=request.metadata,
        )
        
        # Get the agent
        try:
            agent = agent_registry.get_agent(agent_id)
        except Exception as e:
            logger.error("agent_not_found", agent_id=agent_id, error=str(e))
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{agent_id}' not found",
            )
        
        # Check required consents
        agent_info = agent_registry.get_agent_info(agent_id)
        if agent_info and agent_info.get("manifest"):
            manifest = agent_info["manifest"]
            for perm in getattr(manifest, "required_permissions", []):
                if not consent_manager.is_granted(perm):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "error": f"Consent required: {perm}",
                            "error_code": "CONSENT_REQUIRED",
                            "capability": perm,
                        },
                    )
        
        # Process request through agent
        response = await agent.process_request(agent_request)
        
        # Convert to API response
        api_response = ChatResponse(
            content=response.content,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason or "stop",
            usage=response.usage,
            conversation_id=response.conversation_id or conv_id,
            turn_count=response.turn_count,
            tool_calls_made=response.tool_calls_made,
            metadata=response.metadata,
        )
        
        logger.info(
            "chat_response",
            client=client.host,
            conversation_id=conv_id,
            turn_count=response.turn_count,
            tool_calls_made=response.tool_calls_made,
        )
        
        return api_response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("chat_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Chat processing failed",
                "error_code": "CHAT_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/stream",
    summary="Send a chat message (streaming)",
    description="Submit a user message and receive streaming response via Server-Sent Events",
)
async def chat_stream(
    request: ChatRequest,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    conversation_id: Optional[str] = Depends(get_conversation_id),
    agent_id: str = Depends(get_current_agent),
):
    """
    Streaming chat endpoint using Server-Sent Events.
    
    Returns a text/event-stream response with chat chunks.
    """
    from fastapi.responses import StreamingResponse
    import json
    
    conv_id = conversation_id or request.conversation_id
    
    # Force streaming
    request.stream = True
    
    logger.info(
        "chat_stream_request",
        client=client.host,
        conversation_id=conv_id,
        message_length=len(request.message),
    )
    
    # Convert conversation history
    history = []
    for msg in request.conversation_history:
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
    
    agent_request = AgentRequest(
        message=request.message,
        conversation_id=conv_id,
        conversation_history=history,
        stream=True,
        allowed_tools=request.allowed_tools,
        blocked_tools=request.blocked_tools,
        max_tool_calls=request.max_tool_calls,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        metadata=request.metadata,
    )
    
    try:
        agent = agent_registry.get_agent(agent_id)
    except Exception as e:
        logger.error("agent_not_found", agent_id=agent_id, error=str(e))
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    
    # Check consents
    agent_info = agent_registry.get_agent_info(agent_id)
    if agent_info and agent_info.get("manifest"):
        manifest = agent_info["manifest"]
        for perm in getattr(manifest, "required_permissions", []):
            if not consent_manager.is_granted(perm):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": f"Consent required: {perm}",
                        "error_code": "CONSENT_REQUIRED",
                        "capability": perm,
                    },
                )
    
    async def generate_stream():
        """Generate SSE stream from agent response chunk-by-chunk."""
        try:
            async for chunk in agent.process_stream(agent_request):
                chunk_data = {
                    "content": chunk.content or "",
                    "tool_calls": chunk.tool_calls or [],
                    "finish_reason": chunk.finish_reason.value if hasattr(chunk.finish_reason, 'value') else (chunk.finish_reason or "stop"),
                    "conversation_id": conv_id,
                    "is_final": chunk.is_final,
                }
                yield f"data: {json.dumps(chunk_data)}\n\n"
        except Exception as e:
            logger.error("chat_stream_failed", error=str(e), exc_info=True)
            error_chunk = {
                "error": "Stream processing failed",
                "error_code": "STREAM_FAILED",
                "details": {"message": str(e)},
                "is_final": True,
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
    
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/task",
    response_model=TaskInfo,
    summary="Create a task",
    description="Create a new orchestrated task for complex multi-step operations",
)
async def create_task(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    conversation_id: Optional[str] = Depends(get_conversation_id),
) -> TaskInfo:
    """
    Create a new orchestrated task for complex operations.
    
    This uses the full orchestration pipeline: task understanding -> planning -> routing -> execution.
    """
    conv_id = conversation_id or request.conversation_id
    
    logger.info(
        "task_create_request",
        client=client.host,
        conversation_id=conv_id,
        message_length=len(request.message),
    )
    
    try:
        # Execute task through orchestrator
        execution_record = await orchestrator.execute_task(
            user_request=request.message,
            conversation_id=conv_id,
            user_id=client.host,
            context={
                "allowed_tools": request.allowed_tools,
                "blocked_tools": request.blocked_tools,
                "max_tool_calls": request.max_tool_calls,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "metadata": request.metadata,
            },
        )
        
        # Convert to API response
        subtasks = []
        for sr in execution_record.subtask_records:
            subtasks.append({
                "subtask_id": sr.subtask_id,
                "status": sr.status.value,
                "assigned_target_type": sr.assigned_target_type,
                "assigned_target_id": sr.assigned_target_id,
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "result": sr.result,
                "error": sr.error,
                "retry_count": sr.retry_count,
                "dependencies": sr.dependencies,
                "execution_time_ms": sr.execution_time_ms,
            })
        
        # Map status
        status_map = {
            TaskExecutionStatus.PENDING: TaskStatus.PENDING,
            TaskExecutionStatus.QUEUED: TaskStatus.QUEUED,
            TaskExecutionStatus.RUNNING: TaskStatus.RUNNING,
            TaskExecutionStatus.WAITING: TaskStatus.WAITING,
            TaskExecutionStatus.PAUSED: TaskStatus.PAUSED,
            TaskExecutionStatus.COMPLETED: TaskStatus.COMPLETED,
            TaskExecutionStatus.FAILED: TaskStatus.FAILED,
            TaskExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
            TaskExecutionStatus.REPLANNING: TaskStatus.REPLANNING,
            TaskExecutionStatus.AWAITING_APPROVAL: TaskStatus.AWAITING_APPROVAL,
        }
        
        return TaskInfo(
            task_id=execution_record.task_id,
            status=status_map.get(execution_record.status, TaskStatus.PENDING),
            conversation_id=execution_record.conversation_id,
            created_at=execution_record.created_at,
            started_at=execution_record.started_at,
            completed_at=execution_record.completed_at,
            error=execution_record.error,
            error_code=execution_record.error_code,
            subtasks=subtasks,
            result=execution_record.result,
            metadata=execution_record.metadata,
        )
        
    except Exception as e:
        logger.error("task_create_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Task creation failed",
                "error_code": "TASK_CREATE_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/task/{task_id}",
    response_model=TaskInfo,
    summary="Get task status",
    description="Retrieve the status and result of a task",
)
async def get_task_status(
    task_id: str,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Get the status and result of a task."""
    try:
        execution_record = await orchestrator.get_task_status(task_id)
        
        if not execution_record:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        # Convert subtasks
        subtasks = []
        for sr in execution_record.subtask_records:
            subtasks.append({
                "subtask_id": sr.subtask_id,
                "status": sr.status.value,
                "assigned_target_type": sr.assigned_target_type,
                "assigned_target_id": sr.assigned_target_id,
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "result": sr.result,
                "error": sr.error,
                "retry_count": sr.retry_count,
                "dependencies": sr.dependencies,
                "execution_time_ms": sr.execution_time_ms,
            })
        
        status_map = {
            TaskExecutionStatus.PENDING: TaskStatus.PENDING,
            TaskExecutionStatus.QUEUED: TaskStatus.QUEUED,
            TaskExecutionStatus.RUNNING: TaskStatus.RUNNING,
            TaskExecutionStatus.WAITING: TaskStatus.WAITING,
            TaskExecutionStatus.PAUSED: TaskStatus.PAUSED,
            TaskExecutionStatus.COMPLETED: TaskStatus.COMPLETED,
            TaskExecutionStatus.FAILED: TaskStatus.FAILED,
            TaskExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
            TaskExecutionStatus.REPLANNING: TaskStatus.REPLANNING,
            TaskExecutionStatus.AWAITING_APPROVAL: TaskStatus.AWAITING_APPROVAL,
        }
        
        return TaskInfo(
            task_id=execution_record.task_id,
            status=status_map.get(execution_record.status, TaskStatus.PENDING),
            conversation_id=execution_record.conversation_id,
            created_at=execution_record.created_at,
            started_at=execution_record.started_at,
            completed_at=execution_record.completed_at,
            error=execution_record.error,
            error_code=execution_record.error_code,
            subtasks=subtasks,
            result=execution_record.result,
            metadata=execution_record.metadata,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_task_status_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get task status",
                "error_code": "TASK_STATUS_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/task/{task_id}/cancel",
    response_model=TaskInfo,
    summary="Cancel a task",
    description="Cancel a running task",
)
async def cancel_task(
    task_id: str,
    request: Optional[TaskCancelRequest] = None,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Cancel a running task."""
    try:
        success = await orchestrator.cancel_task(task_id)
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found or not cancellable",
                    "error_code": "TASK_NOT_CANCELLABLE",
                },
            )
        
        # Get updated status
        execution_record = await orchestrator.get_task_status(task_id)
        
        if not execution_record:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        subtasks = []
        for sr in execution_record.subtask_records:
            subtasks.append({
                "subtask_id": sr.subtask_id,
                "status": sr.status.value,
                "assigned_target_type": sr.assigned_target_type,
                "assigned_target_id": sr.assigned_target_id,
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "result": sr.result,
                "error": sr.error,
                "retry_count": sr.retry_count,
                "dependencies": sr.dependencies,
                "execution_time_ms": sr.execution_time_ms,
            })
        
        status_map = {
            TaskExecutionStatus.PENDING: TaskStatus.PENDING,
            TaskExecutionStatus.QUEUED: TaskStatus.QUEUED,
            TaskExecutionStatus.RUNNING: TaskStatus.RUNNING,
            TaskExecutionStatus.WAITING: TaskStatus.WAITING,
            TaskExecutionStatus.PAUSED: TaskStatus.PAUSED,
            TaskExecutionStatus.COMPLETED: TaskStatus.COMPLETED,
            TaskExecutionStatus.FAILED: TaskStatus.FAILED,
            TaskExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
            TaskExecutionStatus.REPLANNING: TaskStatus.REPLANNING,
            TaskExecutionStatus.AWAITING_APPROVAL: TaskStatus.AWAITING_APPROVAL,
        }
        
        return TaskInfo(
            task_id=execution_record.task_id,
            status=status_map.get(execution_record.status, TaskStatus.CANCELLED),
            conversation_id=execution_record.conversation_id,
            created_at=execution_record.created_at,
            started_at=execution_record.started_at,
            completed_at=execution_record.completed_at,
            error=execution_record.error,
            error_code=execution_record.error_code,
            subtasks=subtasks,
            result=execution_record.result,
            metadata=execution_record.metadata,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("cancel_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to cancel task",
                "error_code": "TASK_CANCEL_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.post(
    "/task/{task_id}/approve",
    response_model=TaskInfo,
    summary="Approve or deny a task subtask",
    description="Submit user approval or rejection for a task awaiting approval",
)
async def approve_task(
    task_id: str,
    request: TaskApproveRequest,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> TaskInfo:
    """Approve or deny a task action."""
    try:
        success = await orchestrator.approve_subtask(
            task_id=task_id,
            subtask_id=request.subtask_id,
            approved=request.approved,
            reason=request.reason,
        )
        
        if not success:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found or not awaiting approval",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        execution_record = await orchestrator.get_task_status(task_id)
        if not execution_record:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Task '{task_id}' not found",
                    "error_code": "TASK_NOT_FOUND",
                },
            )
        
        subtasks = []
        for sr in execution_record.subtask_records:
            subtasks.append({
                "subtask_id": sr.subtask_id,
                "status": sr.status.value,
                "assigned_target_type": sr.assigned_target_type,
                "assigned_target_id": sr.assigned_target_id,
                "started_at": sr.started_at,
                "completed_at": sr.completed_at,
                "result": sr.result,
                "error": sr.error,
                "retry_count": sr.retry_count,
                "dependencies": sr.dependencies,
                "execution_time_ms": sr.execution_time_ms,
            })
        
        status_map = {
            TaskExecutionStatus.PENDING: TaskStatus.PENDING,
            TaskExecutionStatus.QUEUED: TaskStatus.QUEUED,
            TaskExecutionStatus.RUNNING: TaskStatus.RUNNING,
            TaskExecutionStatus.WAITING: TaskStatus.WAITING,
            TaskExecutionStatus.PAUSED: TaskStatus.PAUSED,
            TaskExecutionStatus.COMPLETED: TaskStatus.COMPLETED,
            TaskExecutionStatus.FAILED: TaskStatus.FAILED,
            TaskExecutionStatus.CANCELLED: TaskStatus.CANCELLED,
            TaskExecutionStatus.REPLANNING: TaskStatus.REPLANNING,
            TaskExecutionStatus.AWAITING_APPROVAL: TaskStatus.AWAITING_APPROVAL,
        }
        
        return TaskInfo(
            task_id=execution_record.task_id,
            status=status_map.get(execution_record.status, TaskStatus.RUNNING if request.approved else TaskStatus.FAILED),
            conversation_id=execution_record.conversation_id,
            created_at=execution_record.created_at,
            started_at=execution_record.started_at,
            completed_at=execution_record.completed_at,
            error=execution_record.error,
            error_code=execution_record.error_code,
            subtasks=subtasks,
            result=execution_record.result,
            metadata=execution_record.metadata,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("approve_task_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to process task approval",
                "error_code": "TASK_APPROVAL_FAILED",
                "details": {"message": str(e)},
            },
        )