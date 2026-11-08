import asyncio
import sys
sys.path.insert(0, 'src')

from m_core.orchestration import orchestrator
from m_core.orchestration.state_manager import state_manager
from m_core.orchestration.schemas import TaskExecutionStatus
from m_core.security import consent_manager, ConsentScope

async def test_task_board():
    print("=== Testing Task Board API ===")
    
    # Grant all required consents
    for cap in ["system:info", "app:control", "app:launch", "system:admin", "file:write", 
                "voice:tts", "voice:stt", "voice:mic", "screen:capture", "screen:ocr", 
                "screen:find", "automation:input", "automation:window", "screen:verify"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
        print(f"  Granted: {cap}")
    
    # Create a task
    print("\n=== Creating Task ===")
    execution_record = await orchestrator.execute_task(
        user_request="Open Notepad and take a screenshot",
        conversation_id="test-board-1",
        user_id="test-user",
        context={}
    )
    
    print(f"Task ID: {execution_record.task_id}")
    print(f"Status: {execution_record.status.value}")
    print(f"Subtasks: {len(execution_record.subtask_records)}")
    
    # Test task board API - list tasks
    print("\n=== Testing Task List ===")
    from m_core.orchestration.state_manager import state_manager
    records = await state_manager.list_executions(limit=10)
    print(f"Total tasks: {len(records)}")
    for r in records[:3]:
        print(f"  Task {r.task_id}: {r.status.value}")
    
    # Test task stats
    print("\n=== Testing Task Stats ===")
    all_records = await state_manager.list_executions(limit=10000)
    total = len(all_records)
    by_status = {}
    for r in all_records:
        status = r.status.value
        by_status[status] = by_status.get(status, 0) + 1
    
    print(f"Total: {total}")
    print(f"By status: {by_status}")
    
    # Test task detail
    print("\n=== Testing Task Detail ===")
    task_id = execution_record.task_id
    record = await state_manager.get_execution_record(task_id)
    if record:
        print(f"Task {task_id}: {record.status.value}")
        print(f"  Subtasks: {len(record.subtask_records)}")
        for sr in record.subtask_records:
            print(f"  - {sr.subtask_id}: {sr.status.value}")
    
    # Test checkpoints
    print("\n=== Testing Checkpoints ===")
    checkpoints = await state_manager.get_task_checkpoints(task_id)
    print(f"Checkpoints: {len(checkpoints)}")
    for cp in checkpoints:
        print(f"  {cp.checkpoint_id}: {cp.checkpoint_type.value} at {cp.created_at}")
    
    # Test cancel
    print("\n=== Testing Cancel ===")
    success = await orchestrator.cancel_task(task_id)
    print(f"Cancel success: {success}")
    
    # Verify cancel
    record = await state_manager.get_execution_record(task_id)
    if record:
        print(f"Status after cancel: {record.status.value}")
    
    print("\n=== Task Board Tests Complete ===")

if __name__ == "__main__":
    asyncio.run(test_task_board())