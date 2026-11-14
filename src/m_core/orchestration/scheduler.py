"""Task Scheduler for M AI Assistant."""
import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

import msgspec
import structlog

from ..core.events import event_bus
from ..planning.schemas import (
    ExecutionPlan,
    ExecutionStrategy,
    RoutingDecision,
    RoutingPlan,
    RoutingTargetType,
    Subtask,
)
from ..utils.logging import get_logger
from .execution_manager import execution_manager
from .recovery import FailureRecoveryManager, RecoveryContext, recovery_manager
from .schemas import (
    RecoveryAction,
    SchedulerConfig,
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
)

logger = get_logger(__name__)


@dataclass
class ScheduledTask:
    """A task scheduled for execution."""
    subtask: Subtask
    routing_decision: RoutingDecision
    record: SubtaskExecutionRecord
    dependencies_met: bool = False


class TaskScheduler:
    """Dependency-aware task scheduler with concurrency control, pause/cancel, and recovery."""

    def __init__(self, config: Optional[SchedulerConfig] = None):
        self.config = config or SchedulerConfig()
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the scheduler."""
        if self._initialized:
            return
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        await execution_manager.initialize()
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
        pause_waiter: Optional[Callable[[], Awaitable[None]]] = None,
        execution_record: Optional[TaskExecutionRecord] = None,
        recovery_mgr: Optional[FailureRecoveryManager] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Execute a plan with per-run dependency tracking, progress callbacks, and recovery."""
        if not self._initialized:
            await self.initialize()

        routing_map = {d.subtask_id: d for d in routing_plan.decisions}

        # Pre-seed completed subtasks when resuming from checkpoint / approval
        completed_tasks: Dict[str, Dict[str, Any]] = {}
        scheduled_tasks: Dict[str, ScheduledTask] = {}

        for subtask in execution_plan.subtasks:
            record = subtask_records[subtask.id]
            routing_decision = routing_map.get(subtask.id) or RoutingDecision(
                subtask_id=subtask.id,
                target_type=RoutingTargetType.AGENT,
                target_id="general_assistant",
                confidence=0.5,
                reasoning="Default routing",
            )
            if record.status == SubtaskExecutionStatus.COMPLETED:
                completed_tasks[subtask.id] = {
                    "success": True,
                    "result": record.result,
                }
                continue
            scheduled_tasks[subtask.id] = ScheduledTask(
                subtask=subtask,
                routing_decision=routing_decision,
                record=record,
            )

        if not scheduled_tasks:
            return dict(completed_tasks)

        run_state: Dict[str, Any] = {
            "completed": completed_tasks,
            "execution_plan": execution_plan,
            "routing_plan": routing_plan,
            "subtask_records": subtask_records,
            "execution_record": execution_record,
            "recovery_mgr": recovery_mgr or recovery_manager,
            "halt_reason": None,
            "replan_id": None,
        }

        if execution_plan.strategy == ExecutionStrategy.SEQUENTIAL:
            results = await self._execute_sequential(
                scheduled_tasks, run_state, task_context, correlation_id,
                progress_callback, cancellation_check, pause_waiter,
            )
        elif execution_plan.strategy == ExecutionStrategy.PARALLEL:
            results = await self._execute_parallel(
                scheduled_tasks, run_state, task_context, correlation_id,
                progress_callback, cancellation_check, pause_waiter,
            )
        else:
            results = await self._execute_dependency_aware(
                scheduled_tasks, run_state, task_context, correlation_id,
                progress_callback, cancellation_check, pause_waiter,
            )

        merged = {**completed_tasks, **results}
        if run_state.get("replan_id"):
            merged["_replanned"] = {"new_plan_id": run_state["replan_id"]}
        if run_state.get("halt_reason"):
            merged["_halt_reason"] = {"reason": run_state["halt_reason"]}
        return merged

    async def _execute_sequential(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        run_state: Dict[str, Any],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_waiter: Optional[Callable[[], Awaitable[None]]],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute remaining subtasks sequentially honoring priority and dependencies."""
        results: Dict[str, Dict[str, Any]] = {}
        sorted_tasks = sorted(scheduled_tasks.values(), key=lambda t: -t.subtask.priority)
        completed: Dict[str, Dict[str, Any]] = run_state["completed"]

        for scheduled in sorted_tasks:
            if pause_waiter:
                await pause_waiter()
            if cancellation_check and cancellation_check():
                scheduled.record.status = SubtaskExecutionStatus.CANCELLED
                scheduled.record.error = "Cancelled"
                await self._notify_progress(progress_callback, scheduled.subtask.id, scheduled.record)
                run_state["halt_reason"] = "cancelled"
                break

            if not self._deps_satisfied(scheduled.subtask, completed):
                scheduled.record.status = SubtaskExecutionStatus.SKIPPED
                scheduled.record.error = "Prerequisite subtask failed or incomplete"
                results[scheduled.subtask.id] = {"success": False, "error": scheduled.record.error}
                completed[scheduled.subtask.id] = results[scheduled.subtask.id]
                await self._notify_progress(progress_callback, scheduled.subtask.id, scheduled.record)
                continue

            result = await self._execute_subtask_with_recovery(
                scheduled, run_state, task_context, correlation_id,
                progress_callback, cancellation_check, pause_waiter,
            )
            results[scheduled.subtask.id] = result
            completed[scheduled.subtask.id] = result

            if result.get("awaiting_approval"):
                run_state["halt_reason"] = "awaiting_approval"
                break
            if result.get("replanned"):
                run_state["replan_id"] = result.get("new_plan_id")
                run_state["halt_reason"] = "replanned"
                break
            if result.get("cancelled"):
                run_state["halt_reason"] = "cancelled"
                break

        return results

    async def _execute_parallel(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        run_state: Dict[str, Any],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_waiter: Optional[Callable[[], Awaitable[None]]],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute independent subtasks concurrently by dependency level."""
        levels = self._compute_dependency_levels(scheduled_tasks)
        results: Dict[str, Dict[str, Any]] = {}
        completed: Dict[str, Dict[str, Any]] = run_state["completed"]

        for level in levels:
            if pause_waiter:
                await pause_waiter()
            if cancellation_check and cancellation_check():
                run_state["halt_reason"] = "cancelled"
                break

            tasks: List[ScheduledTask] = []
            for tid in level:
                st = scheduled_tasks[tid]
                if self._deps_satisfied(st.subtask, completed):
                    tasks.append(st)
                else:
                    st.record.status = SubtaskExecutionStatus.SKIPPED
                    st.record.error = "Prerequisite subtask failed or incomplete"
                    res = {"success": False, "error": st.record.error}
                    results[tid] = res
                    completed[tid] = res
                    await self._notify_progress(progress_callback, tid, st.record)

            if not tasks:
                continue

            async def execute_with_semaphore(scheduled: ScheduledTask):
                async with self._semaphore:
                    return await self._execute_subtask_with_recovery(
                        scheduled, run_state, task_context, correlation_id,
                        progress_callback, cancellation_check, pause_waiter,
                    )

            level_results = await asyncio.gather(
                *[execute_with_semaphore(t) for t in tasks],
                return_exceptions=True,
            )

            halt = False
            for scheduled, result in zip(tasks, level_results):
                if isinstance(result, Exception):
                    result = {"success": False, "error": str(result)}
                results[scheduled.subtask.id] = result
                completed[scheduled.subtask.id] = result
                if result.get("awaiting_approval"):
                    run_state["halt_reason"] = "awaiting_approval"
                    halt = True
                elif result.get("replanned"):
                    run_state["replan_id"] = result.get("new_plan_id")
                    run_state["halt_reason"] = "replanned"
                    halt = True
                elif result.get("cancelled"):
                    run_state["halt_reason"] = "cancelled"
                    halt = True

            if halt:
                break

        return results

    async def _execute_dependency_aware(
        self,
        scheduled_tasks: Dict[str, ScheduledTask],
        run_state: Dict[str, Any],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_waiter: Optional[Callable[[], Awaitable[None]]],
    ) -> Dict[str, Dict[str, Any]]:
        """Execute subtasks respecting dependencies with max concurrency and no deadlock on failures."""
        results: Dict[str, Dict[str, Any]] = {}
        completed: Dict[str, Dict[str, Any]] = run_state["completed"]
        pending = set(scheduled_tasks.keys())
        running: Dict[str, asyncio.Task] = {}

        while pending or running:
            if pause_waiter:
                await pause_waiter()

            if cancellation_check and cancellation_check():
                for task in running.values():
                    task.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                for tid in list(pending):
                    scheduled_tasks[tid].record.status = SubtaskExecutionStatus.CANCELLED
                    scheduled_tasks[tid].record.error = "Cancelled"
                run_state["halt_reason"] = "cancelled"
                break

            done = [tid for tid, task in running.items() if task.done()]
            halt_triggered = False

            for tid in done:
                task = running.pop(tid)
                scheduled = scheduled_tasks[tid]
                try:
                    result = await task
                except Exception as e:
                    result = {"success": False, "error": str(e)}

                results[tid] = result
                completed[tid] = result
                pending.discard(tid)

                if result.get("awaiting_approval"):
                    run_state["halt_reason"] = "awaiting_approval"
                    halt_triggered = True
                elif result.get("replanned"):
                    run_state["replan_id"] = result.get("new_plan_id")
                    run_state["halt_reason"] = "replanned"
                    halt_triggered = True
                elif result.get("cancelled"):
                    run_state["halt_reason"] = "cancelled"
                    halt_triggered = True

            if halt_triggered:
                for task in running.values():
                    task.cancel()
                if running:
                    await asyncio.gather(*running.values(), return_exceptions=True)
                break

            # Mark subtasks whose dependencies already failed/skipped so we never deadlock
            blocked = []
            for tid in list(pending):
                scheduled = scheduled_tasks[tid]
                for dep in scheduled.subtask.dependencies:
                    if dep in completed and not completed[dep].get("success"):
                        blocked.append((tid, dep))
                        break
                    if dep not in scheduled_tasks and dep not in completed:
                        blocked.append((tid, dep))
                        break

            for tid, bad_dep in blocked:
                pending.discard(tid)
                scheduled = scheduled_tasks[tid]
                scheduled.record.status = SubtaskExecutionStatus.SKIPPED
                scheduled.record.error = f"Skipped because prerequisite '{bad_dep}' did not succeed"
                res = {"success": False, "error": scheduled.record.error}
                results[tid] = res
                completed[tid] = res
                await self._notify_progress(progress_callback, tid, scheduled.record)

            # Find ready subtasks whose dependencies succeeded
            ready = []
            for tid in list(pending):
                scheduled = scheduled_tasks[tid]
                if self._deps_satisfied(scheduled.subtask, completed):
                    ready.append(tid)

            ready.sort(key=lambda tid: -scheduled_tasks[tid].subtask.priority)

            available_slots = self.config.max_concurrency - len(running)
            for tid in ready[:max(0, available_slots)]:
                scheduled = scheduled_tasks[tid]
                pending.remove(tid)
                task = asyncio.create_task(
                    self._execute_subtask_with_recovery(
                        scheduled, run_state, task_context, correlation_id,
                        progress_callback, cancellation_check, pause_waiter,
                    )
                )
                running[tid] = task

            if running:
                await asyncio.sleep(0.05)
            elif pending and not ready:
                # Cycle or unresolvable dependency guard
                for tid in list(pending):
                    scheduled = scheduled_tasks[tid]
                    scheduled.record.status = SubtaskExecutionStatus.FAILED
                    scheduled.record.error = "Unresolvable subtask dependency cycle"
                    res = {"success": False, "error": scheduled.record.error}
                    results[tid] = res
                    completed[tid] = res
                    await self._notify_progress(progress_callback, tid, scheduled.record)
                break

        return results

    def _deps_satisfied(self, subtask: Subtask, completed: Dict[str, Dict[str, Any]]) -> bool:
        """Check whether all dependencies completed successfully."""
        return all(
            dep in completed and bool(completed[dep].get("success"))
            for dep in subtask.dependencies
        )

    def _compute_dependency_levels(self, scheduled_tasks: Dict[str, ScheduledTask]) -> List[List[str]]:
        """Compute execution levels based on dependencies among remaining subtasks."""
        levels: List[List[str]] = []
        remaining = set(scheduled_tasks.keys())
        seen: set[str] = set()

        while remaining:
            level = []
            for tid in list(remaining):
                scheduled = scheduled_tasks[tid]
                relevant_deps = [d for d in scheduled.subtask.dependencies if d in scheduled_tasks]
                if all(dep in seen for dep in relevant_deps):
                    level.append(tid)
            if not level:
                level = list(remaining)
            for tid in level:
                remaining.remove(tid)
                seen.add(tid)
            levels.append(level)

        return levels

    async def _execute_subtask_with_recovery(
        self,
        scheduled: ScheduledTask,
        run_state: Dict[str, Any],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_waiter: Optional[Callable[[], Awaitable[None]]],
    ) -> Dict[str, Any]:
        """Execute a subtask via ExecutionManager and invoke FailureRecoveryManager on failure."""
        result = await self._run_single(
            scheduled, run_state["completed"], task_context, correlation_id,
            progress_callback, cancellation_check, pause_waiter,
        )

        # Do not retry or replan cancellation / approval gates / successes
        if result.get("success") or result.get("awaiting_approval") or result.get("cancelled"):
            return result

        execution_record: Optional[TaskExecutionRecord] = run_state.get("execution_record")
        rec_mgr: Optional[FailureRecoveryManager] = run_state.get("recovery_mgr")
        if not execution_record or not rec_mgr:
            return result

        # Bounded recovery loop (retry backoff / fallback target / replan)
        for _ in range(max(1, scheduled.subtask.max_retries + 2)):
            recovery_ctx = RecoveryContext(
                task_id=execution_record.task_id,
                execution_record=execution_record,
                subtask_records=run_state["subtask_records"],
                execution_plan=run_state["execution_plan"],
                routing_plan=run_state["routing_plan"],
                failed_subtask_id=scheduled.subtask.id,
                error=result.get("error"),
            )
            recovery_plan = await rec_mgr.handle_failure(recovery_ctx)

            if recovery_plan.action == RecoveryAction.ESCALATE:
                return {"success": False, "awaiting_approval": True, "error": recovery_plan.reason}

            async def _reexecute(_subtask_id: str) -> Dict[str, Any]:
                updated_decision = next(
                    (d for d in recovery_ctx.routing_plan.decisions if d.subtask_id == _subtask_id),
                    scheduled.routing_decision,
                )
                scheduled.routing_decision = updated_decision
                run_state["routing_plan"] = recovery_ctx.routing_plan
                return await self._run_single(
                    scheduled, run_state["completed"], task_context, correlation_id,
                    progress_callback, cancellation_check, pause_waiter,
                )

            rec_result = await rec_mgr.execute_recovery(recovery_plan, recovery_ctx, _reexecute)
            if rec_result.get("replanned") or rec_result.get("skipped") or rec_result.get("success"):
                return rec_result
            if rec_result.get("awaiting_approval") or rec_result.get("cancelled") or rec_result.get("escalated"):
                return {
                    **rec_result,
                    "awaiting_approval": bool(rec_result.get("escalated") or rec_result.get("awaiting_approval")),
                }
            result = rec_result

        return result

    async def _run_single(
        self,
        scheduled: ScheduledTask,
        completed: Dict[str, Dict[str, Any]],
        task_context: Any,
        correlation_id: str,
        progress_callback: Optional[Callable],
        cancellation_check: Optional[Callable],
        pause_waiter: Optional[Callable[[], Awaitable[None]]],
    ) -> Dict[str, Any]:
        """Inject completed dependency outputs and delegate execution to ExecutionManager."""
        subtask = scheduled.subtask
        if subtask.dependencies:
            dep_results = {
                dep: completed[dep].get("result")
                for dep in subtask.dependencies
                if dep in completed and completed[dep].get("result") is not None
            }
            if dep_results:
                subtask = msgspec.structs.replace(
                    subtask,
                    metadata={**subtask.metadata, "dependency_results": dep_results},
                )
                scheduled.subtask = subtask

        timeout_s = max(5.0, float(subtask.timeout_seconds or self.config.default_timeout_seconds))
        try:
            result = await asyncio.wait_for(
                execution_manager.execute_subtask(
                    subtask=subtask,
                    routing_decision=scheduled.routing_decision,
                    record=scheduled.record,
                    task_context=task_context,
                    correlation_id=correlation_id,
                    progress_callback=progress_callback,
                    cancellation_check=cancellation_check,
                    pause_waiter=pause_waiter,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            scheduled.record.status = SubtaskExecutionStatus.FAILED
            scheduled.record.error = f"Subtask '{subtask.name}' timed out after {timeout_s}s"
            scheduled.record.completed_at = int(datetime.now().timestamp() * 1000)
            result = {"success": False, "error": scheduled.record.error}
        except asyncio.CancelledError:
            scheduled.record.status = SubtaskExecutionStatus.CANCELLED
            scheduled.record.error = "Cancelled"
            scheduled.record.completed_at = int(datetime.now().timestamp() * 1000)
            raise

        await self._notify_progress(progress_callback, subtask.id, scheduled.record)
        return result

    async def _notify_progress(
        self,
        progress_callback: Optional[Callable],
        subtask_id: str,
        record: SubtaskExecutionRecord,
    ) -> None:
        """Invoke async or sync progress callback safely."""
        if not progress_callback:
            return
        try:
            if inspect.iscoroutinefunction(progress_callback):
                await progress_callback(subtask_id, record)
            else:
                maybe_coro = progress_callback(subtask_id, record)
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro
        except Exception as e:
            logger.warning("Progress callback error for %s: %s", subtask_id, e)

    async def _worker_loop(self) -> None:
        """Background maintenance loop."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler worker error: {e}")


# Global scheduler instance
scheduler = TaskScheduler()
