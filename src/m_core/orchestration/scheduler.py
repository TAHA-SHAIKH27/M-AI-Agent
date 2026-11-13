"""Task Scheduler for M AI Assistant."""
import asyncio
import time
import structlog
from typing import Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass, field

from ..agents import agent_registry
from ..tools import tool_registry
from ..tools.schemas import ToolInvocation, ToolResult
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
        self._completed_tasks: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
        pause_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute a plan with dependency-aware scheduling."""
        
        # Keep dependency results isolated by task: the scheduler is shared by
        # concurrent background executions.
        task_key = getattr(execution_plan, "task_id", "scheduler-default")
        self._completed_tasks[task_key] = {}

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
        
        # Execute based on strategy and release task-local dependency state.
        try:
            if execution_plan.strategy == ExecutionStrategy.SEQUENTIAL:
                return await self._execute_sequential(
                    scheduled_tasks, execution_plan, task_context, correlation_id,
                    progress_callback, cancellation_check, pause_event
                )
            elif execution_plan.strategy == ExecutionStrategy.PARALLEL:
                return await self._execute_parallel(
                    scheduled_tasks, execution_plan, task_context, correlation_id,
                    progress_callback, cancellation_check, pause_event
                )
            return await self._execute_dependency_aware(
                scheduled_tasks, execution_plan, task_context, correlation_id,
                progress_callback, cancellation_check, pause_event
            )
        finally:
            self._completed_tasks.pop(task_key, None)

    async def _execute_sequential(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute subtasks sequentially."""
        task_key = getattr(execution_plan, "task_id", "scheduler-default")
        completed_tasks = self._completed_tasks.setdefault(task_key, {})
        results = {}
        
        # Sort by priority (highest first)
        sorted_tasks = sorted(
            scheduled_tasks.values(),
            key=lambda t: -t.subtask.priority
        )
        
        for scheduled in sorted_tasks:
            if cancellation_check and cancellation_check():
                break
            if pause_event:
                await pause_event.wait()
            
            result = await self._execute_subtask(
                scheduled, task_context, correlation_id, progress_callback
            )
            results[scheduled.subtask.id] = result
            completed_tasks[scheduled.subtask.id] = result
            if progress_callback:
                await progress_callback(scheduled.subtask.id, scheduled.record)
        
        return results
    
    async def _execute_parallel(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute independent subtasks in parallel."""
        task_key = getattr(execution_plan, "task_id", "scheduler-default")
        completed_tasks = self._completed_tasks.setdefault(task_key, {})
        # Group by dependency level. Invalid graphs must fail closed rather
        # than executing steps whose prerequisites are missing or cyclic.
        results = {}
        try:
            levels = self._compute_dependency_levels(scheduled_tasks)
        except ValueError as exc:
            for tid, scheduled in scheduled_tasks.items():
                scheduled.record.status = SubtaskExecutionStatus.FAILED
                scheduled.record.error = str(exc)
                scheduled.record.completed_at = int(datetime.now().timestamp() * 1000)
                results[tid] = {"success": False, "error": str(exc)}
                if progress_callback:
                    await progress_callback(tid, scheduled.record)
            return results

        for level in levels:
            if cancellation_check and cancellation_check():
                break
            if pause_event:
                await pause_event.wait()
            
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
                completed_tasks[scheduled.subtask.id] = result
                if progress_callback:
                    await progress_callback(scheduled.subtask.id, scheduled.record)
        
        return results
    
    async def _execute_dependency_aware(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        execution_plan: ExecutionPlan,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute subtasks respecting dependencies with max concurrency."""
        task_key = getattr(execution_plan, "task_id", "scheduler-default")
        completed_tasks = self._completed_tasks.setdefault(task_key, {})
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
            if pause_event:
                await pause_event.wait()
            
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
                    completed_tasks[tid] = result
                except Exception as e:
                    results[tid] = {"success": False, "error": str(e)}
                    completed_tasks[tid] = {"success": False, "error": str(e)}
                
                pending.discard(tid)
                
                if progress_callback:
                    scheduled = scheduled_tasks[tid]
                    await progress_callback(tid, scheduled.record)
            
            # Start new tasks whose dependencies are met
            ready = []
            for tid in list(pending):
                scheduled = scheduled_tasks[tid]
                deps_met = all(
                    dep in completed_tasks and completed_tasks[dep].get("success")
                    for dep in scheduled.subtask.dependencies
                )
                if deps_met:
                    ready.append(tid)
            
            if not ready and not running and pending:
                # No dependency can be satisfied: report a cycle or missing dependency
                # instead of spinning forever.
                for tid in list(pending):
                    scheduled = scheduled_tasks[tid]
                    scheduled.record.status = SubtaskExecutionStatus.FAILED
                    scheduled.record.error = "Unmet or cyclic subtask dependencies"
                    scheduled.record.completed_at = int(datetime.now().timestamp() * 1000)
                    failure = {"success": False, "error": scheduled.record.error}
                    results[tid] = failure
                    completed_tasks[tid] = failure
                    if progress_callback:
                        await progress_callback(tid, scheduled.record)
                pending.clear()
                break

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
            
            if running and (not ready or available_slots <= 0):
                # Avoid a busy loop while tasks are running or concurrency is full.
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
                raise ValueError("Unmet or cyclic subtask dependencies")
            
            for tid in level:
                remaining.remove(tid)
                completed.add(tid)
            
            levels.append(level)
        
        return levels
    
    def _is_retry_safe(self, scheduled: ScheduledTask) -> bool:
        """Allow retries only for idempotent or explicitly retry-safe actions."""
        if scheduled.subtask.metadata.get("retry_safe") is True:
            return True
        if scheduled.routing_decision.target_type == RoutingTargetType.MODEL_PROVIDER:
            return True
        if scheduled.routing_decision.target_type == RoutingTargetType.AGENT:
            return False
        if scheduled.routing_decision.target_type != RoutingTargetType.TOOL:
            return False
        try:
            tool = tool_registry.get_tool(scheduled.routing_decision.target_id)
            manifest = tool.manifest
            risk = getattr(manifest, "risk_level", "")
            risk_value = str(getattr(risk, "value", risk)).lower()
            return risk_value == "low" and not bool(getattr(manifest, "requires_approval", False))
        except Exception:
            return False

    @staticmethod
    def _is_retryable_error(error: Optional[str]) -> bool:
        """Avoid retrying permission, approval, validation, and lookup failures."""
        message = (error or "").lower()
        permanent_markers = (
            "consent required",
            "approval_required",
            "approval required",
            "permission denied",
            "not found",
            "invalid",
            "validation",
            "forbidden",
            "denied",
        )
        return not any(marker in message for marker in permanent_markers)

    async def _execute_subtask(
        self,
        scheduled: ScheduledTask,
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
    ) -> Dict[str, Any]:
        """Execute a subtask with bounded retries for safe, repeatable operations."""
        subtask = scheduled.subtask
        routing = scheduled.routing_decision
        record = scheduled.record

        record.status = SubtaskExecutionStatus.RUNNING
        record.started_at = int(datetime.now().timestamp() * 1000)
        record.assigned_target_type = routing.target_type.value
        record.assigned_target_id = routing.target_id

        start_time = time.time()
        retry_safe = self._is_retry_safe(scheduled)
        max_attempts = 1 + (max(0, subtask.max_retries) if retry_safe else 0)
        result: Dict[str, Any] = {"success": False, "error": "Execution did not run"}

        try:
            for attempt in range(max_attempts):
                record.retry_count = attempt
                try:
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
                        result = {
                            "success": False,
                            "error": f"Unknown target type: {routing.target_type}",
                        }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}

                if result.get("success"):
                    break
                if (
                    attempt + 1 >= max_attempts
                    or not self._is_retryable_error(result.get("error"))
                ):
                    break

                record.status = SubtaskExecutionStatus.RETRYING
                record.retry_count = attempt + 1
                await event_bus.publish_sync(
                    "task.retry",
                    "scheduler",
                    {
                        "task_id": getattr(task_context, "task_id", None),
                        "subtask_id": subtask.id,
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "error": result.get("error"),
                    },
                    correlation_id=correlation_id,
                )
                await asyncio.sleep(min(0.5 * (2 ** attempt), 4.0))

            record.status = (
                SubtaskExecutionStatus.COMPLETED
                if result.get("success")
                else SubtaskExecutionStatus.FAILED
            )
            record.result = result.get("result")
            record.error = result.get("error")
            record.execution_time_ms = (time.time() - start_time) * 1000
            record.completed_at = int(datetime.now().timestamp() * 1000)

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

        except Exception as exc:
            logger.error("Subtask execution failed", error=str(exc), subtask_id=subtask.id)
            record.status = SubtaskExecutionStatus.FAILED
            record.error = str(exc)
            record.execution_time_ms = (time.time() - start_time) * 1000
            record.completed_at = int(datetime.now().timestamp() * 1000)
            return {"success": False, "error": str(exc)}

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