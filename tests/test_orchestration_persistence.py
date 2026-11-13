"""Regression tests for shared orchestration state and durable persistence."""
import pytest

from m_core.orchestration import orchestrator, state_manager
from m_core.orchestration.state_manager import ExecutionStateManager, OrchestrationContext
from m_core.orchestration.schemas import (
    Checkpoint,
    CheckpointType,
    SubtaskExecutionRecord,
    SubtaskExecutionStatus,
    TaskExecutionRecord,
    TaskExecutionStatus,
)


@pytest.mark.asyncio
async def test_default_orchestrator_shares_task_board_state_manager():
    await orchestrator.initialize()
    assert orchestrator._state_manager is state_manager


@pytest.mark.asyncio
async def test_execution_subtasks_and_checkpoints_survive_reload(tmp_path):
    manager = ExecutionStateManager(data_dir=tmp_path)
    await manager.initialize()

    subtask = SubtaskExecutionRecord(
        subtask_id="subtask-1",
        task_id="task-1",
        status=SubtaskExecutionStatus.COMPLETED,
        assigned_target_type="tool",
        assigned_target_id="system_info",
    )
    record = TaskExecutionRecord(
        task_id="task-1",
        execution_plan_id="plan-1",
        status=TaskExecutionStatus.COMPLETED,
        subtask_records=[subtask],
    )
    context = OrchestrationContext(
        task_id="task-1",
        execution_record=record,
        subtask_records={"subtask-1": subtask},
        checkpoints=[],
    )
    checkpoint = Checkpoint(
        checkpoint_id="checkpoint-1",
        task_id="task-1",
        checkpoint_type=CheckpointType.POST_EXECUTION,
        execution_state={"status": "completed"},
        completed_subtasks=["subtask-1"],
    )

    await manager.save_execution_state(context)
    await manager.save_checkpoint(checkpoint)

    reloaded = ExecutionStateManager(data_dir=tmp_path)
    await reloaded.initialize()

    loaded_record = await reloaded.get_execution_record("task-1")
    loaded_subtasks = await reloaded.get_subtask_records("task-1")
    loaded_checkpoints = await reloaded.get_task_checkpoints("task-1")

    assert loaded_record is not None
    assert loaded_record.status is TaskExecutionStatus.COMPLETED
    assert loaded_record.subtask_records[0].subtask_id == "subtask-1"
    assert loaded_subtasks["subtask-1"].status is SubtaskExecutionStatus.COMPLETED
    assert len(loaded_checkpoints) == 1
    assert loaded_checkpoints[0].checkpoint_type is CheckpointType.POST_EXECUTION

