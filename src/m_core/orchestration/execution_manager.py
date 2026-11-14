"""Execution Manager for M AI Assistant."""
import asyncio
import inspect
import structlog
from typing import Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass, field

from ..agents import agent_registry
from ..tools import tool_registry
from m_core.common_schemas import ToolInvocation, ToolResult
from ..model_providers import provider_factory
from ..model_providers.schemas import (
    CompletionRequest, ChatMessage, MessageRole,
)
from ..security import consent_manager, audit_logger, AuditEventType
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    SubtaskExecutionStatus, SubtaskExecutionRecord,
)
from .autonomous_executor import autonomous_executor
from ..planning.schemas import (
    Subtask, RoutingDecision, RoutingTargetType, ApprovalStatus,
)

logger = get_logger(__name__)


@dataclass
class ExecutionContext:
    """Context for a single execution."""
    subtask_id: str
    routing_decision: RoutingDecision
    record: SubtaskExecutionRecord
    correlation_id: str
    task_context: Any
    cancelled: bool = False
    progress_callback: Optional[Callable] = None
    cancellation_check: Optional[Callable[[], bool]] = None
    pause_waiter: Optional[Callable[[], Awaitable[None]]] = None


class ExecutionManager:
    """Manages execution of individual subtasks."""
    
    def __init__(self):
        self._active_executions: Dict[str, ExecutionContext] = {}
        self._execution_hooks: List[Callable] = []
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the execution manager."""
        if self._initialized:
            return
        self._initialized = True
        logger.info("ExecutionManager initialized")
    
    def add_execution_hook(self, hook: Callable) -> None:
        """Add a hook for execution events."""
        self._execution_hooks.append(hook)
    
    async def execute_subtask(
        self,
        subtask: Subtask,
        routing_decision: RoutingDecision,
        record: SubtaskExecutionRecord,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        pause_waiter: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Execute a single subtask."""
        context = ExecutionContext(
            subtask_id=subtask.id,
            routing_decision=routing_decision,
            record=record,
            correlation_id=correlation_id,
            task_context=task_context,
            progress_callback=progress_callback,
            cancellation_check=cancellation_check,
            pause_waiter=pause_waiter,
        )
        
        self._active_executions[subtask.id] = context
        
        try:
            # Fail-closed enforcement for subtasks needing explicit user approval
            if subtask.requires_approval and subtask.approval_status != ApprovalStatus.GRANTED:
                record.status = SubtaskExecutionStatus.AWAITING_APPROVAL
                record.error = f"Approval required before executing: {subtask.name}"
                return {
                    "success": False,
                    "awaiting_approval": True,
                    "error": record.error,
                }

            record.status = SubtaskExecutionStatus.RUNNING
            record.started_at = int(datetime.now().timestamp() * 1000)
            record.assigned_target_type = routing_decision.target_type.value
            record.assigned_target_id = routing_decision.target_id
            
            start_time = datetime.now()
            
            # Execute based on target type
            if routing_decision.target_type == RoutingTargetType.AUTONOMOUS:
                result = await self._execute_autonomous(subtask, routing_decision, context)
            elif routing_decision.target_type == RoutingTargetType.TOOL:
                result = await self._execute_tool(subtask, routing_decision, context)
            elif routing_decision.target_type == RoutingTargetType.AGENT:
                result = await self._execute_agent(subtask, routing_decision, context)
            elif routing_decision.target_type == RoutingTargetType.MODEL_PROVIDER:
                result = await self._execute_model(subtask, routing_decision, context)
            else:
                result = {"success": False, "error": f"Unknown target type: {routing_decision.target_type}"}
            
            # Update record
            execution_time = (datetime.now() - start_time).total_seconds() * 1000
            record.execution_time_ms = execution_time
            record.completed_at = int(datetime.now().timestamp() * 1000)
            
            if result.get("checkpoint_state"):
                record.metadata = {**record.metadata, "autonomous_state": result["checkpoint_state"]}
            if result.get("pending_action"):
                record.metadata = {**record.metadata, "pending_action": result["pending_action"]}
            
            if result.get("success"):
                record.status = SubtaskExecutionStatus.COMPLETED
                record.result = result.get("result")
                record.error = None
            elif result.get("awaiting_approval"):
                record.status = SubtaskExecutionStatus.AWAITING_APPROVAL
                record.error = result.get("error")
            elif result.get("cancelled"):
                record.status = SubtaskExecutionStatus.CANCELLED
                record.error = result.get("error") or "Cancelled"
            else:
                record.status = SubtaskExecutionStatus.FAILED
                record.error = result.get("error")
            
            # Call hooks
            for hook in self._execution_hooks:
                try:
                    if inspect.iscoroutinefunction(hook):
                        await hook(context, result)
                    else:
                        hook(context, result)
                except Exception as e:
                    logger.warning(f"Execution hook failed: {e}")
            
            return result
            
        finally:
            self._active_executions.pop(subtask.id, None)
    
    async def _execute_autonomous(
        self,
        subtask: Subtask,
        routing_decision: RoutingDecision,
        context: ExecutionContext,
    ) -> Dict[str, Any]:
        """Execute a goal-oriented autonomous workflow via AutonomousTaskExecutor."""
        result = await autonomous_executor.execute_subtask(
            subtask=subtask,
            task_context=context.task_context,
            correlation_id=context.correlation_id,
            progress_callback=None,  # Step events already published on event_bus
            cancellation_check=lambda: context.cancelled or bool(context.cancellation_check and context.cancellation_check()),
            pause_waiter=context.pause_waiter,
        )
        audit_logger.log(
            event_type=AuditEventType.AGENT_COMPLETED if result.get("success") else AuditEventType.AGENT_FAILED,
            resource=subtask.id,
            action=f"autonomous:{routing_decision.target_id}",
            result="success" if result.get("success") else ("awaiting_approval" if result.get("awaiting_approval") else "failure"),
            details={"error": result.get("error")} if not result.get("success") else {},
            correlation_id=context.correlation_id,
        )
        return result
    
    async def _execute_tool(
        self,
        subtask: Subtask,
        routing_decision: RoutingDecision,
        context: ExecutionContext,
    ) -> Dict[str, Any]:
        """Execute a tool."""
        tool_name = routing_decision.target_id
        tool = tool_registry.get_tool(tool_name)
        
        if not tool:
            return {"success": False, "error": f"Tool not found: {tool_name}"}
        
        # Check consent for required permissions
        if hasattr(tool.manifest, 'required_permissions'):
            for perm in tool.manifest.required_permissions:
                if not consent_manager.is_granted(perm):
                    return {"success": False, "error": f"Consent required: {perm}"}
        
        # Enforce tool-level approval even if planning missed it
        if getattr(tool.manifest, "requires_approval", False) and subtask.approval_status != ApprovalStatus.GRANTED:
            return {
                "success": False,
                "awaiting_approval": True,
                "error": f"Approval required for tool: {tool_name}",
            }
        
        # Prepare shallow copy of arguments
        raw_args = subtask.metadata.get("tool_arguments", subtask.metadata.get("arguments", {}))
        arguments = dict(raw_args) if isinstance(raw_args, dict) else {}
        if subtask.approval_status == ApprovalStatus.GRANTED:
            arguments["approved"] = True
        else:
            arguments.pop("approved", None)
        
        # Invoke tool through registry
        result = await tool_registry.invoke(
            tool_name=tool_name,
            arguments=arguments,
            correlation_id=context.correlation_id,
        )
        
        # Log audit
        audit_logger.log_tool(
            tool=tool_name,
            success=result.success,
            duration_ms=result.execution_time_ms,
            correlation_id=context.correlation_id,
        )
        
        return {
            "success": result.success,
            "result": result.result,
            "error": result.error,
        }
    
    async def _execute_agent(
        self,
        subtask: Subtask,
        routing_decision: RoutingDecision,
        context: ExecutionContext,
    ) -> Dict[str, Any]:
        """Execute an agent."""
        agent_id = routing_decision.target_id
        
        try:
            agent = agent_registry.get_agent(agent_id)
        except Exception:
            return {"success": False, "error": f"Agent not found: {agent_id}"}
        
        # Check agent permissions
        agent_info = agent_registry.get_agent_info(agent_id)
        if agent_info and agent_info.get("manifest"):
            manifest = agent_info["manifest"]
            for perm in getattr(manifest, 'required_permissions', []):
                if not consent_manager.is_granted(perm):
                    return {"success": False, "error": f"Agent consent required: {perm}"}
        
        # Prepare agent request (include upstream dependency outputs when present)
        from ..agents.schemas import AgentRequest
        
        prompt_message = subtask.description
        dep_results = subtask.metadata.get("dependency_results")
        if dep_results:
            import json as _json
            prompt_message = (
                f"{subtask.description}\n\n"
                f"Results from completed prerequisite subtasks:\n"
                f"{_json.dumps(dep_results, default=str)[:4000]}"
            )
        
        request = AgentRequest(
            message=prompt_message,
            conversation_id=context.task_context.conversation_id,
            metadata={
                "subtask_id": subtask.id,
                "task_id": context.task_context.task_id,
                "correlation_id": context.correlation_id,
            },
            allowed_tools=[subtask.assigned_tool] if subtask.assigned_tool else [],
            max_tool_calls=subtask.metadata.get("max_tool_calls", 5),
        )
        
        try:
            response = await agent.process_request(request)
        except Exception as e:
            audit_logger.log_agent(
                agent=agent_id,
                event="failed",
                correlation_id=context.correlation_id,
            )
            return {"success": False, "error": str(e)}
        
        # Log audit
        audit_logger.log_agent(
            agent=agent_id,
            event="completed",
            correlation_id=context.correlation_id,
        )
        
        return {
            "success": True,
            "result": {
                "content": response.content,
                "tool_calls": response.tool_calls,
                "usage": response.usage,
            },
        }
    
    async def _execute_model(
        self,
        subtask: Subtask,
        routing_decision: RoutingDecision,
        context: ExecutionContext,
    ) -> Dict[str, Any]:
        """Execute a model provider directly."""
        provider_id = routing_decision.target_id
        provider = provider_factory.get_provider(provider_id)
        
        if not provider:
            return {"success": False, "error": f"Provider not found: {provider_id}"}
        
        request = CompletionRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content="You are a helpful assistant."),
                ChatMessage(role=MessageRole.USER, content=subtask.description),
            ],
            temperature=subtask.metadata.get("temperature", 0.7),
            max_tokens=subtask.metadata.get("max_tokens", 4096),
        )
        
        try:
            response = await provider.complete(request)
            
            # Log audit
            audit_logger.log_model(
                provider=provider_id,
                model=provider.get_default_model(),
                success=True,
                latency_ms=response.latency_ms,
                correlation_id=context.correlation_id,
            )
            
            return {
                "success": True,
                "result": response.content,
            }
        except Exception as e:
            audit_logger.log_model(
                provider=provider_id,
                model=provider.get_default_model(),
                success=False,
                latency_ms=0,
                correlation_id=context.correlation_id,
            )
            return {"success": False, "error": str(e)}
    
    async def cancel_execution(self, subtask_id: str) -> bool:
        """Cancel an active execution."""
        context = self._active_executions.get(subtask_id)
        if context:
            context.cancelled = True
            return True
        return False
    
    def get_active_executions(self) -> List[Dict[str, Any]]:
        """Get list of active executions."""
        return [
            {
                "subtask_id": ctx.subtask_id,
                "target_type": ctx.routing_decision.target_type.value,
                "target_id": ctx.routing_decision.target_id,
                "record": ctx.record,
            }
            for ctx in self._active_executions.values()
        ]


# Global execution manager instance
execution_manager = ExecutionManager()