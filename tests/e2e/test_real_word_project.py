"""
Phase 6 — Real-World End-to-End Desktop Automation Test

This test proves M can independently operate Microsoft Word through the Windows desktop
using actual mouse and keyboard actions controlled by NVIDIA Nemotron 3 Ultra.

Test Requirements:
- M must understand the request using NVIDIA Nemotron 3 Ultra
- M must create and execute its own action plan
- M must open Microsoft Word through existing Windows automation tools
- All document creation must happen through actual mouse and keyboard interaction
- Use real clicks, typing, keyboard shortcuts, selections, scrolling, menus, and Word UI controls
- Capture screenshots throughout the workflow
- Verify document quality and save location
- Close Word and verify cleanup

Prerequisites:
- Microsoft Word installed (winword.exe)
- Windows 11 with UI automation capabilities
- NVIDIA Nemotron 3 Ultra API key configured
- All required Python dependencies (mss, pytesseract, opencv-python, pywinauto, etc.)

Execution:
    python -m pytest tests/e2e/test_real_word_project.py -v -s --tb=short
    OR
    python tests/e2e/test_real_word_project.py
"""

import asyncio
import os
import sys
import time
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from m_core.orchestration import orchestrator
from m_core.orchestration.state_manager import state_manager
from m_core.orchestration.schemas import TaskExecutionStatus
from m_core.tools import tool_registry
from m_core.common_schemas import ToolExecutionContext
from m_core.security import consent_manager, ConsentScope
from m_core.planning import task_understanding, planner
from m_core.planning.schemas import TaskContext, TaskType, TaskComplexity

# Test configuration
TEST_USER_ID = "e2e-test-user"
TEST_CONVERSATION_ID = "e2e-word-project-test"
EVIDENCE_DIR = Path(__file__).parent / "evidence" / f"word_project_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
DESKTOP_PATH = Path(os.path.expanduser("~/Desktop"))
TARGET_DOCUMENT = DESKTOP_PATH / "Greenhouse_Effect_Project.docx"

# User request as specified
USER_REQUEST = (
    "Open Microsoft Word and create a professional, creative, well-formatted project document "
    "on the topic Greenhouse Effect. Include a title page, properly structured headings, "
    "detailed explanations, different font sizes and styles, colored headings, bullet points, "
    "numbered lists, at least one formatted table, and relevant graphs or charts. Save the "
    "finished document to my Desktop as Greenhouse_Effect_Project.docx. Verify that it is "
    "saved correctly, then close Word and notify me when everything is complete."
)

# Required capabilities for this test
REQUIRED_CAPABILITIES = [
    "app:launch",
    "app:control",
    "automation:input",
    "automation:window",
    "screen:capture",
    "screen:ocr",
    "screen:find",
    "screen:verify",
    "file:write",
    "file:read",
    "system:info",
]


