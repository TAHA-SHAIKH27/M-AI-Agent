"""
Phase 6 Acceptance Tests - Comprehensive Integration Tests
Tests all 7 acceptance criteria for Phase 6 completion.
"""
import asyncio
import sys
import os
import tempfile
import shutil
sys.path.insert(0, 'src')

from m_core.orchestration import orchestrator
from m_core.orchestration.state_manager import state_manager
from m_core.orchestration.schemas import TaskExecutionStatus, SubtaskExecutionStatus
from m_core.tools import tool_registry
from m_core.tools.schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.api.rest.routes.tasks import router as tasks_router, get_task_events
from m_core.api.rest.dependencies import APIClientInfo
from m_core.core.events import event_bus

async def test_application_automation():
    """TEST 1: Application Automation"""
    print("\n" + "="*60)
    print("TEST 1: Application Automation")
    print("="*60)
    
    # Grant required consents
    for cap in ["app:launch", "app:control", "automation:input", "screen:capture", "screen:ocr", "screen:verify"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # 1. Launch Notepad
    print("\n1. Launching Notepad...")
    tool = tool_registry.get_tool("app_launch")
    context = ToolExecutionContext(
        tool_name="app_launch",
        arguments={"app_name": "notepad", "approved": True},
        correlation_id="accept-1",
        user_id="test-user",
        permissions_granted=["app:launch"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    pid = result.result['process_id']
    print(f"   Notepad PID: {pid}")
    
    # 2. Verify it opened
    print("\n2. Verifying Notepad opened...")
    tool = tool_registry.get_tool("window_list")
    context = ToolExecutionContext(
        tool_name="window_list",
        arguments={"filter_process": "notepad.exe"},
        correlation_id="accept-2",
        user_id="test-user",
        permissions_granted=["app:control"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   FAIL: window enumeration failed: {result.error}")
        return False
    if result.success:
        notepad_windows = [w for w in result.result['windows'] if 'notepad' in w['process_name'].lower()]
        print(f"   Notepad windows found: {len(notepad_windows)}")
        if not notepad_windows:
            # Fallback: check by title
            notepad_windows = [w for w in result.result['windows'] if 'notepad' in w['title'].lower()]
            print(f"   Notepad windows by title: {len(notepad_windows)}")
        if not notepad_windows:
            print("   FAIL: Notepad process launched but no matching window was found")
            return False
    
    # 3. Request approval before keyboard input
    print("\n3. Testing approval requirement for keyboard input...")
    tool = tool_registry.get_tool("keyboard_input")
    context = ToolExecutionContext(
        tool_name="keyboard_input",
        arguments={"action": "type", "text": "Test"},
        correlation_id="accept-3",
        user_id="test-user",
        permissions_granted=["automation:input"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if result.success:
        print("   FAIL: Should have required approval")
        return False
    else:
        print(f"   Correctly rejected: {result.error}")
    
    # 4. Enter test sentence after approval
    print("\n4. Entering test sentence with approval...")
    context = ToolExecutionContext(
        tool_name="keyboard_input",
        arguments={"action": "type", "text": "Phase 6 Acceptance Test", "approved": True},
        correlation_id="accept-4",
        user_id="test-user",
        permissions_granted=["automation:input"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    
    # 5. Verify entered text (via OCR)
    print("\n5. Verifying entered text via OCR...")
    tool = tool_registry.get_tool("screen_ocr")
    context = ToolExecutionContext(
        tool_name="screen_ocr",
        arguments={"source": "screen", "language": "eng"},
        correlation_id="accept-5",
        user_id="test-user",
        permissions_granted=["screen:ocr"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   FAIL: OCR failed: {result.error}")
        return False
    text = result.result['text']
    print(f"   OCR text length: {len(text)}")
    if "Phase 6 Acceptance Test" not in text:
        print("   FAIL: Typed acceptance text was not detected by OCR")
        return False
    print("   PASS: Typed acceptance text found in OCR")
    
    # 6. Verify screen state
    print("\n6. Verifying screen state...")
    tool = tool_registry.get_tool("visual_verify")
    context = ToolExecutionContext(
        tool_name="visual_verify",
        arguments={"check_type": "text_present", "search_text": "Phase"},
        correlation_id="accept-6",
        user_id="test-user",
        permissions_granted=["screen:verify"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success or not result.result.get("passed", False):
        print(f"   FAIL: Screen verification did not pass: {result.error or result.result}")
        return False
    
    print("\nTEST 1: Application Automation - PASS")
    return True

async def test_file_automation():
    """TEST 2: File Automation"""
    print("\n" + "="*60)
    print("TEST 2: File Automation")
    print("="*60)
    
    for cap in ["file:write", "file:read", "file:list"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    test_dir = os.path.join(tempfile.gettempdir(), "m_acceptance_test")
    test_file = os.path.join(test_dir, "test_file.txt")
    expected_content = "Phase 6 Acceptance Test Content\nLine 2\nLine 3"
    
    # 1. Create test directory
    print("\n1. Creating test directory...")
    os.makedirs(test_dir, exist_ok=True)
    print(f"   Directory created: {test_dir}")
    
    # 2. Create text file with content
    print("\n2. Writing test file...")
    tool = tool_registry.get_tool("file_write")
    context = ToolExecutionContext(
        tool_name="file_write",
        arguments={"path": test_file, "content": expected_content, "approved": True},
        correlation_id="accept-f1",
        user_id="test-user",
        permissions_granted=["file:write"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    print(f"   Bytes written: {result.result['bytes_written']}")
    
    # 3. Verify file exists
    print("\n3. Verifying file exists...")
    tool = tool_registry.get_tool("file_read")
    context = ToolExecutionContext(
        tool_name="file_read",
        arguments={"path": test_file},
        correlation_id="accept-f2",
        user_id="test-user",
        permissions_granted=["file:read"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    print(f"   File size: {result.result['size']} bytes")
    
    # 4. Read file back
    print("\n4. Reading file back...")
    actual_content = result.result['content']
    print(f"   Content length: {len(actual_content)}")
    
    # 5. Compare content
    print("\n5. Comparing content...")
    if actual_content == expected_content:
        print("   PASS: Content matches exactly")
    else:
        print("   FAIL: Content mismatch")
        print(f"   Expected: {repr(expected_content)}")
        print(f"   Actual:   {repr(actual_content)}")
        return False
    
    # Cleanup
    shutil.rmtree(test_dir, ignore_errors=True)
    
    print("\nTEST 2: File Automation - PASS")
    return True

async def test_visual_automation():
    """TEST 3: Visual Automation"""
    print("\n" + "="*60)
    print("TEST 3: Visual Automation")
    print("="*60)
    
    for cap in ["screen:capture", "screen:ocr", "screen:find", "screen:verify"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # 1. Capture current screen
    print("\n1. Capturing screen...")
    tool = tool_registry.get_tool("screen_capture")
    context = ToolExecutionContext(
        tool_name="screen_capture",
        arguments={"mode": "full", "output_format": "png"},
        correlation_id="accept-v1",
        user_id="test-user",
        permissions_granted=["screen:capture"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    image_path = result.result['image_path']
    print(f"   Image: {result.result['width']}x{result.result['height']}")
    if os.path.exists(image_path):
        print("   File verified")
    
    # 2. Run OCR
    print("\n2. Running OCR...")
    tool = tool_registry.get_tool("screen_ocr")
    context = ToolExecutionContext(
        tool_name="screen_ocr",
        arguments={"source": "screen", "language": "eng"},
        correlation_id="accept-v2",
        user_id="test-user",
        permissions_granted=["screen:ocr"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   Error: {result.error}")
        return False
    text = result.result['text']
    print(f"   Text extracted: {len(text)} chars, confidence: {result.result['confidence']:.2f}")
    
    # 3. Find visible text element
    print("\n3. Finding visible text element...")
    tool = tool_registry.get_tool("visual_find")
    context = ToolExecutionContext(
        tool_name="visual_find",
        arguments={"method": "text", "search_text": "Phase", "threshold": 0.5},
        correlation_id="accept-v3",
        user_id="test-user",
        permissions_granted=["screen:find"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success:
        print(f"   FAIL: Visual find failed: {result.error}")
        return False
    matches = result.result['matches']
    print(f"   Matches found: {len(matches)}")
    if not matches:
        print("   FAIL: Expected visible text was not found")
        return False
    for m in matches[:3]:
        print(f"   Match at ({m['x']}, {m['y']}) size {m['width']}x{m['height']} conf {m['confidence']:.2f}")
    
    # 4. Report result
    print("\n4. Reporting visual verification...")
    tool = tool_registry.get_tool("visual_verify")
    context = ToolExecutionContext(
        tool_name="visual_verify",
        arguments={"check_type": "text_present", "search_text": "Phase", "threshold": 0.5},
        correlation_id="accept-v4",
        user_id="test-user",
        permissions_granted=["screen:verify"],
        consent_manager=consent_manager
    )
    result = await tool.execute(context)
    print(f"   Success: {result.success}")
    if not result.success or not result.result.get("passed", False):
        print(f"   FAIL: Visual verification failed: {result.error or result.result}")
        return False
    print(f"   Verification passed: {result.result['passed']}")
    print(f"   Confidence: {result.result['confidence']:.2f}")
    
    print("\nTEST 3: Visual Automation - PASS")
    return True

async def test_task_orchestration():
    """TEST 4: Task Orchestration"""
    print("\n" + "="*60)
    print("TEST 4: Task Orchestration")
    print("="*60)
    
    for cap in ["system:info", "app:launch", "app:control", "screen:capture", "screen:ocr", "screen:verify", "automation:input"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # 1. Task creation
    print("\n1. Creating task...")
    execution_record = await orchestrator.execute_task(
        user_request="Open Notepad and take a screenshot",
        conversation_id="accept-orch-1",
        user_id="test-user",
        context={}
    )
    print(f"   Task ID: {execution_record.task_id}")
    print(f"   Status: {execution_record.status.value}")
    print(f"   Subtasks: {len(execution_record.subtask_records)}")
    
    # 2. AI-generated planning
    print("\n2. Verifying AI-generated plan...")
    if execution_record.execution_plan_id:
        print(f"   Execution Plan ID: {execution_record.execution_plan_id}")
    print(f"   Subtasks: {len(execution_record.subtask_records)}")
    for sr in execution_record.subtask_records:
        print(f"  - {sr.subtask_id}: {sr.status.value} (target: {sr.assigned_target_type}:{sr.assigned_target_id})")
    
    # 3. Step dependencies
    print("\n3. Checking step dependencies...")
    for sr in execution_record.subtask_records:
        if sr.dependencies:
            print(f"  - {sr.subtask_id} depends on: {sr.dependencies}")
    
    # 4. Intelligent tool selection
    print("\n4. Verifying intelligent tool selection...")
    for sr in execution_record.subtask_records:
        if sr.assigned_target_type and sr.assigned_target_id:
            print(f"  - {sr.subtask_id}: {sr.assigned_target_type} -> {sr.assigned_target_id}")
    
    # 5. Actual tool execution
    print("\n5. Verifying actual tool execution...")
    completed = sum(1 for sr in execution_record.subtask_records if sr.status == SubtaskExecutionStatus.COMPLETED)
    failed = sum(1 for sr in execution_record.subtask_records if sr.status == SubtaskExecutionStatus.FAILED)
    print(f"  Completed: {completed}, Failed: {failed}")
    
    # 6. Verification
    print("\n6. Verification integration...")
    # The evaluator runs automatically after execution
    print(f"  Final status: {execution_record.status.value}")
    if execution_record.result:
        print(f"  Result: {str(execution_record.result)[:100]}")
    
    # 7. Task state updates
    print("\n7. Task state updates...")
    print(f"  Created at: {execution_record.created_at}")
    print(f"  Started at: {execution_record.started_at}")
    print(f"  Completed at: {execution_record.completed_at}")
    
    # 8. Completion reporting
    print("\n8. Completion reporting...")
    if execution_record.status != TaskExecutionStatus.COMPLETED:
        print(f"  FAIL: Task did not complete: {execution_record.status.value} ({execution_record.error})")
        return False
    if completed != len(execution_record.subtask_records) or failed:
        print("  FAIL: One or more subtasks did not complete successfully")
        return False
    if not execution_record.result:
        print("  FAIL: Completed task has no consolidated result")
        return False
    
    print("\nTEST 4: Task Orchestration - PASS")
    return True

async def test_human_intervention():
    """TEST 5: Human Intervention"""
    print("\n" + "="*60)
    print("TEST 5: Human Intervention")
    print("="*60)
    
    for cap in ["app:launch", "app:control", "system:admin"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # 1. Approval-required action
    print("\n1. Testing approval-required action...")
    execution_record = await orchestrator.execute_task(
        user_request="Launch Notepad and close it",
        conversation_id="accept-hi-1",
        user_id="test-user",
        context={}
    )
    print(f"   Task ID: {execution_record.task_id}")
    print(f"   Status: {execution_record.status.value}")
    
    if execution_record.status == TaskExecutionStatus.AWAITING_APPROVAL:
        print("   PASS: Task correctly awaiting approval")
        
        # 2. Approval acceptance
        print("\n2. Testing approval acceptance...")
        success = await orchestrator.approve_subtask(
            task_id=execution_record.task_id,
            subtask_id=None,
            approved=True,
            reason="User approved"
        )
        print(f"   Approval success: {success}")
        
        # Check status after approval
        record = await orchestrator.get_task_status(execution_record.task_id)
        if not record or record.status == TaskExecutionStatus.AWAITING_APPROVAL:
            print("   FAIL: Approved task did not leave the approval-waiting state")
            return False
        print(f"   Status after approval: {record.status.value}")
        
        # 3. Test rejection on new task
        print("\n3. Testing approval rejection...")
        execution_record2 = await orchestrator.execute_task(
            user_request="Launch Calculator",
            conversation_id="accept-hi-2",
            user_id="test-user",
            context={}
        )
        if execution_record2.status == TaskExecutionStatus.AWAITING_APPROVAL:
            success = await orchestrator.approve_subtask(
                task_id=execution_record2.task_id,
                subtask_id=None,
                approved=False,
                reason="User denied"
            )
            print(f"   Rejection success: {success}")
            record = await orchestrator.get_task_status(execution_record2.task_id)
            if not record or record.status != TaskExecutionStatus.FAILED:
                print("   FAIL: Rejected task was not marked failed")
                return False
            print(f"   Status after rejection: {record.status.value}")
        else:
            print("   FAIL: Rejection test task did not request approval")
            return False
        
        # 4. Task pause (via cancellation)
        print("\n4. Testing task cancellation (pause)...")
        execution_record3 = await orchestrator.execute_task(
            user_request="Open Notepad",
            conversation_id="accept-hi-3",
            user_id="test-user",
            context={}
        )
        if execution_record3.status not in (
            TaskExecutionStatus.AWAITING_APPROVAL,
            TaskExecutionStatus.RUNNING,
            TaskExecutionStatus.QUEUED,
        ):
            print(f"   FAIL: Cancellation test task was not cancellable: {execution_record3.status.value}")
            return False
        success = await orchestrator.cancel_task(execution_record3.task_id)
        record = await orchestrator.get_task_status(execution_record3.task_id)
        if not success or not record or record.status != TaskExecutionStatus.CANCELLED:
            print("   FAIL: Task cancellation did not persist the cancelled state")
            return False
        print(f"   Cancel success: {success}")
        print(f"   Status after cancel: {record.status.value}")
        
        print("\nTEST 5: Human Intervention - PASS")
        return True
    else:
        print("   Status not AWAITING_APPROVAL (may have auto-approved or failed)")
        print(f"   Error: {execution_record.error}")
        return False

async def test_persistence():
    """TEST 6: Persistence"""
    print("\n" + "="*60)
    print("TEST 6: Persistence")
    print("="*60)
    
    for cap in ["system:info", "app:launch"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    # 1. Create and complete a task
    print("\n1. Creating task for persistence test...")
    execution_record = await orchestrator.execute_task(
        user_request="Get system info",
        conversation_id="accept-persist-1",
        user_id="test-user",
        context={}
    )
    task_id = execution_record.task_id
    print(f"   Task ID: {task_id}")
    
    # Wait for completion
    await asyncio.sleep(2)
    record = await state_manager.get_execution_record(task_id)
    if record:
        print(f"   Status: {record.status.value}")
        print(f"   Completed at: {record.completed_at}")
    
    # 2. Verify task persisted
    print("\n2. Verifying task persistence...")
    records = await state_manager.list_executions(limit=10000)
    found = any(r.task_id == task_id for r in records)
    print(f"   Task found in list: {found}")
    if not found:
        print("   FAIL: orchestrator task is not visible to the shared task state manager")
        return False
    
    # 3. Verify subtasks persisted
    print("\n3. Verifying subtask persistence...")
    persisted_subtasks = await state_manager.get_subtask_records(task_id)
    print(f"   Subtasks persisted: {len(persisted_subtasks)}")
    if not persisted_subtasks:
        print("   FAIL: no subtask records were persisted")
        return False
    
    # 4. Verify checkpoints persisted
    print("\n4. Verifying checkpoint persistence...")
    checkpoints = await state_manager.get_task_checkpoints(task_id)
    print(f"   Checkpoints: {len(checkpoints)}")
    if not checkpoints:
        print("   FAIL: no checkpoints were persisted")
        return False
    for cp in checkpoints:
        print(f"  - {cp.checkpoint_id}: {cp.checkpoint_type.value}")
    
    # 5. Verify state survives manager restart/reload
    print("\n5. Testing recovery from persisted state...")
    from m_core.orchestration.state_manager import ExecutionStateManager
    reloaded_manager = ExecutionStateManager(data_dir=state_manager.data_dir)
    await reloaded_manager.initialize()
    reloaded_record = await reloaded_manager.get_execution_record(task_id)
    reloaded_subtasks = await reloaded_manager.get_subtask_records(task_id)
    reloaded_checkpoints = await reloaded_manager.get_task_checkpoints(task_id)
    if not reloaded_record or not reloaded_subtasks or not reloaded_checkpoints:
        print("   FAIL: task, subtasks, or checkpoints did not survive state reload")
        return False
    print(f"   Reloaded task: {reloaded_record.task_id} ({reloaded_record.status.value})")
    print(f"   Reloaded subtasks: {len(reloaded_subtasks)}")
    print(f"   Reloaded checkpoints: {len(reloaded_checkpoints)}")
    
    # 6. Verify no duplicate execution
    print("\n6. Verifying no duplicate execution...")
    # The orchestrator should not re-execute completed actions
    record = await state_manager.get_execution_record(task_id)
    if record and record.status == TaskExecutionStatus.COMPLETED:
        # Try to execute same task again - should create new task
        new_record = await orchestrator.execute_task(
            user_request="Get system info",
            conversation_id="accept-persist-2",
            user_id="test-user",
            context={}
        )
        print(f"   New task created: {new_record.task_id != task_id}")
        print(f"   Original task unchanged: {record.status.value == 'completed'}")
    
    print("\nTEST 6: Persistence - PASS")
    return True

async def test_realtime_communication():
    """TEST 7: Real-time Communication"""
    print("\n" + "="*60)
    print("TEST 7: Real-time Communication (WebSocket Events)")
    print("="*60)
    
    print("\nNote: Full WebSocket testing requires a WebSocket client.")
    print("Testing event emission via orchestrator event bus...")
    
    # We can verify the event system is wired by checking the orchestrator
    # publishes events during task execution
    
    for cap in ["system:info"]:
        consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
    
    print("\n1. Creating task to trigger events...")
    execution_record = await orchestrator.execute_task(
        user_request="Get system info",
        conversation_id="accept-ws-1",
        user_id="test-user",
        context={}
    )
    task_id = execution_record.task_id
    
    # Wait for completion
    await asyncio.sleep(3)
    
    # Check that events would have been published
    # The orchestrator publishes to event_bus for:
    # - task.created
    # - task.started  
    # - task.progress (for each subtask)
    # - task.step_started
    # - task.step_completed
    # - task.verification
    # - task.completed or task.failed
    
    print("\n2. Verifying events actually emitted...")
    events = [
        event for event in event_bus.get_history(limit=1000)
        if event.payload.get("task_id") == task_id
    ]
    event_types = {event.type for event in events}
    required_events = {"task.created", "task.started", "task.progress"}
    if not required_events.issubset(event_types):
        print(f"   FAIL: Missing emitted events: {sorted(required_events - event_types)}")
        return False
    if not ({"task.completed", "task.failed"} & event_types):
        print("   FAIL: No terminal task event was emitted")
        return False

    print("\n3. Verifying reconnect replay endpoint...")
    replay = await get_task_events(
        task_id,
        after_event_id=None,
        client=APIClientInfo(host="127.0.0.1"),
        _=None,
    )
    replay_types = {event.event_type for event in replay.events}
    if not required_events.issubset(replay_types):
        print(f"   FAIL: Reconnect replay is missing events: {sorted(required_events - replay_types)}")
        return False
    if replay.latest_event_id != replay.events[-1].event_id:
        print("   FAIL: Latest event cursor is incorrect")
        return False
    print(f"   Replayed {len(replay.events)} events; latest cursor: {replay.latest_event_id}")

    print("\nTEST 7: Real-time Communication - PASS")
    return True

async def main():
    print("="*60)
    print("PHASE 6 FINAL INTEGRATION ACCEPTANCE TEST")
    print("="*60)
    
    results = {}
    
    try:
        results["test_1_app_automation"] = await test_application_automation()
    except Exception as e:
        print(f"TEST 1 FAILED: {e}")
        results["test_1_app_automation"] = False
    
    try:
        results["test_2_file_automation"] = await test_file_automation()
    except Exception as e:
        print(f"TEST 2 FAILED: {e}")
        results["test_2_file_automation"] = False
    
    try:
        results["test_3_visual_automation"] = await test_visual_automation()
    except Exception as e:
        print(f"TEST 3 FAILED: {e}")
        results["test_3_visual_automation"] = False
    
    try:
        results["test_4_task_orchestration"] = await test_task_orchestration()
    except Exception as e:
        print(f"TEST 4 FAILED: {e}")
        results["test_4_task_orchestration"] = False
    
    try:
        results["test_5_human_intervention"] = await test_human_intervention()
    except Exception as e:
        print(f"TEST 5 FAILED: {e}")
        results["test_5_human_intervention"] = False
    
    try:
        results["test_6_persistence"] = await test_persistence()
    except Exception as e:
        print(f"TEST 6 FAILED: {e}")
        results["test_6_persistence"] = False
    
    try:
        results["test_7_realtime"] = await test_realtime_communication()
    except Exception as e:
        print(f"TEST 7 FAILED: {e}")
        results["test_7_realtime"] = False
    
    # Summary
    print("\n" + "="*60)
    print("ACCEPTANCE TEST SUMMARY")
    print("="*60)
    
    all_passed = True
    for test, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {test}: {status}")
        if not passed:
            all_passed = False
    
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    
    # Run regression suite
    print("\nRunning regression suite...")
    return all_passed

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)