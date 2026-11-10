"""Task Scheduler for M AI Assistant."""
import asyncio
import time
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
    TaskExecutionStatus, SubtaskExecutionStatus,
    SubtaskExecutionRecord, SchedulerConfig,
)
from ..planning.schemas import (
    ExecutionPlan, RoutingPlan, Subtask, RoutingDecision,
    RoutingTargetType, ExecutionStrategy,
)

logger = get_logger(__name__)


@dataclass
class ScheduledTask:
    """A task scheduled for execution."""
    subtask: Subtask
    routing_decision: RoutingDecision
    record: SubtaskExecutionRecord
    future: Optional[asyncio.Future] = None
    dependencies_met: bool = False


class TaskScheduler:
    """Dependency-aware task scheduler with concurrency control."""
    
    def __init__(self, config: Optional[SchedulerConfig] = None):
        self.config = config or SchedulerConfig()
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._completed_tasks: Dict[str, Dict[str, Any]] = {}
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the scheduler."""
        if self._initialized:
            return
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._initialized = True
        logger.info("TaskScheduler initialized")
    
    async def shutdown(self) -> None:
        """Shutdown the scheduler."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("TaskScheduler shutdown")
    
    async def execute_plan(
        self,
        execution_plan: ExecutionPlan,
        routing_plan: RoutingPlan,
        subtask_records: Dict[str, SubtaskExecutionRecord],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable[[str, SubtaskExecutionRecord], Awaitable[None]]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute a plan with dependency-aware scheduling."""
        
        # Build routing decision map
        routing_map = {d.subtask_id: d for d in routing_plan.decisions}
        
        # Create scheduled tasks
        scheduled_tasks = {}
        for subtask in execution_plan.subtasks:
            routing_decision = routing_map.get(subtask.id)
            if not routing_decision:
                # Create default routing
                routing_decision = RoutingDecision(
                    subtask_id=subtask.id,
                    target_type=RoutingTargetType.AGENT,
                    target_id="general_assistant",
                    confidence=0.5,
                    reasoning="Default routing",
                )
            
            record = subtask_records[subtask.id]
            scheduled_tasks[subtask.id] = ScheduledTask(
                subtask=subtask,
                routing_decision=routing_decision,
                record=record,
            )
        
        # Execute based on strategy
        if execution_plan.strategy == ExecutionStrategy.SEQUENTIAL:
            return await self._execute_sequential(
                scheduled_tasks, execution_plan, task_context, correlation_id,
                progress_callback, cancellation_check
            )
        elif execution_plan.strategy == ExecutionStrategy.PARALLEL:
            return await self._execute_parallel(
                scheduled_tasks, execution_plan, task_context, correlation_id,
                progress_callback, cancellation_check
            )
        else:  # DEPENDENCY_AWARE
            return await self._execute_dependency_aware(
                scheduled_tasks, execution_plan, task_context, correlation_id,
                progress_callback, cancellation_check
            )
    
    async def _execute_sequential(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute subtasks sequentially."""
        results = {}
        
        # Sort by priority (highest first)
        sorted_tasks = sorted(
            scheduled_tasks.values(),
            key=lambda t: -t.subtask.priority
        )
        
        for scheduled in sorted_tasks:
            if cancellation_check and cancellation_check():
                break
            
            result = await self._execute_subtask(
                scheduled, task_context, correlation_id, progress_callback
            )
            results[scheduled.subtask.id] = result
            self._completed_tasks[scheduled.subtask.id] = result
        
        return results
    
    async def _execute_parallel(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute independent subtasks in parallel."""
        # Group by dependency level
        levels = self._compute_dependency_levels(scheduled_tasks)
        results = {}
        
        for level in levels:
            if cancellation_check and cancellation_check():
                break
            
            # Execute all tasks in this level in parallel
            tasks = [scheduled_tasks[tid] for tid in level]
            
            async def execute_with_semaphore(scheduled):
                async with self._semaphore:
                    return await self._execute_subtask(
                        scheduled, task_context, correlation_id, progress_callback
                    )
            
            level_results = await asyncio.gather(
                *[execute_with_semaphore(t) for t in tasks],
                return_exceptions=True
            )
            
            for scheduled, result in zip(tasks, level_results):
                if isinstance(result, Exception):
                    result = {"success": False, "error": str(result)}
                results[scheduled.subtask.id] = result
                self._completed_tasks[scheduled.subtask.id] = result
        
        return results
    
    async def _execute_dependency_aware(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute subtasks respecting dependencies with max concurrency."""
        results = {}
        pending = set(scheduled_tasks.keys())
        running: Dict[str, asyncio.Task] = {}
        
        while pending or running:
            if cancellation_check and cancellation_check():
                # Cancel running tasks
                for task in running.values():
                    task.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                break
            
            # Check for completed running tasks
            done = []
            for tid, task in running.items():
                if task.done():
                    done.append(tid)
            
            for tid in done:
                task = running.pop(tid)
                try:
                    result = await task
                    results[tid] = result
                    self._completed_tasks[tid] = result
                except Exception as e:
                    results[tid] = {"success": False, "error": str(e)}
                    self._completed_tasks[tid] = {"success": False, "error": str(e)}
                
                pending.discard(tid)
                
                if progress_callback:
                    scheduled = scheduled_tasks[tid]
                    await progress_callback(tid, scheduled.record)
            
            # Start new tasks whose dependencies are met
            ready = []
            for tid in list(pending):
                scheduled = scheduled_tasks[tid]
                deps_met = all(
                    dep in self._completed_tasks and self._completed_tasks[dep].get("success")
                    for dep in scheduled.subtask.dependencies
                )
                if deps_met:
                    ready.append(tid)
            
            # Sort ready tasks by priority
            ready.sort(key=lambda tid: -scheduled_tasks[tid].subtask.priority)
            
            # Start up to max_concurrency tasks
            available_slots = self.config.max_concurrency - len(running)
            for tid in ready[:available_slots]:
                scheduled = scheduled_tasks[tid]
                pending.remove(tid)
                
                # Create task
                task = asyncio.create_task(
                    self._execute_subtask(
                        scheduled, task_context, correlation_id, progress_callback
                    )
                )
                running[tid] = task
            
            if running and not ready:
                # Wait for at least one task to complete
                await asyncio.sleep(0.1)
        
        return results
    
    def _compute_dependency_levels(self, scheduled_tasks: Dict[str, ScheduledTask]) -> List[List[str]]:
        """Compute execution levels based on dependencies."""
        levels = []
        remaining = set(scheduled_tasks.keys())
        completed = set()
        
        while remaining:
            level = []
            for tid in list(remaining):
                scheduled = scheduled_tasks[tid]
                if all(dep in completed for dep in scheduled.subtask.dependencies):
                    level.append(tid)
            
            if not level:
                # Circular dependency or missing dependency - execute remaining
                level = list(remaining)
            
            for tid in level:
                remaining.remove(tid)
                completed.add(tid)
            
            levels.append(level)
        
        return levels
    
    async def _execute_subtask(
        self,
        scheduled: ScheduledTask,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
    ) -> Dict[str, Any]:
        """Execute a single subtask."""
        subtask = scheduled.subtask
        routing = scheduled.routing_decision
        record = scheduled.record
        
        record.status = SubtaskExecutionStatus.RUNNING
        record.started_at = int(datetime.now().timestamp() * 1000)
        record.assigned_target_type = routing.target_type.value
        record.assigned_target_id = routing.target_id
        
        start_time = time.time()
        
        try:
            # Execute based on target type
            if routing.target_type == RoutingTargetType.TOOL:
                result = await self._execute_tool(
                    routing.target_id, subtask, task_context, correlation_id
                )
            elif routing.target_type == RoutingTargetType.AGENT:
                result = await self._execute_agent(
                    routing.target_id, subtask, task_context, correlation_id
                )
            elif routing.target_type == RoutingTargetType.MODEL_PROVIDER:
                result = await self._execute_model(
                    routing.target_id, subtask, task_context, correlation_id
                )
            else:
                result = {"success": False, "error": f"Unknown target type: {routing.target_type}"}
            
            record.status = SubtaskExecutionStatus.COMPLETED if result.get("success") else SubtaskExecutionStatus.FAILED
            record.result = result.get("result")
            record.error = result.get("error")
            record.execution_time_ms = (time.time() - start_time) * 1000
            record.completed_at = int(datetime.now().timestamp() * 1000)
            
            # Log audit
            audit_logger.log_tool(
                tool=routing.target_id,
                success=result.get("success", False),
                duration_ms=record.execution_time_ms,
                correlation_id=correlation_id,
            )
            
            return result
            
        except asyncio.CancelledError:
            record.status = SubtaskExecutionStatus.CANCELLED
            record.error = "Cancelled"
            record.completed_at = int(datetime.now().timestamp() * 1000)
            raise
            
        except Exception as e:
            logger.error(f"Subtask execution failed: {e}")
            record.status = SubtaskExecutionStatus.FAILED
            record.error = str(e)
            record.execution_time_ms = (time.time() - start_time) * 1000
            record.completed_at = int(datetime.now().timestamp() * 1000)
            
            return {"success": False, "error": str(e)}
    
    async def _execute_tool(
        self,
        tool_name: str,
        subtask: Subtask,
        task_context: Any,
        correlation_id: str,
    ) -> Dict[str, Any]:
        """Execute a tool."""
        tool = tool_registry.get_tool(tool_name)
        if not tool:
            return {"success": False, "error": f"Tool not found: {tool_name}"}
        
        # Check consent
        if hasattr(tool.manifest, 'required_permissions'):
            for perm in tool.manifest.required_permissions:
                if not consent_manager.is_granted(perm):
                    return {"success": False, "error": f"Consent required: {perm}"}
        
        # Prepare arguments from subtask description or metadata
        arguments = subtask.metadata.get("arguments", {})
        
        # Invoke tool
        invocation = ToolInvocation(
            tool_name=tool_name,
            arguments=arguments,
            correlation_id=correlation_id,
        )
        
        result = await tool_registry.invoke(tool_name, arguments, correlation_id)
        
        return {
            "success": result.success,
            "result": result.result,
            "error": result.error,
        }
    
    async def _execute_agent(
        self,
        agent_id: str,
        subtask: Subtask,
        task_context: Any,
        correlation_id: str,
    ) -> Dict[str, Any]:
        """Execute an agent."""
        try:
            agent = agent_registry.get_agent(agent_id)
        except Exception:
            return {"success": False, "error": f"Agent not found: {agent_id}"}
        
        # Prepare request
        from ..agents.schemas import AgentRequest
        
        request = AgentRequest(
            message=subtask.description,
            conversation_id=task_context.conversation_id,
            metadata={
                "subtask_id": subtask.id,
                "task_id": task_context.task_id,
                "correlation_id": correlation_id,
            },
            allowed_tools=[subtask.assigned_tool] if subtask.assigned_tool else [],
        )
        
        # Execute agent
        response = await agent.process_request(request)
        
        return {
            "success": True,
            "result": {
                "content": response.content,
                "tool_calls": response.tool_calls,
            },
        }
    
    async def _execute_model(
        self,
        provider_id: str,
        subtask: Subtask,
        task_context: Any,
        correlation_id: str,
    ) -> Dict[str, Any]:
        """Execute a model provider directly."""
        provider = provider_factory.get_provider(provider_id)
        if not provider:
            return {"success": False, "error": f"Provider not found: {provider_id}"}
        
        request = CompletionRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content="You are a helpful assistant."),
                ChatMessage(role=MessageRole.USER, content=subtask.description),
            ],
            temperature=0.7,
            max_tokens=4096,
        )
        
        try:
            response = await provider.complete(request)
            return {
                "success": True,
                "result": response.content,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def _worker_loop(self) -> None:
        """Background worker loop."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler worker error: {e}")


# Global scheduler instance
scheduler = TaskScheduler()