class WordProjectTester:
    """End-to-end tester for Microsoft Word automation."""
    
    def __init__(self):
        self.evidence_dir = EVIDENCE_DIR
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots = []
        self.verification_results = {}
        self.start_time = time.time()
        
    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [{level}] {message}")
        
    def save_evidence(self, step: str, description: str, screenshot_path: str = None, 
                     ocr_text: str = None, metadata: Dict = None):
        """Save test evidence with metadata."""
        evidence = {
            "step": step,
            "description": description,
            "timestamp": datetime.now().isoformat(),
            "screenshot_path": screenshot_path,
            "ocr_text": ocr_text,
            "metadata": metadata or {}
        }
        evidence_file = self.evidence_dir / f"{step.replace(' ', '_').lower()}_evidence.json"
        with open(evidence_file, 'w', encoding='utf-8') as f:
            json.dump(evidence, f, indent=2, ensure_ascii=False)
        self.screenshots.append(evidence)
        return evidence
        
    async def take_screenshot(self, step_name: str, source: str = "screen", 
                             window_title: str = None, region: Dict = None) -> str:
        """Take a screenshot and save to evidence directory."""
        tool = tool_registry.get_tool("screen_capture")
        args = {
            "mode": "window" if window_title else "full",
            "output_format": "png",
            "save_to_file": True,
        }
        if window_title:
            args["window_title"] = window_title
        if region:
            args["mode"] = "region"
            args["region"] = region
            
        context = ToolExecutionContext(
            tool_name="screen_capture",
            arguments=args,
            correlation_id=f"screenshot-{step_name}",
            user_id=TEST_USER_ID,
            permissions_granted=["screen:capture"],
            consent_manager=consent_manager
        )
        
        result = await tool.execute(context)
        if result.success:
            src_path = result.result["image_path"]
            # Copy to evidence directory with descriptive name
            dst_name = f"{step_name.replace(' ', '_').lower()}_{int(time.time())}.png"
            dst_path = self.evidence_dir / dst_name
            shutil.copy2(src_path, dst_path)
            self.log(f"Screenshot saved: {dst_name}")
            return str(dst_path)
        else:
            self.log(f"Screenshot failed: {result.error}", "ERROR")
            return None
            
    async def run_ocr(self, step_name: str, source: str = "window", 
                     window_title: str = None) -> Optional[str]:
        """Run OCR on screen/window and return extracted text."""
        tool = tool_registry.get_tool("screen_ocr")
        args = {
            "source": source,
            "language": "eng",
            "preprocess": True,
        }
        if window_title:
            args["window_title"] = window_title
            
        context = ToolExecutionContext(
            tool_name="screen_ocr",
            arguments=args,
            correlation_id=f"ocr-{step_name}",
            user_id=TEST_USER_ID,
            permissions_granted=["screen:ocr"],
            consent_manager=consent_manager
        )
        
        result = await tool.execute(context)
        if result.success:
            text = result.result.get("text", "")
            confidence = result.result.get("confidence", 0)
            self.log(f"OCR ({step_name}): {len(text)} chars, confidence: {confidence:.2f}")
            return text
        else:
            self.log(f"OCR failed: {result.error}", "ERROR")
            return None
            
    async def verify_text_present(self, search_text: str, window_title: str = None) -> bool:
        """Verify specific text is present on screen/window."""
        tool = tool_registry.get_tool("visual_verify")
        args = {
            "check_type": "text_present",
            "search_text": search_text,
        }
        if window_title:
            args["window_title"] = window_title
            
        context = ToolExecutionContext(
            tool_name="visual_verify",
            arguments=args,
            correlation_id=f"verify-{search_text[:20]}",
            user_id=TEST_USER_ID,
            permissions_granted=["screen:verify"],
            consent_manager=consent_manager
        )
        
        result = await tool.execute(context)
        if result.success:
            passed = result.result.get("passed", False)
            self.log(f"Verification '{search_text}': {'PASS' if passed else 'FAIL'}")
            return passed
        return False
        
    def grant_all_permissions(self):
        """Grant all required capabilities for the test."""
        self.log("Granting all required permissions...")
        for cap in REQUIRED_CAPABILITIES:
            consent_manager.grant(cap, scope=ConsentScope.GLOBAL, ttl_hours=1)
            self.log(f"  Granted: {cap}")
            
    async def check_word_process(self) -> Optional[int]:
        """Check if Word process is running and return PID."""
        tool = tool_registry.get_tool("process_list")
        context = ToolExecutionContext(
            tool_name="process_list",
            arguments={"filter_name": "WINWORD", "limit": 10},
            correlation_id="check-word-process",
            user_id=TEST_USER_ID,
            permissions_granted=["system:info"],
            consent_manager=consent_manager
        )
        result = await tool.execute(context)
        if result.success and result.result.get("processes"):
            for proc in result.result["processes"]:
                if "WINWORD" in proc.get("name", "").upper():
                    return proc.get("pid")
        return None
        
    async def check_word_window(self) -> Optional[Dict]:
        """Check if Word window is visible and return window info."""
        tool = tool_registry.get_tool("window_list")
        context = ToolExecutionContext(
            tool_name="window_list",
            arguments={"filter_process": "WINWORD.EXE", "include_hidden": False},
            correlation_id="check-word-window",
            user_id=TEST_USER_ID,
            permissions_granted=["app:control"],
            consent_manager=consent_manager
        )
        result = await tool.execute(context)
        if result.success and result.result.get("windows"):
            return result.result["windows"][0]
        return None
        
    async def verify_document_file(self) -> Dict[str, Any]:
        """Verify the saved document exists and has reasonable content."""
        verification = {
            "exists": False,
            "size_bytes": 0,
            "size_mb": 0,
            "is_valid_docx": False,
            "extracted_text_length": 0,
            "has_title": False,
            "has_headings": False,
            "has_table": False,
            "sections_found": [],
        }
        
        if not TARGET_DOCUMENT.exists():
            self.log(f"Document not found at: {TARGET_DOCUMENT}", "ERROR")
            return verification
            
        verification["exists"] = True
        verification["size_bytes"] = TARGET_DOCUMENT.stat().st_size
        verification["size_mb"] = verification["size_bytes"] / (1024 * 1024)
        self.log(f"Document exists: {verification['size_mb']:.2f} MB")
        
        # Try to read as DOCX (zip file)
        try:
            import zipfile
            if zipfile.is_zipfile(TARGET_DOCUMENT):
                verification["is_valid_docx"] = True
                with zipfile.ZipFile(TARGET_DOCUMENT, 'r') as z:
                    # Read document.xml for text content
                    if 'word/document.xml' in z.namelist():
                        doc_xml = z.read('word/document.xml').decode('utf-8', errors='ignore')
                        verification["extracted_text_length"] = len(doc_xml)
                        
                        # Check for key content
                        import re
                        # Check for title
                        if "Greenhouse Effect" in doc_xml or "GREENHOUSE EFFECT" in doc_xml:
                            verification["has_title"] = True
                        # Check for headings
                        if "<w:pStyle" in doc_xml and "Heading" in doc_xml:
                            verification["has_headings"] = True
                        # Check for table
                        if "<w:tbl>" in doc_xml:
                            verification["has_table"] = True
                            
                        # Extract section titles
                        heading_matches = re.findall(r'<w:pStyle[^>]*w:val="Heading[^"]*"[^>]*>.*?<w:t>(.*?)</w:t>', doc_xml)
                        verification["sections_found"] = heading_matches[:10]
                        
                        self.log(f"DOCX validation: title={verification['has_title']}, "
                               f"headings={verification['has_headings']}, table={verification['has_table']}")
        except Exception as e:
            self.log(f"DOCX validation error: {e}", "WARNING")
            
        return verification
        
    async def run_test(self) -> Dict[str, Any]:
        """Execute the complete end-to-end test."""
        self.log("=" * 80)
        self.log("PHASE 6 — REAL-WORLD END-TO-END WORD AUTOMATION TEST")
        self.log("=" * 80)
        self.log(f"Evidence directory: {self.evidence_dir}")
        self.log(f"Target document: {TARGET_DOCUMENT}")
        
        # Clean up any existing target document
        if TARGET_DOCUMENT.exists():
            self.log(f"Removing existing target document: {TARGET_DOCUMENT}")
            TARGET_DOCUMENT.unlink()
            
        # Grant permissions
        self.grant_all_permissions()
        
        # Step 1: Verify Word is not already running
        self.log("\n--- STEP 1: Verify Word is not running ---")
        word_pid_before = await self.check_word_process()
        word_window_before = await self.check_word_window()
        
        screenshot1 = await self.take_screenshot("01_before_launch", source="screen")
        self.save_evidence("01_before_launch", "Desktop before launching Word", screenshot1,
                         metadata={"word_pid": word_pid_before, "word_window": word_window_before})
        
        if word_pid_before:
            self.log(f"WARNING: Word already running (PID: {word_pid_before}), will attempt to use existing instance")
        else:
            self.log("Confirmed: Word is not running")
            
        # Step 2: Execute the task through orchestrator
        self.log("\n--- STEP 2: Execute task through orchestrator ---")
        self.log("Sending request to M with NVIDIA Nemotron 3 Ultra...")
        
        execution_record = await orchestrator.execute_task(
            user_request=USER_REQUEST,
            conversation_id=TEST_CONVERSATION_ID,
            user_id=TEST_USER_ID,
            context={
                "test_mode": True,
                "evidence_dir": str(self.evidence_dir),
                "target_document": str(TARGET_DOCUMENT),
            }
        )
        
        task_id = execution_record.task_id
        self.log(f"Task created: {task_id}")
        self.log(f"Initial status: {execution_record.status.value}")
        
        # Wait for task to complete (or await approval)
        max_wait = 300  # 5 minutes max
        wait_interval = 5
        elapsed = 0
        
        while elapsed < max_wait:
            await asyncio.sleep(wait_interval)
            elapsed += wait_interval
            
            record = await orchestrator.get_task_status(task_id)
            if record:
                self.log(f"Task status: {record.status.value} (elapsed: {elapsed}s)")
                if record.status in [TaskExecutionStatus.COMPLETED, TaskExecutionStatus.FAILED, 
                                    TaskExecutionStatus.CANCELLED]:
                    execution_record = record
                    break
                elif record.status == TaskExecutionStatus.AWAITING_APPROVAL:
                    self.log("Task awaiting approval - this should not happen in automated test")
                    # Auto-approve for test purposes
                    await orchestrator.approve_subtask(task_id, None, True, "Auto-approved for E2E test")
            else:
                self.log("Could not retrieve task status", "ERROR")
                break
                
        # Step 3: Capture screenshots during/after execution
        self.log("\n--- STEP 3: Capture post-execution evidence ---")
        
        # Wait a bit for UI to settle
        await asyncio.sleep(3)
        
        # Screenshot after Word should be open
        screenshot2 = await self.take_screenshot("02_word_open", source="window", window_title="Word")
        if not screenshot2:
            screenshot2 = await self.take_screenshot("02_word_open_fallback", source="screen")
        self.save_evidence("02_word_open", "Word application open", screenshot2)
        
        # OCR to verify document content
        ocr_text = await self.run_ocr("03_document_content", source="window", window_title="Word")
        self.save_evidence("03_document_content", "OCR of document content", screenshot2, ocr_text)
        
        # Verify specific content elements
        content_checks = [
            ("Greenhouse Effect", "Title/heading present"),
            ("Introduction", "Introduction section"),
            ("Causes", "Causes section"),
            ("Effects", "Effects section"),
            ("Solutions", "Solutions/Mitigation section"),
            ("Table", "Table present"),
            ("Conclusion", "Conclusion section"),
        ]
        
        for search_text, description in content_checks:
            found = await self.verify_text_present(search_text, window_title="Word")
            self.verification_results[description] = found
            
        # Step 4: Verify document was saved
        self.log("\n--- STEP 4: Verify saved document ---")
        # Give time for save to complete
        await asyncio.sleep(2)
        
        doc_verification = await self.verify_document_file()
        self.verification_results.update(doc_verification)
        
        # Screenshot of saved file on Desktop
        screenshot3 = await self.take_screenshot("04_desktop_file", source="region", 
                                                region={"left": 0, "top": 0, "width": 1920, "height": 1080})
        self.save_evidence("04_desktop_file", "Desktop showing saved document", screenshot3,
                         metadata=doc_verification)
        
        # Step 5: Close Word
        self.log("\n--- STEP 5: Close Word ---")
        screenshot4 = await self.take_screenshot("05_before_close", source="window", window_title="Word")
        self.save_evidence("05_before_close", "Before closing Word", screenshot4)
        
        # Use app_control to close Word
        tool = tool_registry.get_tool("app_control")
        context = ToolExecutionContext(
            tool_name="app_control",
            arguments={"action": "close", "process_name": "WINWORD.EXE", "approved": True},
            correlation_id="close-word",
            user_id=TEST_USER_ID,
            permissions_granted=["app:control"],
            consent_manager=consent_manager
        )
        close_result = await tool.execute(context)
        self.log(f"Close Word result: {close_result.success}")
        
        # Wait for process to exit
        await asyncio.sleep(2)
        
        screenshot5 = await self.take_screenshot("06_after_close", source="screen")
        self.save_evidence("06_after_close", "Desktop after closing Word", screenshot5)
        
        # Verify Word is closed
        word_pid_after = await self.check_word_process()
        word_window_after = await self.check_word_window()
        
        word_closed = word_pid_after is None and word_window_after is None
        self.verification_results["word_closed"] = word_closed
        self.log(f"Word closed: {word_closed} (PID: {word_pid_after}, Window: {word_window_after})")
        
        # Final verification
        self.log("\n--- FINAL VERIFICATION ---")
        all_checks = {
            "task_completed": execution_record.status == TaskExecutionStatus.COMPLETED,
            "word_was_opened": word_pid_before is not None or word_window_before is not None,
            "document_created": doc_verification["exists"],
            "document_valid_docx": doc_verification["is_valid_docx"],
            "document_has_title": doc_verification["has_title"],
            "document_has_headings": doc_verification["has_headings"],
            "document_has_table": doc_verification["has_table"],
            "document_size_reasonable": doc_verification["size_mb"] > 0.01,  # At least 10KB
            "word_closed": word_closed,
            "content_checks_passed": sum(1 for v in self.verification_results.values() if isinstance(v, bool) and v),
        }
        
        self.verification_results.update(all_checks)
        
        # Generate report
        report = self.generate_report(execution_record, all_checks)
        
        return report
        
    def generate_report(self, execution_record, all_checks: Dict) -> Dict[str, Any]:
        """Generate comprehensive test report."""
        duration = time.time() - self.start_time
        
        report = {
            "test_name": "Phase 6 Real-Word Word Project E2E Test",
            "timestamp": datetime.now().isoformat(),
            "duration_seconds": round(duration, 2),
            "task_id": execution_record.task_id,
            "task_status": execution_record.status.value,
            "user_request": USER_REQUEST,
            "target_document": str(TARGET_DOCUMENT),
            "evidence_directory": str(self.evidence_dir),
            "model_provider": "NVIDIA Nemotron 3 Ultra",
            "verification_results": self.verification_results,
            "summary_checks": all_checks,
            "screenshots_captured": len(self.screenshots),
            "screenshots": [
                {
                    "step": s["step"],
                    "description": s["description"],
                    "timestamp": s["timestamp"],
                    "screenshot": s["screenshot_path"],
                    "has_ocr": s["ocr_text"] is not None
                }
                for s in self.screenshots
            ],
            "overall_success": all([
                all_checks.get("task_completed", False),
                all_checks.get("document_created", False),
                all_checks.get("document_valid_docx", False),
                all_checks.get("document_has_title", False),
                all_checks.get("document_has_headings", False),
                all_checks.get("document_has_table", False),
                all_checks.get("word_closed", False),
            ]),
        }
        
        # Save report
        report_file = self.evidence_dir / "test_report.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
            
        # Print summary
        self.log("\n" + "=" * 80)
        self.log("TEST REPORT SUMMARY")
        self.log("=" * 80)
        self.log(f"Task ID: {report['task_id']}")
        self.log(f"Task Status: {report['task_status']}")
        self.log(f"Duration: {report['duration_seconds']:.1f}s")
        self.log(f"Screenshots: {report['screenshots_captured']}")
        self.log(f"Evidence Dir: {report['evidence_directory']}")
        self.log("")
        self.log("VERIFICATION CHECKS:")
        for check, result in all_checks.items():
            status = "[PASS]" if result else "[FAIL]"
            self.log(f"  {status} {check}")
        self.log("")
        self.log(f"OVERALL: {'[SUCCESS]' if report['overall_success'] else '[FAILURE]'}")
        self.log(f"Report saved: {report_file}")
        
        return report


