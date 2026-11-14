"""Execution Orchestrator for M AI Assistant."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

import msgspec
import structlog

from ..agents import agent_registry
from ..core.events import event_bus
from ..memory import memory_manager
from ..memory.base import MemoryTier
from ..model_providers import provider_factory
from ..planning import (
    ExecutionPlan,
    RoutingPlan,
    Subtask,
    TaskAnalysis,
    TaskComplexity,
    TaskContext,
    TaskType,
    enhanced_router,
    planner,
    router,
    task_understanding,
)
from ..planning.schemas import ApprovalStatus
from ..security import AuditEventType, audit_logger, consent_manager
from ..tools import tool_registry
from ..utils.logging import get_logger
from .autonomous_executor import autonomous_executor
from .schemas import (
    Checkpoint,
    CheckpointType,
    EvaluationResult,
    RecoveryAction,
    RecoveryPlan,
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
    TaskExecutionStatus,
)

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
    paused: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        if not self.paused:
            self.pause_event.set()


class ExecutionOrchestrator:
    """Central orchestrator managing planning, routing, execution, approvals, checkpoints, pause/resume, and recovery."""

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
        self._execution_hooks: List[Callable] = []
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the orchestrator and dependencies."""
        if self._initialized:
            return

        from .evaluator import evaluator as default_evaluator
        from .recovery import recovery_manager as default_recovery
        from .scheduler import scheduler as default_scheduler
        from .state_manager import state_manager as default_state_manager

        self._scheduler = self._scheduler or default_scheduler
        self._state_manager = self._state_manager or default_state_manager
        self._recovery_manager = self._recovery_manager or default_recovery
        self._evaluator = self._evaluator or default_evaluator

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
    ) -> TaskExecutionRecord:
        """Execute a complete user task from request to final result."""
        await self.initialize()

        task_id = str(uuid.uuid4())[:8]
        correlation_id = str(uuid.uuid4())

        task_context = TaskContext(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            session_data=context or {},
        )

        execution_record = TaskExecutionRecord(
            task_id=task_id,
            execution_plan_id="",
            conversation_id=conversation_id,
            user_id=user_id,
            status=TaskExecutionStatus.PENDING,
        )

        await event_bus.publish_sync(
            "task.created",
            "orchestrator",
            {"task_id": task_id, "conversation_id": conversation_id},
            correlation_id=correlation_id,
        )

        orch_context: Optional[OrchestrationContext] = None
        try:
            # Phase 1: Task Understanding
            analysis = await task_understanding.analyze(user_request, context)

            # Phase 2: Planning
            execution_plan = await planner.create_plan(analysis, task_context, user_request)

            # Phase 3: Control-method enhanced Routing
            try:
                routing_plan = await enhanced_router.create_routing_plan(execution_plan)
            except Exception as route_err:
                logger.warning("Enhanced router fell back to base router: %s", route_err)
                routing_plan = await router.create_routing_plan(execution_plan)

            execution_record.execution_plan_id = execution_plan.task_id
            execution_record.routing_plan_id = routing_plan.execution_plan_id
            execution_record.status = TaskExecutionStatus.QUEUED

            subtask_records: Dict[str, SubtaskExecutionRecord] = {}
            for subtask in execution_plan.subtasks:
                subtask_records[subtask.id] = SubtaskExecutionRecord(
                    subtask_id=subtask.id,
                    task_id=task_id,
                    status=SubtaskExecutionStatus.PENDING,
                    dependencies=list(subtask.dependencies),
                    max_retries=subtask.max_retries,
                )
            execution_record.subtask_records = list(subtask_records.values())

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

            # Phase 4: Upfront Approval Gate (Fail-Closed)
            approval_subtasks = [
                s for s in execution_plan.subtasks
                if s.requires_approval and s.approval_status == ApprovalStatus.PENDING
            ]
            if approval_subtasks:
                for st in approval_subtasks:
                    subtask_records[st.id].status = SubtaskExecutionStatus.AWAITING_APPROVAL
                await self._suspend_for_approval(
                    orch_context,
                    pending_subtask=approval_subtasks[0],
                    correlation_id=correlation_id,
                )
                return execution_record

            # Persist initial state & pre-execution checkpoint
            await self._state_manager.save_execution_state(orch_context)
            await self._create_checkpoint(orch_context, CheckpointType.PRE_EXECUTION)

            # Execute plan and finalize
            await self._run_and_finalize(orch_context, correlation_id)
            return execution_record

        except asyncio.CancelledError:
            execution_record.status = TaskExecutionStatus.CANCELLED
            execution_record.error = "Task cancelled"
            execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            if orch_context is not None:
                try:
                    await self._state_manager.save_execution_state(orch_context)
                except Exception:
                    pass
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": task_id},
                correlation_id=correlation_id,
            )
            raise

        except Exception as e:
            logger.error(f"Task execution failed: {e}")
            execution_record.status = TaskExecutionStatus.FAILED
            execution_record.error = str(e)
            execution_record.error_code = type(e).__name__
            execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            if orch_context is not None:
                try:
                    await self._state_manager.save_execution_state(orch_context)
                except Exception:
                    pass
            await event_bus.publish_sync(
                "task.failed",
                "orchestrator",
                {"task_id": task_id, "error": str(e)},
                correlation_id=correlation_id,
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
            if task_id in self._active_tasks:
                ctx = self._active_tasks[task_id]
                if ctx.execution_record.status not in (
                    TaskExecutionStatus.AWAITING_APPROVAL,
                    TaskExecutionStatus.PAUSED,
                ):
                    self._active_tasks.pop(task_id, None)

    async def _run_and_finalize(
        self,
        context: OrchestrationContext,
        correlation_id: str,
    ) -> None:
        """Execute plan (handling mid-flight approval/pause/cancel/replan) and evaluate."""
        results = await self._execute_plan(context, correlation_id)
        halt_reason = (results.get("_halt_reason") or {}).get("reason")

        if halt_reason == "awaiting_approval":
            pending_rec = next(
                (r for r in context.subtask_records.values() if r.status == SubtaskExecutionStatus.AWAITING_APPROVAL),
                None,
            )
            pending_subtask = (
                next((s for s in context.execution_plan.subtasks if s.id == pending_rec.subtask_id), None)
                if pending_rec
                else None
            )
            await self._suspend_for_approval(
                context,
                pending_subtask=pending_subtask,
                pending_record=pending_rec,
                correlation_id=correlation_id,
            )
            return

        if context.cancelled or halt_reason == "cancelled":
            context.execution_record.status = TaskExecutionStatus.CANCELLED
            context.execution_record.error = context.execution_record.error or "Task cancelled"
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            await self._state_manager.save_execution_state(context)
            await self._create_checkpoint(context, CheckpointType.AUTOMATIC)
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": context.task_id},
                correlation_id=correlation_id,
            )
            return

        # Evaluate completed/failed plan
        evaluation = await self._evaluator.evaluate_task(context)
        context.execution_record.status = self._evaluation_to_status(evaluation.overall_result)
        context.execution_record.result = evaluation.consolidated_result
        context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)

        self._recovery_manager.reset_replan_count(context.task_id)
        await self._state_manager.save_execution_state(context)
        await self._create_checkpoint(context, CheckpointType.POST_EXECUTION)

        await event_bus.publish_sync(
            "task.completed" if evaluation.overall_result == EvaluationResult.SUCCESS else "task.failed",
            "orchestrator",
            {
                "task_id": context.task_id,
                "result": evaluation.overall_result.value,
                "execution_time_ms": evaluation.execution_time_ms,
                "summary": evaluation.summary,
            },
            correlation_id=correlation_id,
        )

        audit_logger.log(
            event_type=AuditEventType.AGENT_COMPLETED if evaluation.overall_result == EvaluationResult.SUCCESS else AuditEventType.AGENT_FAILED,
            resource=context.task_id,
            action="execute_task",
            result="success" if evaluation.overall_result == EvaluationResult.SUCCESS else "failure",
            details={"evaluation": evaluation.overall_result.value},
            correlation_id=correlation_id,
        )

    async def _execute_plan(
        self,
        context: OrchestrationContext,
        correlation_id: str,
    ) -> Dict[str, Any]:
        """Execute plan through scheduler, supporting transparent replan transitions."""
        execution_record = context.execution_record
        execution_record.status = TaskExecutionStatus.RUNNING
        if not execution_record.started_at:
            execution_record.started_at = int(datetime.now().timestamp() * 1000)

        await event_bus.publish_sync(
            "task.started",
            "orchestrator",
            {"task_id": context.task_id, "subtask_count": len(context.execution_plan.subtasks)},
            correlation_id=correlation_id,
        )

        async def _pause_waiter() -> None:
            if context.paused:
                await context.pause_event.wait()

        results: Dict[str, Any] = {}
        max_replan_hops = 3

        for _ in range(max_replan_hops + 1):
            results = await self._scheduler.execute_plan(
                execution_plan=context.execution_plan,
                routing_plan=context.routing_plan,
                subtask_records=context.subtask_records,
                task_context=context.task_context,
                correlation_id=correlation_id,
                progress_callback=lambda s, r: self._on_subtask_progress(context, s, r, correlation_id),
                cancellation_check=lambda: context.cancelled,
                pause_waiter=_pause_waiter,
                execution_record=context.execution_record,
                recovery_mgr=self._recovery_manager,
            )

            replanned_info = results.get("_replanned")
            if not replanned_info:
                break

            bundle = self._recovery_manager.pop_replanned_bundle(replanned_info.get("new_plan_id"))
            if not bundle:
                break

            new_plan, new_routing = bundle
            context.execution_plan = new_plan
            context.routing_plan = new_routing
            context.execution_record.execution_plan_id = new_plan.task_id
            context.execution_record.routing_plan_id = new_routing.execution_plan_id
            for st in new_plan.subtasks:
                if st.id not in context.subtask_records:
                    context.subtask_records[st.id] = SubtaskExecutionRecord(
                        subtask_id=st.id,
                        task_id=context.task_id,
                        status=SubtaskExecutionStatus.PENDING,
                        dependencies=list(st.dependencies),
                        max_retries=st.max_retries,
                    )
            context.execution_record.subtask_records = list(context.subtask_records.values())
            await self._state_manager.save_execution_state(context)
            await self._create_checkpoint(context, CheckpointType.AUTOMATIC)

        return results

    async def _suspend_for_approval(
        self,
        context: OrchestrationContext,
        pending_subtask: Optional[Subtask] = None,
        pending_record: Optional[SubtaskExecutionRecord] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Suspend task cleanly in AWAITING_APPROVAL state and persist checkpoint."""
        context.execution_record.status = TaskExecutionStatus.AWAITING_APPROVAL
        context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
        context.execution_record.subtask_records = list(context.subtask_records.values())

        pending_action = (
            pending_record.metadata.get("pending_action")
            if pending_record and isinstance(pending_record.metadata, dict)
            else None
        )
        action_label = (
            (pending_action or {}).get("step_goal")
            or (pending_action or {}).get("action_type")
            or (pending_subtask.name if pending_subtask else "execute")
        )
        description = (
            (pending_action or {}).get("risk_reason")
            or (pending_subtask.description if pending_subtask else "Action requires approval")
        )
        risk_level = (pending_action or {}).get("risk_level") or "high"

        await self._state_manager.save_execution_state(context)
        await self._create_checkpoint(context, CheckpointType.PRE_EXECUTION)

        await event_bus.publish_sync(
            "task.awaiting_approval",
            "orchestrator",
            {
                "task_id": context.task_id,
                "subtask_id": pending_subtask.id if pending_subtask else None,
                "action": action_label,
                "description": description,
                "risk_level": risk_level,
                "pending_action": pending_action,
            },
            correlation_id=correlation_id,
        )

        audit_logger.log(
            event_type=AuditEventType.AGENT_COMPLETED,
            resource=context.task_id,
            action="execute_task",
            result="awaiting_approval",
            details={"approval_required": True, "subtask_id": pending_subtask.id if pending_subtask else None},
            correlation_id=correlation_id,
        )

    async def _on_subtask_progress(
        self,
        context: OrchestrationContext,
        subtask_id: str,
        record: SubtaskExecutionRecord,
        correlation_id: str,
    ) -> None:
        """Handle subtask progress updates."""
        context.execution_record.subtask_records = list(context.subtask_records.values())
        await self._state_manager.save_execution_state(context)

        subtask = next((s for s in context.execution_plan.subtasks if s.id == subtask_id), None)
        if subtask and (
            subtask.requires_approval
            or len(subtask.dependencies) > 0
            or record.status == SubtaskExecutionStatus.AWAITING_APPROVAL
        ):
            await self._create_checkpoint(context, CheckpointType.AUTOMATIC)

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
            correlation_id=correlation_id,
        )

    async def _create_checkpoint(
        self,
        context: OrchestrationContext,
        checkpoint_type: CheckpointType,
    ) -> Checkpoint:
        """Create and persist an execution checkpoint including autonomous step state."""
        checkpoint = Checkpoint(
            checkpoint_id=str(uuid.uuid4())[:8],
            task_id=context.task_id,
            checkpoint_type=checkpoint_type,
            execution_state={
                "status": context.execution_record.status.value,
                "started_at": context.execution_record.started_at,
                "execution_plan_id": context.execution_plan.task_id,
                "routing_plan_id": context.routing_plan.execution_plan_id,
            },
            subtask_states={
                sid: {
                    "status": r.status.value,
                    "result": r.result,
                    "error": r.error,
                    "retry_count": r.retry_count,
                    "metadata": r.metadata,
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
        """Pause an active running task cooperatively at the next step/subtask boundary."""
        await self.initialize()
        context = self._active_tasks.get(task_id)
        if not context:
            return False
        if context.execution_record.status not in (
            TaskExecutionStatus.RUNNING,
            TaskExecutionStatus.QUEUED,
            TaskExecutionStatus.PENDING,
        ):
            return False

        context.paused = True
        context.pause_event.clear()
        context.execution_record.status = TaskExecutionStatus.PAUSED
        await self._state_manager.save_execution_state(context)
        await self._create_checkpoint(context, CheckpointType.MANUAL)
        await event_bus.publish_sync("task.paused", "orchestrator", {"task_id": task_id})
        return True

    async def resume_task(self, task_id: str) -> bool:
        """Resume a paused task (from memory or persisted checkpoint)."""
        await self.initialize()
        context = self._active_tasks.get(task_id)
        if context:
            if context.execution_record.status != TaskExecutionStatus.PAUSED:
                return False
            context.paused = False
            context.execution_record.status = TaskExecutionStatus.RUNNING
            context.pause_event.set()
            await self._state_manager.save_execution_state(context)
            await event_bus.publish_sync("task.resumed", "orchestrator", {"task_id": task_id})
            return True

        # Reconstruct from persisted state when paused across reload/restart
        restored = await self._restore_context(task_id)
        if not restored or restored.execution_record.status != TaskExecutionStatus.PAUSED:
            return False

        restored.paused = False
        restored.pause_event.set()
        restored.execution_record.status = TaskExecutionStatus.RUNNING
        self._active_tasks[task_id] = restored
        await self._state_manager.save_execution_state(restored)
        await event_bus.publish_sync("task.resumed", "orchestrator", {"task_id": task_id})
        asyncio.create_task(self._resume_execution(restored))
        return True

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running, paused, or awaiting-approval task."""
        await self.initialize()
        context = self._active_tasks.get(task_id)
        if context:
            context.cancelled = True
            context.paused = False
            context.execution_record.status = TaskExecutionStatus.CANCELLED
            context.execution_record.error = "Task cancelled by user"
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            context.pause_event.set()
            await self._state_manager.save_execution_state(context)
            await event_bus.publish_sync("task.cancelled", "orchestrator", {"task_id": task_id})
            return True

        record = await self._state_manager.get_execution_record(task_id)
        if record and record.status in (
            TaskExecutionStatus.PENDING,
            TaskExecutionStatus.QUEUED,
            TaskExecutionStatus.RUNNING,
            TaskExecutionStatus.PAUSED,
            TaskExecutionStatus.AWAITING_APPROVAL,
        ):
            record.status = TaskExecutionStatus.CANCELLED
            record.error = "Task cancelled by user"
            record.completed_at = int(datetime.now().timestamp() * 1000)
            await self._state_manager.save_execution_record(record)
            await event_bus.publish_sync("task.cancelled", "orchestrator", {"task_id": task_id})
            return True
        return False

    async def approve_subtask(
        self,
        task_id: str,
        subtask_id: Optional[str] = None,
        approved: bool = True,
        reason: Optional[str] = None,
    ) -> bool:
        """Process user approval or denial for a task/subtask or pending autonomous step."""
        await self.initialize()
        context = self._active_tasks.get(task_id)
        if not context:
            restored = await self._restore_context(task_id)
            if restored:
                context = restored
                self._active_tasks[task_id] = context
            else:
                record = await self._state_manager.get_execution_record(task_id)
                if not record:
                    return False
                if not approved:
                    record.status = TaskExecutionStatus.FAILED
                    record.error = f"Approval denied: {reason or 'User denied action'}"
                    record.completed_at = int(datetime.now().timestamp() * 1000)
                    await self._state_manager.save_execution_record(record)
                    await event_bus.publish_sync(
                        "task.failed",
                        "orchestrator",
                        {"task_id": task_id, "error": record.error},
                    )
                    return True
                return False

        subtasks = context.execution_plan.subtasks
        targets = [
            s for s in subtasks
            if (subtask_id is None or s.id == subtask_id)
            and (
                s.requires_approval
                or (
                    s.id in context.subtask_records
                    and context.subtask_records[s.id].status == SubtaskExecutionStatus.AWAITING_APPROVAL
                )
            )
        ]
        if not targets and subtask_id is not None:
            targets = [s for s in subtasks if s.id == subtask_id]
        if not targets:
            return False

        target_ids = {t.id for t in targets}
        updated_subtasks: List[Subtask] = []

        for st in subtasks:
            if st.id in target_ids:
                rec = context.subtask_records.get(st.id)
                new_meta = dict(st.metadata or {})
                if approved:
                    # Preserve mid-task autonomous step checkpoint + exact approved action digest
                    if rec and isinstance(rec.metadata, dict):
                        if rec.metadata.get("autonomous_state"):
                            new_meta["autonomous_state"] = rec.metadata["autonomous_state"]
                        pending_act = rec.metadata.get("pending_action") or (
                            rec.metadata.get("autonomous_state") or {}
                        ).get("pending_action")
                        if pending_act and pending_act.get("action_digest"):
                            approved_list = list(new_meta.get("approved_actions", []))
                            if pending_act["action_digest"] not in approved_list:
                                approved_list.append(pending_act["action_digest"])
                            new_meta["approved_actions"] = approved_list
                        rec.metadata.pop("pending_action", None)
                        rec.status = SubtaskExecutionStatus.PENDING
                        rec.error = None

                    updated_subtasks.append(
                        msgspec.structs.replace(
                            st,
                            requires_approval=True,
                            approval_status=ApprovalStatus.GRANTED,
                            metadata=new_meta,
                        )
                    )
                    audit_logger.log_consent(
                        capability=f"subtask:{st.id}",
                        granted=True,
                        scope="task",
                        task_id=task_id,
                    )
                else:
                    if rec:
                        rec.status = SubtaskExecutionStatus.FAILED
                        rec.error = f"Approval denied: {reason or 'User denied action'}"
                    updated_subtasks.append(
                        msgspec.structs.replace(
                            st,
                            approval_status=ApprovalStatus.DENIED,
                        )
                    )
                    audit_logger.log_consent(
                        capability=f"subtask:{st.id}",
                        granted=False,
                        scope="task",
                        task_id=task_id,
                        reason=reason,
                    )
            else:
                updated_subtasks.append(st)

        context.execution_plan = msgspec.structs.replace(
            context.execution_plan,
            subtasks=updated_subtasks,
        )
        context.execution_record.subtask_records = list(context.subtask_records.values())

        if approved:
            still_pending = any(
                s.requires_approval and s.approval_status == ApprovalStatus.PENDING
                for s in updated_subtasks
            )
            if not still_pending:
                context.paused = False
                context.execution_record.status = TaskExecutionStatus.RUNNING
                context.execution_record.completed_at = None
                context.pause_event.set()
                await self._state_manager.save_execution_state(context)
                asyncio.create_task(self._resume_execution(context))
            else:
                await self._state_manager.save_execution_state(context)
        else:
            context.execution_record.status = TaskExecutionStatus.FAILED
            context.execution_record.error = f"Action denied: {reason or 'User denied action'}"
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            context.cancelled = True
            context.paused = False
            context.pause_event.set()
            await self._state_manager.save_execution_state(context)
            self._active_tasks.pop(task_id, None)
            await event_bus.publish_sync(
                "task.failed",
                "orchestrator",
                {"task_id": task_id, "error": context.execution_record.error},
            )

        return True

    async def _resume_execution(self, context: OrchestrationContext) -> None:
        """Resume task execution cleanly after approval or checkpoint restore."""
        correlation_id = str(uuid.uuid4())
        try:
            await self._run_and_finalize(context, correlation_id)
        except asyncio.CancelledError:
            context.execution_record.status = TaskExecutionStatus.CANCELLED
            context.execution_record.error = "Task cancelled"
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            await self._state_manager.save_execution_state(context)
            await event_bus.publish_sync(
                "task.cancelled",
                "orchestrator",
                {"task_id": context.task_id},
                correlation_id=correlation_id,
            )
        except Exception as e:
            logger.error(f"Task resume execution failed: {e}")
            context.execution_record.status = TaskExecutionStatus.FAILED
            context.execution_record.error = str(e)
            context.execution_record.error_code = type(e).__name__
            context.execution_record.completed_at = int(datetime.now().timestamp() * 1000)
            await self._state_manager.save_execution_state(context)
            await event_bus.publish_sync(
                "task.failed",
                "orchestrator",
                {"task_id": context.task_id, "error": str(e)},
                correlation_id=correlation_id,
            )
        finally:
            if context.execution_record.status not in (
                TaskExecutionStatus.AWAITING_APPROVAL,
                TaskExecutionStatus.PAUSED,
            ):
                self._active_tasks.pop(context.task_id, None)

    async def _restore_context(
        self,
        task_id: str,
        checkpoint: Optional[Checkpoint] = None,
    ) -> Optional[OrchestrationContext]:
        """Reconstruct full OrchestrationContext from persisted state and optional checkpoint."""
        record = await self._state_manager.get_execution_record(task_id)
        bundle = await self._state_manager.get_plan_bundle(task_id)
        if not record or not bundle:
            return None

        execution_plan: ExecutionPlan = bundle["execution_plan"]
        routing_plan: RoutingPlan = bundle["routing_plan"]
        task_context: TaskContext = bundle["task_context"]
        persisted_subs = await self._state_manager.get_subtask_records(task_id)
        subtask_records: Dict[str, SubtaskExecutionRecord] = {}

        for st in execution_plan.subtasks:
            existing = persisted_subs.get(st.id)
            if existing:
                subtask_records[st.id] = existing
            else:
                subtask_records[st.id] = SubtaskExecutionRecord(
                    subtask_id=st.id,
                    task_id=task_id,
                    status=SubtaskExecutionStatus.PENDING,
                    dependencies=list(st.dependencies),
                    max_retries=st.max_retries,
                )

        if checkpoint:
            updated_subs: List[Subtask] = []
            for st in execution_plan.subtasks:
                cp_state = checkpoint.subtask_states.get(st.id)
                rec = subtask_records[st.id]
                if cp_state:
                    try:
                        status_enum = SubtaskExecutionStatus(cp_state.get("status", "pending"))
                    except ValueError:
                        status_enum = SubtaskExecutionStatus.PENDING
                    if status_enum in (SubtaskExecutionStatus.RUNNING, SubtaskExecutionStatus.RETRYING, SubtaskExecutionStatus.FAILED):
                        status_enum = SubtaskExecutionStatus.PENDING
                    rec.status = status_enum
                    rec.result = cp_state.get("result")
                    rec.error = cp_state.get("error") if status_enum == SubtaskExecutionStatus.AWAITING_APPROVAL else None
                    if cp_state.get("metadata"):
                        rec.metadata = dict(cp_state["metadata"])
                        if cp_state["metadata"].get("autonomous_state"):
                            st = msgspec.structs.replace(
                                st,
                                metadata={**st.metadata, "autonomous_state": cp_state["metadata"]["autonomous_state"]},
                            )
                updated_subs.append(st)
            execution_plan = msgspec.structs.replace(execution_plan, subtasks=updated_subs)

        checkpoints = await self._state_manager.get_task_checkpoints(task_id)
        return OrchestrationContext(
            task_id=task_id,
            execution_plan=execution_plan,
            routing_plan=routing_plan,
            task_context=task_context,
            execution_record=record,
            subtask_records=subtask_records,
            checkpoints=checkpoints,
            paused=(record.status == TaskExecutionStatus.PAUSED),
        )

    async def get_task_status(self, task_id: str) -> Optional[TaskExecutionRecord]:
        """Get status of a task."""
        if task_id in self._active_tasks:
            return self._active_tasks[task_id].execution_record
        return await self._state_manager.get_execution_record(task_id)

    async def resume_from_checkpoint(self, checkpoint_id: str) -> TaskExecutionRecord:
        """Resume task execution from a saved checkpoint (fail-closed if approvals pending)."""
        await self.initialize()
        checkpoint = await self._state_manager.get_checkpoint(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")

        record = await self._state_manager.get_execution_record(checkpoint.task_id)
        if not record:
            raise ValueError(f"Execution record not found for checkpoint: {checkpoint_id}")

        context = await self._restore_context(checkpoint.task_id, checkpoint=checkpoint)
        if not context:
            return record

        # Fail closed: never bypass pending approvals via checkpoint resume
        has_pending_approval = any(
            s.requires_approval and s.approval_status != ApprovalStatus.GRANTED
            for s in context.execution_plan.subtasks
        ) or any(
            r.status == SubtaskExecutionStatus.AWAITING_APPROVAL
            for r in context.subtask_records.values()
        )
        if has_pending_approval:
            context.execution_record.status = TaskExecutionStatus.AWAITING_APPROVAL
            self._active_tasks[context.task_id] = context
            await self._state_manager.save_execution_state(context)
            return context.execution_record

        context.cancelled = False
        context.paused = False
        context.pause_event.set()
        context.execution_record.status = TaskExecutionStatus.RUNNING
        context.execution_record.error = None
        context.execution_record.completed_at = None
        self._active_tasks[context.task_id] = context
        await self._state_manager.save_execution_state(context)
        await event_bus.publish_sync(
            "task.resumed",
            "orchestrator",
            {"task_id": context.task_id, "checkpoint_id": checkpoint_id},
        )
        asyncio.create_task(self._resume_execution(context))
        return context.execution_record


# Global orchestrator instance
orchestrator = ExecutionOrchestrator()
