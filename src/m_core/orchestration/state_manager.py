"""Execution State Manager for M AI Assistant."""
import asyncio
import json
import msgspec
import uuid
import structlog
import msgspec
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

from ..config import settings
from ..planning.schemas import ExecutionPlan, RoutingPlan, TaskContext
from ..utils.logging import get_logger
from .schemas import (
    TaskExecutionRecord, SubtaskExecutionRecord,
    Checkpoint, CheckpointType,
    TaskExecutionStatus, SubtaskExecutionStatus,
)

logger = get_logger(__name__)


@dataclass
class OrchestrationContext:
    """Context for orchestration execution (forward reference)."""
    task_id: str
    execution_record: TaskExecutionRecord
    subtask_records: Dict[str, SubtaskExecutionRecord]
    checkpoints: List[Checkpoint]


class ExecutionStateManager:
    """Manages persistent execution state and checkpointing."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or (settings.data_dir / "orchestration")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._execution_records: Dict[str, TaskExecutionRecord] = {}
        self._subtask_records: Dict[str, Dict[str, SubtaskExecutionRecord]] = {}
        self._checkpoints: Dict[str, Checkpoint] = {}
        self._task_checkpoints: Dict[str, List[str]] = {}  # task_id -> checkpoint_ids
        self._plan_bundles: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the state manager."""
        if self._initialized:
            return
        
        # Load existing state
        await self._load_state()
        self._initialized = True
        logger.info("ExecutionStateManager initialized")
    
    async def _load_state(self) -> None:
        """Load state from disk."""
        try:
            # Load execution records
            exec_file = self.data_dir / "executions.json"
            if exec_file.exists():
                data = json.loads(exec_file.read_text(encoding="utf-8"))
                for task_id, record_data in data.items():
                    # Convert to TaskExecutionRecord
                    record = msgspec.convert(record_data, type=TaskExecutionRecord)
                    # msgspec may leave str-backed enum fields as plain strings.
                    # Restore enum instances because orchestration code uses .value.
                    record.status = TaskExecutionStatus(record.status)
                    for subtask in record.subtask_records:
                        subtask.status = SubtaskExecutionStatus(subtask.status)
                    self._execution_records[task_id] = record
            
            # Load subtask records
            subtask_dir = self.data_dir / "subtasks"
            if subtask_dir.exists():
                for task_file in subtask_dir.glob("*.json"):
                    task_id = task_file.stem
                    data = json.loads(task_file.read_text(encoding="utf-8"))
                    loaded_subtasks = {
                        sid: msgspec.convert(sdata, type=SubtaskExecutionRecord)
                        for sid, sdata in data.items()
                    }
                    for subtask in loaded_subtasks.values():
                        subtask.status = SubtaskExecutionStatus(subtask.status)
                    self._subtask_records[task_id] = loaded_subtasks
            
            # Load checkpoints
            checkpoint_dir = self.data_dir / "checkpoints"
            if checkpoint_dir.exists():
                for cp_file in checkpoint_dir.glob("*.json"):
                    data = json.loads(cp_file.read_text(encoding="utf-8"))
                    checkpoint = Checkpoint(
                        **{
                            **data,
                            "checkpoint_type": CheckpointType(data["checkpoint_type"]),
                        }
                    )
                    self._checkpoints[checkpoint.checkpoint_id] = checkpoint
                    
                    if checkpoint.task_id not in self._task_checkpoints:
                        self._task_checkpoints[checkpoint.task_id] = []
                    self._task_checkpoints[checkpoint.task_id].append(checkpoint.checkpoint_id)
            
            logger.info(f"Loaded state: {len(self._execution_records)} executions, {len(self._checkpoints)} checkpoints")
            
        except Exception as e:
            logger.error(f"Failed to load persisted orchestration state: {e}")
            raise
    
    async def save_execution_state(self, context: OrchestrationContext) -> None:
        """Save execution state, subtask records, and plan bundle to disk."""
        async with self._lock:
            # Update in-memory
            self._execution_records[context.task_id] = context.execution_record
            self._subtask_records[context.task_id] = context.subtask_records
            if hasattr(context, "execution_plan") and hasattr(context, "routing_plan") and hasattr(context, "task_context"):
                self._plan_bundles[context.task_id] = {
                    "execution_plan": context.execution_plan,
                    "routing_plan": context.routing_plan,
                    "task_context": context.task_context,
                }
                await self._persist_plan_bundle(context.task_id)
            
            # Persist to disk
            await self._persist_execution_record(context.task_id)
            await self._persist_subtask_records(context.task_id)
    
    async def _persist_plan_bundle(self, task_id: str) -> None:
        """Persist ExecutionPlan, RoutingPlan, and TaskContext for full checkpoint/restart recovery."""
        bundle = self._plan_bundles.get(task_id)
        if not bundle:
            return
        try:
            plans_dir = self.data_dir / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            plan_file = plans_dir / f"{task_id}.json"
            payload = {
                "execution_plan": json.loads(msgspec.json.encode(bundle["execution_plan"]).decode("utf-8")),
                "routing_plan": json.loads(msgspec.json.encode(bundle["routing_plan"]).decode("utf-8")),
                "task_context": json.loads(msgspec.json.encode(bundle["task_context"]).decode("utf-8")),
            }
            plan_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to persist plan bundle for {task_id}: {e}")
    
    async def get_plan_bundle(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached or persisted ExecutionPlan, RoutingPlan, and TaskContext."""
        if task_id in self._plan_bundles:
            return self._plan_bundles[task_id]
        plan_file = self.data_dir / "plans" / f"{task_id}.json"
        if not plan_file.exists():
            return None
        try:
            raw = json.loads(plan_file.read_text(encoding="utf-8"))
            bundle = {
                "execution_plan": msgspec.json.decode(json.dumps(raw["execution_plan"]).encode("utf-8"), type=ExecutionPlan),
                "routing_plan": msgspec.json.decode(json.dumps(raw["routing_plan"]).encode("utf-8"), type=RoutingPlan),
                "task_context": msgspec.json.decode(json.dumps(raw["task_context"]).encode("utf-8"), type=TaskContext),
            }
            self._plan_bundles[task_id] = bundle
            return bundle
        except Exception as e:
            logger.error(f"Failed to load persisted plan bundle for {task_id}: {e}")
            return None
    
    async def _persist_execution_record(self, task_id: str) -> None:
        """Persist one execution record, or rewrite the complete index when task_id is empty."""
        try:
            exec_file = self.data_dir / "executions.json"
            data = {}
            if exec_file.exists():
                try:
                    loaded = json.loads(exec_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except (json.JSONDecodeError, OSError) as exc:
                    logger.error(f"Failed to read execution index before update: {exc}")
                    raise

            if task_id:
                record = self._execution_records.get(task_id)
                if record is None:
                    return
                data[task_id] = self._record_to_dict(record)
            else:
                data = {
                    current_id: self._record_to_dict(record)
                    for current_id, record in self._execution_records.items()
                }

            exec_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = exec_file.with_suffix(exec_file.suffix + ".tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(exec_file)
        except Exception as e:
            logger.error(f"Failed to persist execution record: {e}")
            raise

    async def _persist_subtask_records(self, task_id: str) -> None:
        """Persist subtask records to disk."""
        try:
            subtask_dir = self.data_dir / "subtasks"
            subtask_dir.mkdir(parents=True, exist_ok=True)
            
            task_file = subtask_dir / f"{task_id}.json"
            
            records = self._subtask_records.get(task_id, {})
            data = {
                sid: self._subtask_record_to_dict(record)
                for sid, record in records.items()
            }
            
            temp_file = task_file.with_suffix(task_file.suffix + ".tmp")
            temp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp_file.replace(task_file)
            
        except Exception as e:
            logger.error(f"Failed to persist subtask records: {e}")
            raise
    
    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Save a checkpoint."""
        async with self._lock:
            self._checkpoints[checkpoint.checkpoint_id] = checkpoint
            
            if checkpoint.task_id not in self._task_checkpoints:
                self._task_checkpoints[checkpoint.task_id] = []
            self._task_checkpoints[checkpoint.task_id].append(checkpoint.checkpoint_id)
            
            await self._persist_checkpoint(checkpoint)
    
    async def _persist_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist checkpoint to disk."""
        try:
            checkpoint_dir = self.data_dir / "checkpoints"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            
            cp_file = checkpoint_dir / f"{checkpoint.checkpoint_id}.json"
            temp_file = cp_file.with_suffix(cp_file.suffix + ".tmp")
            temp_file.write_text(
                json.dumps(self._checkpoint_to_dict(checkpoint), indent=2),
                encoding="utf-8"
            )
            temp_file.replace(cp_file)
            
        except Exception as e:
            logger.error(f"Failed to persist checkpoint: {e}")
            raise
    
    async def get_execution_record(self, task_id: str) -> Optional[TaskExecutionRecord]:
        """Get execution record by task ID."""
        return self._execution_records.get(task_id)

    async def save_execution_record(self, record: TaskExecutionRecord) -> None:
        """Save execution record to disk."""
        async with self._lock:
            self._execution_records[record.task_id] = record
            await self._persist_execution_record(record.task_id)

    async def get_subtask_records(self, task_id: str) -> Dict[str, SubtaskExecutionRecord]:
        """Get subtask records for a task."""
        return self._subtask_records.get(task_id, {})
    
    async def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        """Get checkpoint by ID."""
        return self._checkpoints.get(checkpoint_id)
    
    async def get_task_checkpoints(self, task_id: str) -> List[Checkpoint]:
        """Get all checkpoints for a task."""
        checkpoint_ids = self._task_checkpoints.get(task_id, [])
        return [self._checkpoints[cid] for cid in checkpoint_ids if cid in self._checkpoints]
    
    async def get_latest_checkpoint(self, task_id: str) -> Optional[Checkpoint]:
        """Get the latest checkpoint for a task."""
        checkpoints = await self.get_task_checkpoints(task_id)
        if not checkpoints:
            return None
        return max(checkpoints, key=lambda c: c.created_at)
    
    async def list_executions(
        self,
        status: Optional[TaskExecutionStatus] = None,
        limit: int = 100,
    ) -> List[TaskExecutionRecord]:
        """List execution records."""
        records = list(self._execution_records.values())
        
        if status:
            records = [r for r in records if r.status == status]
        
        # Sort by created_at descending
        records.sort(key=lambda r: r.created_at, reverse=True)
        
        return records[:limit]
    
    async def cleanup_old_records(self, max_age_days: int = 30) -> int:
        """Clean up old execution records."""
        cutoff = int((datetime.now().timestamp() - max_age_days * 86400) * 1000)
        cleaned = 0
        
        async with self._lock:
            to_remove = [
                task_id for task_id, record in self._execution_records.items()
                if record.created_at < cutoff
            ]
            
            for task_id in to_remove:
                del self._execution_records[task_id]
                self._subtask_records.pop(task_id, None)
                
                # Remove checkpoint files
                for cp_id in self._task_checkpoints.get(task_id, []):
                    self._checkpoints.pop(cp_id, None)
                    cp_file = self.data_dir / "checkpoints" / f"{cp_id}.json"
                    if cp_file.exists():
                        cp_file.unlink()
                
                self._task_checkpoints.pop(task_id, None)
                self._plan_bundles.pop(task_id, None)
                sub_file = self.data_dir / "subtasks" / f"{task_id}.json"
                if sub_file.exists():
                    sub_file.unlink()
                plan_file = self.data_dir / "plans" / f"{task_id}.json"
                if plan_file.exists():
                    plan_file.unlink()
                cleaned += 1
            
            # Rewrite execution index cleanly
            await self._rewrite_execution_index()
        
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} old execution records")
        
        return cleaned
    
    async def _rewrite_execution_index(self) -> None:
        """Rewrite the full executions index file from current in-memory records."""
        try:
            exec_file = self.data_dir / "executions.json"
            data = {
                tid: self._record_to_dict(rec)
                for tid, rec in self._execution_records.items()
            }
            exec_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to rewrite execution index: {e}")
    
    def _record_to_dict(self, record: TaskExecutionRecord) -> Dict[str, Any]:
        """Convert execution record to dictionary."""
        return {
            "task_id": record.task_id,
            "execution_plan_id": record.execution_plan_id,
            "routing_plan_id": record.routing_plan_id,
            "status": record.status.value,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "parent_task_id": record.parent_task_id,
            "conversation_id": record.conversation_id,
            "user_id": record.user_id,
            "subtask_records": [
                self._subtask_record_to_dict(r) for r in record.subtask_records
            ],
            "result": record.result,
            "error": record.error,
            "error_code": record.error_code,
            "retry_count": record.retry_count,
            "max_retries": record.max_retries,
            "metadata": record.metadata,
        }
    
    def _subtask_record_to_dict(self, record: SubtaskExecutionRecord) -> Dict[str, Any]:
        """Convert subtask record to dictionary."""
        return {
            "subtask_id": record.subtask_id,
            "task_id": record.task_id,
            "status": record.status.value,
            "assigned_target_type": record.assigned_target_type,
            "assigned_target_id": record.assigned_target_id,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "result": record.result,
            "error": record.error,
            "error_code": record.error_code,
            "retry_count": record.retry_count,
            "max_retries": record.max_retries,
            "dependencies": record.dependencies,
            "execution_time_ms": record.execution_time_ms,
            "metadata": record.metadata,
        }
    
    def _checkpoint_to_dict(self, checkpoint: Checkpoint) -> Dict[str, Any]:
        """Convert checkpoint to dictionary."""
        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "task_id": checkpoint.task_id,
            "checkpoint_type": checkpoint.checkpoint_type.value,
            "created_at": checkpoint.created_at,
            "execution_state": checkpoint.execution_state,
            "subtask_states": checkpoint.subtask_states,
            "completed_subtasks": checkpoint.completed_subtasks,
            "failed_subtasks": checkpoint.failed_subtasks,
            "metadata": checkpoint.metadata,
        }


# Global state manager instance
state_manager = ExecutionStateManager()