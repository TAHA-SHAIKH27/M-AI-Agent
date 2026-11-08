"""Failure Recovery Manager for M AI Assistant."""
import asyncio
import structlog
import random
from typing import Optional, List, Dict, Any, Callable, Awaitable
from datetime import datetime
from dataclasses import dataclass

from ..planning import planner, router
from ..planning.schemas import (
    ExecutionPlan, RoutingPlan, Subtask, RoutingDecision,
    RoutingTargetType, ApprovalStatus,
)
from ..model_providers import provider_factory
from ..agents import agent_registry
from ..tools import tool_registry
from ..security import consent_manager, audit_logger, AuditEventType
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    RecoveryAction, RecoveryPlan,
    SubtaskExecutionStatus, SubtaskExecutionRecord,
    TaskExecutionStatus, TaskExecutionRecord,
)

logger = get_logger(__name__)


@dataclass
class RecoveryContext:
    """Context for recovery operations."""
    task_id: str
    execution_record: TaskExecutionRecord
    subtask_records: Dict[str, SubtaskExecutionRecord]
    execution_plan: ExecutionPlan
    routing_plan: RoutingPlan
    failed_subtask_id: Optional[str] = None
    error: Optional[str] = None


class FailureRecoveryManager:
    """Manages failure recovery for task execution."""
    
    def __init__(self):
        self._retry_policies: Dict[str, Dict[str, Any]] = {}
        self._max_replans = 3
        self._replan_counts: Dict[str, int] = {}
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the recovery manager."""
        if self._initialized:
            return
        
        # Default retry policies
        self._retry_policies = {
            "default": {
                "max_attempts": 3,
                "backoff_base": 2.0,
                "backoff_max": 60.0,
                "backoff_multiplier": 2.0,
                "jitter": True,
            },
            "tool": {
                "max_attempts": 2,
                "backoff_base": 1.0,
                "backoff_max": 30.0,
                "backoff_multiplier": 2.0,
                "jitter": True,
            },
            "agent": {
                "max_attempts": 2,
                "backoff_base": 2.0,
                "backoff_max": 60.0,
                "backoff_multiplier": 2.0,
                "jitter": True,
            },
            "model": {
                "max_attempts": 3,
                "backoff_base": 1.0,
                "backoff_max": 20.0,
                "backoff_multiplier": 1.5,
                "jitter": True,
            },
        }
        
        self._initialized = True
        logger.info("FailureRecoveryManager initialized")
    
    def set_retry_policy(self, target_type: str, policy: Dict[str, Any]) -> None:
        """Set retry policy for a target type."""
        self._retry_policies[target_type] = policy
    
    async def handle_failure(
        self,
        context: RecoveryContext,
    ) -> RecoveryPlan:
        """Handle a subtask failure and create a recovery plan."""
        failed_subtask_id = context.failed_subtask_id
        if not failed_subtask_id:
            return RecoveryPlan(
                task_id=context.task_id,
                action=RecoveryAction.CANCEL,
                reason="No failed subtask identified",
            )
        
        failed_record = context.subtask_records.get(failed_subtask_id)
        failed_subtask = next(
            (s for s in context.execution_plan.subtasks if s.id == failed_subtask_id),
            None
        )
        
        if not failed_record or not failed_subtask:
            return RecoveryPlan(
                task_id=context.task_id,
                failed_subtask_id=failed_subtask_id,
                action=RecoveryAction.CANCEL,
                reason="Failed subtask or record not found",
            )
        
        # Increment retry count
        failed_record.retry_count += 1
        
        # Get retry policy for target type
        target_type = failed_record.assigned_target_type or "default"
        policy = self._retry_policies.get(target_type, self._retry_policies["default"])
        
        # Check if we should retry
        if failed_record.retry_count <= failed_subtask.max_retries:
            return await self._create_retry_plan(
                context, failed_subtask, failed_record, policy
            )
        
        # Max retries exceeded - try fallback
        return await self._create_fallback_plan(
            context, failed_subtask, failed_record
        )
    
    async def _create_retry_plan(
        self,
        context: RecoveryContext,
        failed_subtask: Subtask,
        failed_record: SubtaskExecutionRecord,
        policy: Dict[str, Any],
    ) -> RecoveryPlan:
        """Create a retry recovery plan."""
        # Calculate backoff
        backoff = self._calculate_backoff(
            failed_record.retry_count,
            policy["backoff_base"],
            policy["backoff_max"],
            policy["backoff_multiplier"],
            policy.get("jitter", True),
        )
        
        return RecoveryPlan(
            task_id=context.task_id,
            failed_subtask_id=failed_subtask.id,
            action=RecoveryAction.RETRY_WITH_BACKOFF,
            reason=f"Retry attempt {failed_record.retry_count}/{failed_subtask.max_retries}",
            max_attempts=failed_subtask.max_retries,
            backoff_base_seconds=backoff,
            backoff_max_seconds=policy["backoff_max"],
        )
    
    async def _create_fallback_plan(
        self,
        context: RecoveryContext,
        failed_subtask: Subtask,
        failed_record: SubtaskExecutionRecord,
    ) -> RecoveryPlan:
        """Create a fallback recovery plan when retries exhausted."""
        
        # Get routing decision for failed subtask
        routing_decision = next(
            (d for d in context.routing_plan.decisions if d.subtask_id == failed_subtask.id),
            None
        )
        
        if not routing_decision:
            return await self._create_replan_recovery(context, failed_subtask)
        
        # Try fallback targets
        for fallback in routing_decision.fallback_targets:
            fb_type = fallback.get("type")
            fb_id = fallback.get("id")
            
            if fb_type == "tool" and await self._check_tool_available(fb_id):
                return RecoveryPlan(
                    task_id=context.task_id,
                    failed_subtask_id=failed_subtask.id,
                    action=RecoveryAction.FALLBACK_TOOL,
                    reason=f"Falling back to tool: {fb_id}",
                    fallback_target={"type": "tool", "id": fb_id},
                )
            
            elif fb_type == "agent" and await self._check_agent_available(fb_id):
                return RecoveryPlan(
                    task_id=context.task_id,
                    failed_subtask_id=failed_subtask.id,
                    action=RecoveryAction.FALLBACK_AGENT,
                    reason=f"Falling back to agent: {fb_id}",
                    fallback_target={"type": "agent", "id": fb_id},
                )
        
        # Try provider fallback
        if routing_decision.target_type == RoutingTargetType.MODEL_PROVIDER:
            fallback_provider = context.routing_plan.fallback_provider
            if fallback_provider:
                health = await provider_factory.get_provider(fallback_provider)
                if health and await health.health_check_cached():
                    return RecoveryPlan(
                        task_id=context.task_id,
                        failed_subtask_id=failed_subtask.id,
                        action=RecoveryAction.FALLBACK_PROVIDER,
                        reason=f"Falling back to provider: {fallback_provider}",
                        fallback_target={"type": "provider", "id": fallback_provider},
                    )
        
        # If subtask requires approval and failed, don't auto-retry
        if failed_subtask.requires_approval:
            return RecoveryPlan(
                task_id=context.task_id,
                failed_subtask_id=failed_subtask.id,
                action=RecoveryAction.ESCALATE,
                reason="Subtask requires approval, escalating for human review",
            )
        
        # Try replanning
        return await self._create_replan_recovery(context, failed_subtask)
    
    async def _create_replan_recovery(
        self,
        context: RecoveryContext,
        failed_subtask: Subtask,
    ) -> RecoveryPlan:
        """Create a replanning recovery plan."""
        replan_count = self._replan_counts.get(context.task_id, 0)
        
        if replan_count >= self._max_replans:
            return RecoveryPlan(
                task_id=context.task_id,
                failed_subtask_id=failed_subtask.id,
                action=RecoveryAction.CANCEL,
                reason=f"Maximum replans ({self._max_replans}) exceeded",
            )
        
        self._replan_counts[context.task_id] = replan_count + 1
        
        # Identify completed and failed subtasks
        completed = [
            sid for sid, record in context.subtask_records.items()
            if record.status == SubtaskExecutionStatus.COMPLETED
        ]
        failed = [
            sid for sid, record in context.subtask_records.items()
            if record.status == SubtaskExecutionStatus.FAILED
        ]
        
        # Create new plan
        new_plan = await planner.replan(
            context.execution_plan,
            completed_subtasks=completed,
            failed_subtasks=failed,
            new_info=f"Subtask {failed_subtask.id} failed: {context.error}",
        )
        
        # Create new routing plan
        new_routing = await router.create_routing_plan(new_plan)
        
        return RecoveryPlan(
            task_id=context.task_id,
            failed_subtask_id=failed_subtask.id,
            action=RecoveryAction.REPLAN,
            reason=f"Replanning after failure (attempt {replan_count + 1})",
            new_execution_plan_id=new_plan.task_id,
        )
    
    def _calculate_backoff(
        self,
        attempt: int,
        base: float,
        max_backoff: float,
        multiplier: float,
        jitter: bool,
    ) -> float:
        """Calculate exponential backoff with optional jitter."""
        backoff = min(base * (multiplier ** (attempt - 1)), max_backoff)
        
        if jitter:
            # Add jitter: ±25%
            backoff = backoff * (0.75 + random.random() * 0.5)
        
        return backoff
    
    async def _check_tool_available(self, tool_name: str) -> bool:
        """Check if a tool is available and consent granted."""
        tool = tool_registry.get_tool(tool_name)
        if not tool:
            return False
        
        if hasattr(tool.manifest, 'required_permissions'):
            for perm in tool.manifest.required_permissions:
                if not consent_manager.is_granted(perm):
                    return False
        
        return True
    
    async def _check_agent_available(self, agent_id: str) -> bool:
        """Check if an agent is available."""
        try:
            agent_info = agent_registry.get_agent_info(agent_id)
            if not agent_info:
                return False
            
            manifest = agent_info.get("manifest")
            if manifest:
                for perm in getattr(manifest, 'required_permissions', []):
                    if not consent_manager.is_granted(perm):
                        return False
            
            return True
        except Exception:
            return False
    
    async def execute_recovery(
        self,
        recovery_plan: RecoveryPlan,
        context: RecoveryContext,
        execute_subtask: Callable,
    ) -> Dict[str, Any]:
        """Execute a recovery plan."""
        
        if recovery_plan.action == RecoveryAction.RETRY_WITH_BACKOFF:
            # Wait for backoff
            await asyncio.sleep(recovery_plan.backoff_base_seconds)
            
            # Retry the subtask
            failed_record = context.subtask_records[recovery_plan.failed_subtask_id]
            failed_record.status = SubtaskExecutionStatus.RETRYING
            
            return await execute_subtask(recovery_plan.failed_subtask_id)
        
        elif recovery_plan.action == RecoveryAction.FALLBACK_TOOL:
            fb_target = recovery_plan.fallback_target
            # Update routing decision
            routing_decision = next(
                (d for d in context.routing_plan.decisions if d.subtask_id == recovery_plan.failed_subtask_id),
                None
            )
            if routing_decision:
                routing_decision.target_type = RoutingTargetType.TOOL
                routing_decision.target_id = fb_target["id"]
            
            failed_record = context.subtask_records[recovery_plan.failed_subtask_id]
            failed_record.status = SubtaskExecutionStatus.PENDING
            failed_record.retry_count = 0
            
            return await execute_subtask(recovery_plan.failed_subtask_id)
        
        elif recovery_plan.action == RecoveryAction.FALLBACK_AGENT:
            fb_target = recovery_plan.fallback_target
            routing_decision = next(
                (d for d in context.routing_plan.decisions if d.subtask_id == recovery_plan.failed_subtask_id),
                None
            )
            if routing_decision:
                routing_decision.target_type = RoutingTargetType.AGENT
                routing_decision.target_id = fb_target["id"]
            
            failed_record = context.subtask_records[recovery_plan.failed_subtask_id]
            failed_record.status = SubtaskExecutionStatus.PENDING
            failed_record.retry_count = 0
            
            return await execute_subtask(recovery_plan.failed_subtask_id)
        
        elif recovery_plan.action == RecoveryAction.FALLBACK_PROVIDER:
            fb_target = recovery_plan.fallback_target
            routing_decision = next(
                (d for d in context.routing_plan.decisions if d.subtask_id == recovery_plan.failed_subtask_id),
                None
            )
            if routing_decision:
                routing_decision.target_type = RoutingTargetType.MODEL_PROVIDER
                routing_decision.target_id = fb_target["id"]
            
            failed_record = context.subtask_records[recovery_plan.failed_subtask_id]
            failed_record.status = SubtaskExecutionStatus.PENDING
            failed_record.retry_count = 0
            
            return await execute_subtask(recovery_plan.failed_subtask_id)
        
        elif recovery_plan.action == RecoveryAction.REPLAN:
            # Signal that replanning is needed
            context.execution_record.status = TaskExecutionStatus.REPLANNING
            
            await event_bus.publish_sync(
                "task.replanned",
                "recovery",
                {
                    "task_id": context.task_id,
                    "new_plan_id": recovery_plan.new_execution_plan_id,
                    "replan_count": self._replan_counts.get(context.task_id, 0),
                }
            )
            
            return {"success": True, "replanned": True, "new_plan_id": recovery_plan.new_execution_plan_id}
        
        elif recovery_plan.action == RecoveryAction.SKIP:
            # Mark subtask as skipped
            failed_record = context.subtask_records[recovery_plan.failed_subtask_id]
            failed_record.status = SubtaskExecutionStatus.SKIPPED
            failed_record.error = "Skipped due to failure"
            
            return {"success": True, "skipped": True}
        
        elif recovery_plan.action == RecoveryAction.ESCALATE:
            # Mark as awaiting approval
            context.execution_record.status = TaskExecutionStatus.AWAITING_APPROVAL
            
            await event_bus.publish_sync(
                "task.awaiting_approval",
                "recovery",
                {"task_id": context.task_id, "reason": recovery_plan.reason}
            )
            
            return {"success": False, "escalated": True, "reason": recovery_plan.reason}
        
        else:  # CANCEL
            context.execution_record.status = TaskExecutionStatus.CANCELLED
            context.execution_record.error = recovery_plan.reason
            
            return {"success": False, "cancelled": True, "reason": recovery_plan.reason}
    
    def get_replan_count(self, task_id: str) -> int:
        """Get number of replans for a task."""
        return self._replan_counts.get(task_id, 0)
    
    def reset_replan_count(self, task_id: str) -> None:
        """Reset replan count for a task."""
        self._replan_counts.pop(task_id, None)


# Global recovery manager instance
recovery_manager = FailureRecoveryManager()