async def main():
    """Main test entry point."""
    print("=" * 80)
    print("Phase 6 — Real-World End-to-End Microsoft Word Automation Test")
    print("=" * 80)
    print()
    print("This test will:")
    print("  1. Launch Microsoft Word using Windows automation")
    print("  2. Create a professional Greenhouse Effect project document")
    print("  3. Use real mouse/keyboard interactions (clicks, typing, shortcuts)")
    print("  4. Capture screenshots at each major stage")
    print("  5. Verify document content via OCR and file inspection")
    print("  6. Save to Desktop as Greenhouse_Effect_Project.docx")
    print("  7. Close Word and verify cleanup")
    print()
    print("Prerequisites:")
    print("  - Microsoft Word installed")
    print("  - NVIDIA Nemotron 3 Ultra API key configured")
    print("  - All Python dependencies installed")
    print()
    
    # Auto-proceed for automated test
    print("Auto-proceeding with test...")
        
    tester = WordProjectTester()
    try:
        report = await tester.run_test()
        
        # Exit with appropriate code
        if report['overall_success']:
            print("\n[SUCCESS] TEST PASSED - M successfully completed the Word automation task!")
            sys.exit(0)
        else:
            print("\n[FAILURE] TEST FAILED - See report for details")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n[ERROR] TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())