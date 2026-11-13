"""Execution Orchestrator for M AI Assistant."""
import asyncio
import uuid
import time
import structlog
from typing import Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass, field

from ..model_providers import provider_factory
from ..agents import agent_registry
from ..tools import tool_registry
from ..memory import memory_manager
from ..memory.base import MemoryTier
from ..security import consent_manager, audit_logger, AuditEventType
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    TaskExecutionStatus, SubtaskExecutionStatus,
    TaskExecutionRecord, SubtaskExecutionRecord,
    Checkpoint, CheckpointType,
    RecoveryAction, RecoveryPlan,
    EvaluationResult,
)
from ..planning import (
    planner, task_understanding,
    TaskAnalysis, ExecutionPlan, RoutingPlan, Subtask, TaskContext,
    TaskType, TaskComplexity,
)
from ..planning.router_enhanced import enhanced_router
from ..planning.schemas import ApprovalStatus

logger = get_logger(__name__)


@dataclass
class OrchestrationContext:
    """Context for orchestration execution."""
    task_id: str
    execution_plan: ExecutionPlan
    routing_plan: RoutingPlan
    task_context: TaskContext
    execution_record: TaskExecutionRecord
    subtask_records: Dict[str, SubtaskExecutionRecord]
    checkpoints: List[Checkpoint]
    cancelled: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    approval_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        # Execution starts unpaused; approval waits use a separate event.
        self.pause_event.set()


class ExecutionOrchestrator:
    """Central orchestrator for managing complete task execution."""
    
    def __init__(
        self,
        scheduler: Optional["TaskScheduler"] = None,
        state_manager: Optional["ExecutionStateManager"] = None,
        recovery_manager: Optional["FailureRecoveryManager"] = None,
        evaluator: Optional["ResultEvaluator"] = None,
    ):
        self._scheduler = scheduler
        self._state_manager = state_manager
        self._recovery_manager = recovery_manager
        self._evaluator = evaluator
        self._active_tasks: Dict[str, OrchestrationContext] = {}
        self._cancelled_task_ids: set[str] = set()
        self._execution_hooks: List[Callable] = []
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the orchestrator and dependencies."""
        if self._initialized:
            return
        
        from .scheduler import TaskScheduler
        from .state_manager import state_manager as shared_state_manager
        from .recovery import FailureRecoveryManager
        from .evaluator import ResultEvaluator
        
        self._scheduler = self._scheduler or TaskScheduler()
        # Share the same manager instance used by task-board API routes.
        self._state_manager = self._state_manager or shared_state_manager
        self._recovery_manager = self._recovery_manager or FailureRecoveryManager()
        self._evaluator = self._evaluator or ResultEvaluator()
        
        await self._scheduler.initialize()
        await self._state_manager.initialize()
        await self._recovery_manager.initialize()
        await self._evaluator.initialize()
        
        self._initialized = True
        logger.info("ExecutionOrchestrator initialized")
    
    def add_execution_hook(self, hook: Callable) -> None:
        """Add a hook for execution events."""
        self._execution_hooks.append(hook)
    
    async def execute_task(
        self,
        user_request: str,
        conversation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
    ) -> TaskExecutionRecord:
        """Execute a complete user task from request to final result."""
        await self.initialize()
        
        task_id = task_id or str(uuid.uuid4())[:8]
        correlation_id = str(uuid.uuid4())
        
        # Create task context
        task_context = TaskContext(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            session_data=context or {},
        )
        
        # Reuse the queued record created by the asynchronous task API when present.
        execution_record = await self._state_manager.get_execution_record(task_id)
        if execution_record is None:
            execution_record = TaskExecutionRecord(
                task_id=task_id,
                execution_plan_id="",
                conversation_id=conversation_id,
                user_id=user_id,
                status=TaskExecutionStatus.PENDING,
            )
        else:
            execution_record.execution_plan_id = ""
            execution_record.conversation_id = conversation_id
            execution_record.user_id = user_id
            if execution_record.status == TaskExecutionStatus.CANCELLED or task_id in self._cancelled_task_ids:
                self._cancelled_task_ids.discard(task_id)
                return execution_record
            execution_record.status = TaskExecutionStatus.QUEUED
            execution_record.error = None
            execution_record.completed_at = None

        # Publish task created event
        await event_bus.publish_sync(
            "task.created",
            "orchestrator",
            {"task_id": task_id, "conversation_id": conversation_id},
            correlation_id=correlation_id
        )
        
        try:
            # Phase 1: Task Understanding
            analysis = await task_understanding.analyze(user_request, context)
            
            # Phase 2: Planning
            execution_plan = await planner.create_plan(analysis, task_context, user_request)
            
            # Phase 3: Routing
            routing_plan = await enhanced_router.create_routing_plan(execution_plan)

            # Close the race where cancellation arrives while planning is in progress.
            if task_id in self._cancelled_task_ids:
                self._cancelled_task_ids.discard(task_id)
                execution_record.status = TaskExecutionStatus.CANCELLED
                execution_record.error = "Task cancelled before execution"
                execution_record.completed_at = int(datetime.now().timestamp() * 1000)
                await self._state_manager.save_execution_record(execution_record)
                return execution_record
            
            # Update execution record with plan IDs
            execution_record.execution_plan_id = execution_plan.task_id
            execution_record.routing_plan_id = routing_plan.execution_plan_id
            execution_record.status = TaskExecutionStatus.QUEUED
            
            subtask_records = {}
            for subtask in execution_plan.subtasks:
                subtask_records[subtask.id] = SubtaskExecutionRecord(
                    subtask_id=subtask.id,
                    task_id=task_id,
                    status=SubtaskExecutionStatus.PENDING,
                    dependencies=subtask.dependencies,
                    max_retries=subtask.max_retries,
                )
            execution_record.subtask_records = list(subtask_records.values())
            
            # Check for approval requirements before execution
            approval_required = any(s.requires_approval and s.approval_status == ApprovalStatus.PENDING for s in execution_plan.subtasks)
            if approval_required:
                execution_record.status = TaskExecutionStatus.AWAITING_APPROVAL
                execution_record.completed_at = None
                
                # Save state
                orch_context = OrchestrationContext(
                    task_id=task_id,
                    execution_plan=execution_plan,
                    routing_plan=routing_plan,
                    task_context=task_context,
                    execution_record=execution_record,
                    subtask_records=subtask_records,
                    checkpoints=[],
                )
                self._active_tasks[task_id] = orch_context
                await self._state_manager.save_execution_state(orch_context)
                
                # Publish awaiting approval event
                await event_bus.publish_sync(
                    "task.awaiting_approval",
                    "orchestrator",
                    {"task_id": task_id, "subtask_count": sum(1 for s in execution_plan.subtasks if s.requires_approval)},
                    correlation_id=correlation_id
                )
                
                audit_logger.log(
                    event_type=AuditEventType.AGENT_COMPLETED,
                    resource=task_id,
                    action="execute_task",
                    result="awaiting_approval",
                    details={"approval_required": True},
                    correlation_id=correlation_id,
                )
                
                return execution_record
            
            # Create orchestration context
            orch_context = OrchestrationContext(
                task_id=task_id,
                execution_plan=execution_plan,
                routing_plan=routing_plan,
                task_context=task_context,
                execution_record=execution_record,
                subtask_records=subtask_records,
                checkpoints=[],
            )
            
            self._active_tasks[task_id] = orch_context
            
            # Save initial state
            await self._state_manager.save_execution_state(orch_context)
            
            # Create initial checkpoint
            await self._create_checkpoint(orch_context, CheckpointType.PRE_EXECUTION)
            
            # Execute the plan
            await self._execute_plan(orch_context, correlation_id)

            # Cancellation is cooperative at scheduler boundaries. Do not let
            # the evaluator turn a cancelled partial run into "completed".
            if orch_context.cancelled:
                execution_record.status = TaskExecutionStatus.CANCELLED
                execution_record.error = "Task cancelled"
                execution_record.completed_at = int(datetime.now().timestamp() * 1000)
                await self._state_manager.save_execution_state(orch_context)
                await self._create_checkpoint(orch_context, CheckpointType.POST_EXECUTION)
                return execution_record

            # Final evaluation
            evaluation = await self._evaluator.evaluate_task(orch_context)
            
            # Update final status
            execution_record.status = self._evaluation_to_status(evaluation.overall_result)
            execution_record.result = evaluation.consolidated_result
            execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            
            # Save final state
            await self._state_manager.save_execution_state(orch_context)
            
            # Create final checkpoint
            await self._create_checkpoint(orch_context, CheckpointType.POST_EXECUTION)
            
            # Publish completion event
            await event_bus.publish_sync(
                "task.completed" if evaluation.overall_result == EvaluationResult.SUCCESS else "task.failed",
                "orchestrator",
                {
                    "task_id": task_id,
                    "result": evaluation.overall_result.value,
                    "execution_time_ms": evaluation.execution_time_ms,
                },
                correlation_id=correlation_id
            )
            
            # Log audit
            audit_logger.log(
                event_type=AuditEventType.AGENT_COMPLETED if evaluation.overall_result == EvaluationResult.SUCCESS else AuditEventType.AGENT_FAILED,
                resource=task_id,
                action="execute_task",
                result="success" if evaluation.overall_result == EvaluationResult.SUCCESS else "failure",
                details={"evaluation": evaluation.overall_result.value},
                correlation_id=correlation_id,
            )
            
            return execution_record
            
        except asyncio.CancelledError:
            execution_record.status = TaskExecutionStatus.CANCELLED
            execution_record.error = "Task cancelled"
            execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            
            # Save state if orch_context exists
            try:
                if 'orch_context' in locals() and orch_context is not None:
                    await self._state_manager.save_execution_state(orch_context)
            except Exception:
                pass
            
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": task_id},
                correlation_id=correlation_id
            )
            raise
            
        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            execution_record.status = TaskExecutionStatus.FAILED
            execution_record.error = str(e)
            execution_record.error_code = type(e).__name__
            execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            
            # Save state if orch_context exists
            try:
                if 'orch_context' in locals() and orch_context is not None:
                    await self._state_manager.save_execution_state(orch_context)
            except Exception:
                pass
            
            await event_bus.publish_sync(
                "task.failed",
                "orchestrator",
                {"task_id": task_id, "error": str(e)},
                correlation_id=correlation_id
            )
            
            audit_logger.log(
                event_type=AuditEventType.AGENT_FAILED,
                resource=task_id,
                action="execute_task",
                result="failure",
                details={"error": str(e)},
                correlation_id=correlation_id,
            )
            raise
            
        finally:
            # Approval-pending contexts must remain available for the approval
            # handler; terminal and completed executions can be released.
            active_context = self._active_tasks.get(task_id)
            if not active_context or active_context.execution_record.status != TaskExecutionStatus.AWAITING_APPROVAL:
                self._active_tasks.pop(task_id, None)
    
    async def _execute_plan(
        self,
        context: OrchestrationContext,
        correlation_id: str,
    ) -> None:
        """Execute the full plan using the scheduler."""
        execution_record = context.execution_record
        execution_record.status = TaskExecutionStatus.RUNNING
        execution_record.started_at = int(datetime.now().timestamp() * 1000)
        
        await event_bus.publish_sync(
            "task.started",
            "orchestrator",
            {"task_id": context.task_id, "subtask_count": len(context.execution_plan.subtasks)},
            correlation_id=correlation_id
        )
        
        # Execute subtasks via scheduler
        results = await self._scheduler.execute_plan(
            execution_plan=context.execution_plan,
            routing_plan=context.routing_plan,
            subtask_records=context.subtask_records,
            task_context=context.task_context,
            correlation_id=correlation_id,
            progress_callback=lambda s, r: self._on_subtask_progress(context, s, r, correlation_id),
            cancellation_check=lambda: context.cancelled,
            pause_event=context.pause_event,
        )
        
        # Update records with results
        for subtask_id, result in results.items():
            if subtask_id in context.subtask_records:
                record = context.subtask_records[subtask_id]
                record.status = SubtaskExecutionStatus.COMPLETED if result.get("success") else SubtaskExecutionStatus.FAILED
                record.result = result.get("result")
                record.error = result.get("error")
                record.completed_at = int(datetime.now().timestamp() * 1000)
        
        execution_record.subtask_records = list(context.subtask_records.values())
        
        # Create checkpoint after execution
        await self._create_checkpoint(context, CheckpointType.POST_EXECUTION)
    
    async def _on_subtask_progress(
        self,
        context: OrchestrationContext,
        subtask_id: str,
        record: SubtaskExecutionRecord,
        correlation_id: str,
    ) -> None:
        """Handle subtask progress updates."""
        # Update execution record
        context.execution_record.subtask_records = list(context.subtask_records.values())
        
        # Save state periodically
        await self._state_manager.save_execution_state(context)
        
        # Create checkpoint for important subtasks
        subtask = next((s for s in context.execution_plan.subtasks if s.id == subtask_id), None)
        if subtask and (subtask.requires_approval or len(subtask.dependencies) > 0):
            await self._create_checkpoint(context, CheckpointType.AUTOMATIC)
        
        # Publish progress event
        await event_bus.publish_sync(
            "task.progress",
            "orchestrator",
            {
                "task_id": context.task_id,
                "subtask_id": subtask_id,
                "status": record.status.value,
                "completed": sum(1 for r in context.subtask_records.values() if r.status == SubtaskExecutionStatus.COMPLETED),
                "total": len(context.subtask_records),
            },
            correlation_id=correlation_id
        )
        
        # Check for approval requirements
        if subtask and subtask.requires_approval and subtask.approval_status == ApprovalStatus.PENDING:
            await self._handle_approval(context, subtask_id, correlation_id)
    
    async def _handle_approval(
        self,
        context: OrchestrationContext,
        subtask_id: str,
        correlation_id: str,
    ) -> None:
        """Handle approval requirement for a subtask."""
        context.execution_record.status = TaskExecutionStatus.AWAITING_APPROVAL
        subtask = next((s for s in context.execution_plan.subtasks if s.id == subtask_id), None)
        
        await event_bus.publish_sync(
            "task.awaiting_approval",
            "orchestrator",
            {
                "task_id": context.task_id,
                "subtask_id": subtask_id,
                "action": getattr(subtask, "name", "execute") if subtask else "execute",
                "description": getattr(subtask, "description", "Action requires approval") if subtask else "Action requires approval",
                "risk_level": "high",
            },
            correlation_id=correlation_id
        )
        
        await self._state_manager.save_execution_state(context)
        
        # If approval is still pending, wait for explicit approval
        if subtask and subtask.approval_status == ApprovalStatus.PENDING:
            context.approval_event.clear()
            try:
                await asyncio.wait_for(context.approval_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                subtask.approval_status = ApprovalStatus.DENIED
                context.execution_record.status = TaskExecutionStatus.FAILED
                context.execution_record.error = "Approval timed out"
                return
        
        if subtask and subtask.approval_status == ApprovalStatus.GRANTED:
            context.execution_record.status = TaskExecutionStatus.RUNNING
        else:
            context.execution_record.status = TaskExecutionStatus.FAILED
    
    async def _create_checkpoint(
        self,
        context: OrchestrationContext,
        checkpoint_type: CheckpointType,
    ) -> Checkpoint:
        """Create an execution checkpoint."""
        checkpoint = Checkpoint(
            checkpoint_id=str(uuid.uuid4())[:8],
            task_id=context.task_id,
            checkpoint_type=checkpoint_type,
            execution_state={
                "status": context.execution_record.status.value,
                "started_at": context.execution_record.started_at,
            },
            subtask_states={
                sid: {
                    "status": r.status.value,
                    "result": r.result,
                    "error": r.error,
                    "retry_count": r.retry_count,
                }
                for sid, r in context.subtask_records.items()
            },
            completed_subtasks=[
                sid for sid, r in context.subtask_records.items()
                if r.status == SubtaskExecutionStatus.COMPLETED
            ],
            failed_subtasks=[
                sid for sid, r in context.subtask_records.items()
                if r.status == SubtaskExecutionStatus.FAILED
            ],
        )
        
        context.checkpoints.append(checkpoint)
        await self._state_manager.save_checkpoint(checkpoint)
        
        return checkpoint
    
    def _evaluation_to_status(self, evaluation: EvaluationResult) -> TaskExecutionStatus:
        """Convert evaluation result to execution status."""
        mapping = {
            EvaluationResult.SUCCESS: TaskExecutionStatus.COMPLETED,
            EvaluationResult.PARTIAL: TaskExecutionStatus.COMPLETED,
            EvaluationResult.FAILED: TaskExecutionStatus.FAILED,
            EvaluationResult.INCOMPLETE: TaskExecutionStatus.FAILED,
            EvaluationResult.NEEDS_REVIEW: TaskExecutionStatus.COMPLETED,
        }
        return mapping.get(evaluation, TaskExecutionStatus.FAILED)
    
    async def pause_task(self, task_id: str) -> bool:
        """Pause a running task between subtask boundaries."""
        context = self._active_tasks.get(task_id)
        if not context or context.execution_record.status != TaskExecutionStatus.RUNNING:
            return False
        context.pause_event.clear()
        context.execution_record.status = TaskExecutionStatus.PAUSED
        checkpoint = await self._create_checkpoint(context, CheckpointType.MANUAL)
        await self._state_manager.save_execution_state(context)
        await event_bus.publish_sync(
            "task.paused",
            "orchestrator",
            {
                "task_id": task_id,
                "status": TaskExecutionStatus.PAUSED.value,
                "checkpoint_id": checkpoint.checkpoint_id,
            },
        )
        return True

    async def resume_task(self, task_id: str) -> bool:
        """Resume a task previously paused by the user."""
        context = self._active_tasks.get(task_id)
        if not context or context.execution_record.status != TaskExecutionStatus.PAUSED:
            return False
        context.execution_record.status = TaskExecutionStatus.RUNNING
        context.pause_event.set()
        await self._state_manager.save_execution_state(context)
        await event_bus.publish_sync(
            "task.resumed",
            "orchestrator",
            {"task_id": task_id, "status": TaskExecutionStatus.RUNNING.value},
        )
        return True

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running, paused, or approval-waiting task."""
        context = self._active_tasks.get(task_id)
        if context:
            context.cancelled = True
            context.execution_record.status = TaskExecutionStatus.CANCELLED
            context.pause_event.set()
            context.approval_event.set()
            await self._state_manager.save_execution_state(context)
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": task_id, "status": TaskExecutionStatus.CANCELLED.value},
            )
            return True

        # A task may still be queued before its background worker has created
        # an orchestration context. Persist cancellation so the worker exits
        # before planning or executing any actions.
        record = await self._state_manager.get_execution_record(task_id)
        if record and record.status in (TaskExecutionStatus.PENDING, TaskExecutionStatus.QUEUED):
            self._cancelled_task_ids.add(task_id)
            record.status = TaskExecutionStatus.CANCELLED
            record.completed_at = int(datetime.now().timestamp() * 1000)
            await self._state_manager.save_execution_record(record)
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": task_id, "status": TaskExecutionStatus.CANCELLED.value},
            )
            return True
        return False

    async def approve_subtask(
        self,
        task_id: str,
        subtask_id: Optional[str] = None,
        approved: bool = True,
        reason: Optional[str] = None,
    ) -> bool:
        """Process user approval or denial for a task/subtask."""
        context = self._active_tasks.get(task_id)
        if not context:
            record = await self._state_manager.get_execution_record(task_id)
            if not record:
                return False
            if not approved:
                record.status = TaskExecutionStatus.FAILED
                record.error = f"Approval denied: {reason or 'User denied action'}"
                record.completed_at = int(datetime.now().timestamp() * 1000)
                await self._state_manager.save_execution_record(record)
                return True
            return False
        
        subtasks = context.execution_plan.subtasks
        targets = [s for s in subtasks if (subtask_id is None or s.id == subtask_id) and s.requires_approval]
        if not targets and subtask_id is None:
            targets = [s for s in subtasks if s.requires_approval]
        
        if not targets:
            return False
        
        for st in targets:
            if approved:
                st.approval_status = ApprovalStatus.GRANTED
                audit_logger.log_consent(
                    capability=f"subtask:{st.id}",
                    granted=True,
                    scope="task",
                    task_id=task_id,
                )
            else:
                st.approval_status = ApprovalStatus.DENIED
                if st.id in context.subtask_records:
                    context.subtask_records[st.id].status = SubtaskExecutionStatus.FAILED
                    context.subtask_records[st.id].error = f"Approval denied: {reason or 'User denied action'}"
                audit_logger.log_consent(
                    capability=f"subtask:{st.id}",
                    granted=False,
                    scope="task",
                    task_id=task_id,
                    reason=reason,
                )
        
        if approved:
            still_pending = any(
                s.requires_approval and s.approval_status == ApprovalStatus.PENDING
                for s in subtasks
            )
            if not still_pending:
                context.execution_record.status = TaskExecutionStatus.RUNNING
                context.execution_record.completed_at = None
                context.approval_event.set()
                correlation_id = str(uuid.uuid4())
                try:
                    await self._execute_plan(context, correlation_id)
                    evaluation = await self._evaluator.evaluate_task(context)
                    context.execution_record.status = self._evaluation_to_status(evaluation.overall_result)
                    context.execution_record.result = evaluation.consolidated_result
                    context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
                    await self._state_manager.save_execution_state(context)
                    await self._create_checkpoint(context, CheckpointType.POST_EXECUTION)
                    await event_bus.publish_sync(
                        "task.completed",
                        "orchestrator",
                        {"task_id": task_id, "status": context.execution_record.status.value},
                        correlation_id=correlation_id,
                    )
                except Exception as exc:
                    context.execution_record.status = TaskExecutionStatus.FAILED
                    context.execution_record.error = str(exc)
                    context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
                    await self._state_manager.save_execution_state(context)
                    logger.exception("Failed to resume approved task", task_id=task_id, error=str(exc))
                    return False
        else:
            context.execution_record.status = TaskExecutionStatus.FAILED
            context.execution_record.error = f"Action denied: {reason or 'User denied action'}"
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            context.cancelled = True
            context.approval_event.set()
            await event_bus.publish_sync(
                "task.failed",
                "orchestrator",
                {"task_id": task_id, "error": context.execution_record.error},
            )
        
        await self._state_manager.save_execution_state(context)
        return True
    
    async def get_task_status(self, task_id: str) -> Optional[TaskExecutionRecord]:
        """Get status of a task."""
        # Check active tasks
        if task_id in self._active_tasks:
            return self._active_tasks[task_id].execution_record
        
        # Check persisted state
        return await self._state_manager.get_execution_record(task_id)
    
    async def resume_from_checkpoint(self, checkpoint_id: str) -> Optional[TaskExecutionRecord]:
        """Resume a paused in-process task after validating its persisted checkpoint.

        The active scheduler context is required because execution plans are not
        yet persisted as restart-recoverable snapshots.
        """
        checkpoint = await self._state_manager.get_checkpoint(checkpoint_id)
        if not checkpoint:
            return None

        context = self._active_tasks.get(checkpoint.task_id)
        if not context:
            # Do not claim a restart recovery that cannot be reconstructed safely.
            return None
        if context.execution_record.status != TaskExecutionStatus.PAUSED:
            return None
        if not context.checkpoints or context.checkpoints[-1].checkpoint_id != checkpoint_id:
            return None

        context.execution_record.status = TaskExecutionStatus.RUNNING
        context.pause_event.set()
        await self._state_manager.save_execution_state(context)
        await event_bus.publish_sync(
            "task.resumed",
            "orchestrator",
            {
                "task_id": checkpoint.task_id,
                "checkpoint_id": checkpoint_id,
                "status": TaskExecutionStatus.RUNNING.value,
            },
        )
        return context.execution_record


# Global orchestrator instance
orchestrator = ExecutionOrchestrator()