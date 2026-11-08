"""Result Evaluator for M AI Assistant."""
import asyncio
import structlog
from typing import Optional, List, Dict, Any
from datetime import datetime

from ..model_providers import provider_factory
from ..model_providers.schemas import (
    CompletionRequest, ChatMessage, MessageRole,
)
from ..memory import memory_manager
from ..memory.base import MemoryTier
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    EvaluationResult, SubtaskEvaluation, TaskEvaluation,
    SubtaskExecutionRecord, TaskExecutionRecord,
    SubtaskExecutionStatus,
)
from ..planning.schemas import (
    ExecutionPlan, Subtask, TaskAnalysis,
)

logger = get_logger(__name__)


# System prompt for evaluation
EVALUATION_PROMPT = """You are a result evaluator for an AI assistant. Evaluate whether a subtask's result meets its objectives.

Given a subtask description and its result, determine:
1. Did the subtask achieve its objective?
2. Is the result complete and accurate?
3. Are there any issues or missing information?
4. Should the task be retried or is it acceptable?

Return a JSON object:
{
  "result": "success|partial|failed|incomplete|needs_review",
  "score": 0.85,
  "criteria_met": ["criteria1", "criteria2"],
  "criteria_failed": ["criteria3"],
  "missing_outputs": ["output1"],
  "issues": ["issue1", "issue2"],
  "recommendations": ["recommendation1"],
  "confidence": 0.9
}

Result values:
- success: Fully achieved objective
- partial: Partially achieved, acceptable
- failed: Did not achieve objective
- incomplete: Missing critical outputs
- needs_review: Requires human judgment"""


class ResultEvaluator:
    """Evaluates task execution results."""
    
    def __init__(self):
        self._model_provider = None
        self._initialized = False
        self._evaluation_cache: Dict[str, SubtaskEvaluation] = {}
    
    async def initialize(self) -> None:
        """Initialize the evaluator."""
        if self._initialized:
            return
        self._model_provider = provider_factory.get_primary()
        if not self._model_provider:
            providers = provider_factory.get_all_providers()
            if providers:
                self._model_provider = providers[0]
        self._initialized = True
        logger.info("ResultEvaluator initialized")
    
    async def evaluate_subtask(
        self,
        subtask: Subtask,
        record: SubtaskExecutionRecord,
        execution_plan: ExecutionPlan,
    ) -> SubtaskEvaluation:
        """Evaluate a single subtask result."""
        
        # Fast path for obvious cases
        if record.status == SubtaskExecutionStatus.COMPLETED and record.result:
            # Check if result has content
            result_content = self._extract_result_content(record.result)
            if result_content and len(str(result_content).strip()) > 10:
                return SubtaskEvaluation(
                    subtask_id=subtask.id,
                    result=EvaluationResult.SUCCESS,
                    score=0.9,
                    criteria_met=["produced_output"],
                    confidence=0.85,
                )
        
        if record.status == SubtaskExecutionStatus.FAILED:
            return SubtaskEvaluation(
                subtask_id=subtask.id,
                result=EvaluationResult.FAILED,
                score=0.0,
                criteria_failed=["execution_failed"],
                issues=[record.error or "Unknown error"],
                confidence=0.95,
            )
        
        if record.status == SubtaskExecutionStatus.SKIPPED:
            return SubtaskEvaluation(
                subtask_id=subtask.id,
                result=EvaluationResult.PARTIAL,
                score=0.5,
                issues=["subtask_skipped"],
                confidence=0.8,
            )
        
        # Use LLM for nuanced evaluation
        if self._model_provider:
            return await self._llm_evaluate(subtask, record, execution_plan)
        
        # Fallback heuristic
        return self._heuristic_evaluate(subtask, record)
    
    async def _llm_evaluate(
        self,
        subtask: Subtask,
        record: SubtaskExecutionRecord,
        execution_plan: ExecutionPlan,
    ) -> SubtaskEvaluation:
        """Use LLM to evaluate subtask result."""
        try:
            result_content = self._extract_result_content(record.result)
            error_content = record.error or ""
            
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=EVALUATION_PROMPT),
                ChatMessage(role=MessageRole.USER, content=f"""
Subtask: {subtask.name}
Description: {subtask.description}
Task Type: {subtask.task_type.value}
Expected: {subtask.metadata.get('expected_output', 'N/A')}

Result Status: {record.status.value}
Result Content: {result_content[:2000] if result_content else 'None'}
Error: {error_content[:500] if error_content else 'None'}

Overall Task: {execution_plan.original_request}
"""),
            ]
            
            req = CompletionRequest(
                messages=messages,
                temperature=0.1,
                max_tokens=1000,
                stream=False,
            )
            
            response = await provider_factory.complete_with_fallback(req)
            
            if response.content:
                return self._parse_evaluation_response(subtask.id, response.content)
            
        except Exception as e:
            logger.warning(f"LLM evaluation failed: {e}")
        
        return self._heuristic_evaluate(subtask, record)
    
    def _parse_evaluation_response(self, subtask_id: str, content: str) -> SubtaskEvaluation:
        """Parse LLM evaluation response."""
        import json
        import re
        
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            return SubtaskEvaluation(
                subtask_id=subtask_id,
                result=EvaluationResult.NEEDS_REVIEW,
                score=0.5,
                confidence=0.3,
            )
        
        try:
            data = json.loads(json_match.group())
            
            result_map = {
                "success": EvaluationResult.SUCCESS,
                "partial": EvaluationResult.PARTIAL,
                "failed": EvaluationResult.FAILED,
                "incomplete": EvaluationResult.INCOMPLETE,
                "needs_review": EvaluationResult.NEEDS_REVIEW,
            }
            
            return SubtaskEvaluation(
                subtask_id=subtask_id,
                result=result_map.get(data.get("result", "needs_review"), EvaluationResult.NEEDS_REVIEW),
                score=float(data.get("score", 0.5)),
                criteria_met=data.get("criteria_met", []),
                criteria_failed=data.get("criteria_failed", []),
                missing_outputs=data.get("missing_outputs", []),
                issues=data.get("issues", []),
                recommendations=data.get("recommendations", []),
                confidence=float(data.get("confidence", 0.5)),
            )
        except Exception as e:
            logger.warning(f"Failed to parse evaluation: {e}")
            return SubtaskEvaluation(
                subtask_id=subtask_id,
                result=EvaluationResult.NEEDS_REVIEW,
                score=0.5,
                confidence=0.3,
            )
    
    def _heuristic_evaluate(
        self,
        subtask: Subtask,
        record: SubtaskExecutionRecord,
    ) -> SubtaskEvaluation:
        """Heuristic evaluation when LLM unavailable."""
        if record.status == SubtaskExecutionStatus.COMPLETED:
            result_content = self._extract_result_content(record.result)
            if result_content and len(str(result_content).strip()) > 20:
                return SubtaskEvaluation(
                    subtask_id=subtask.id,
                    result=EvaluationResult.SUCCESS,
                    score=0.8,
                    criteria_met=["produced_output"],
                    confidence=0.7,
                )
            else:
                return SubtaskEvaluation(
                    subtask_id=subtask.id,
                    result=EvaluationResult.INCOMPLETE,
                    score=0.3,
                    missing_outputs=["substantive_content"],
                    confidence=0.6,
                )
        
        elif record.status == SubtaskExecutionStatus.FAILED:
            return SubtaskEvaluation(
                subtask_id=subtask.id,
                result=EvaluationResult.FAILED,
                score=0.0,
                criteria_failed=["execution_failed"],
                issues=[record.error or "Unknown error"],
                confidence=0.9,
            )
        
        else:
            return SubtaskEvaluation(
                subtask_id=subtask.id,
                result=EvaluationResult.NEEDS_REVIEW,
                score=0.5,
                confidence=0.4,
            )
    
    def _extract_result_content(self, result: Any) -> Optional[str]:
        """Extract text content from result."""
        if result is None:
            return None
        
        if isinstance(result, str):
            return result
        
        if isinstance(result, dict):
            # Try common keys
            for key in ["content", "result", "text", "output", "message"]:
                if key in result and result[key]:
                    return str(result[key])
            # Return stringified dict
            return str(result)
        
        if isinstance(result, list):
            return "\n".join(str(item) for item in result)
        
        return str(result)
    
    async def evaluate_task(
        self,
        context: Any,  # OrchestrationContext
    ) -> TaskEvaluation:
        """Evaluate overall task execution."""
        execution_plan = context.execution_plan
        subtask_records = context.subtask_records
        execution_record = context.execution_record
        
        start_time = execution_record.started_at or 0
        end_time = execution_record.completed_at or int(datetime.now().timestamp() * 1000)
        execution_time_ms = end_time - start_time
        
        # Evaluate each subtask
        subtask_evaluations = []
        completed_count = 0
        failed_count = 0
        skipped_count = 0
        
        for subtask in execution_plan.subtasks:
            record = subtask_records.get(subtask.id)
            if not record:
                continue
            
            if record.status == SubtaskExecutionStatus.COMPLETED:
                completed_count += 1
            elif record.status == SubtaskExecutionStatus.FAILED:
                failed_count += 1
            elif record.status == SubtaskExecutionStatus.SKIPPED:
                skipped_count += 1
            
            evaluation = await self.evaluate_subtask(subtask, record, execution_plan)
            subtask_evaluations.append(evaluation)
        
        total_subtasks = len(execution_plan.subtasks)
        
        # Determine overall result
        if failed_count == 0 and completed_count == total_subtasks:
            overall_result = EvaluationResult.SUCCESS
        elif failed_count == 0 and completed_count > 0:
            overall_result = EvaluationResult.PARTIAL
        elif completed_count == 0 and failed_count > 0:
            overall_result = EvaluationResult.FAILED
        elif completed_count < total_subtasks * 0.5:
            overall_result = EvaluationResult.FAILED
        else:
            overall_result = EvaluationResult.PARTIAL
        
        # Check for incomplete results
        if overall_result != EvaluationResult.FAILED:
            incomplete = any(
                e.result == EvaluationResult.INCOMPLETE
                for e in subtask_evaluations
            )
            if incomplete:
                overall_result = EvaluationResult.INCOMPLETE
        
        # Consolidate results
        consolidated_result = self._consolidate_results(
            execution_plan, subtask_records, subtask_evaluations
        )
        
        # Collect issues and recommendations
        all_issues = []
        all_recommendations = []
        for eval in subtask_evaluations:
            all_issues.extend(eval.issues)
            all_recommendations.extend(eval.recommendations)
        
        # Generate summary
        summary = self._generate_summary(
            overall_result, completed_count, failed_count, skipped_count, total_subtasks
        )
        
        return TaskEvaluation(
            task_id=context.task_id,
            overall_result=overall_result,
            subtask_evaluations=subtask_evaluations,
            completed_subtasks=completed_count,
            failed_subtasks=failed_count,
            skipped_subtasks=skipped_count,
            total_subtasks=total_subtasks,
            execution_time_ms=execution_time_ms,
            summary=summary,
            issues=list(set(all_issues)),  # Deduplicate
            recommendations=list(set(all_recommendations)),
            consolidated_result=consolidated_result,
        )
    
    def _consolidate_results(
        self,
        execution_plan: ExecutionPlan,
        subtask_records: Dict[str, SubtaskExecutionRecord],
        evaluations: List[SubtaskEvaluation],
    ) -> Any:
        """Consolidate successful subtask results into final output."""
        results = {}
        
        for subtask in execution_plan.subtasks:
            record = subtask_records.get(subtask.id)
            if not record or record.status != SubtaskExecutionStatus.COMPLETED:
                continue
            
            eval_result = next((e for e in evaluations if e.subtask_id == subtask.id), None)
            if eval_result and eval_result.result in (EvaluationResult.SUCCESS, EvaluationResult.PARTIAL):
                content = self._extract_result_content(record.result)
                if content:
                    results[subtask.name] = content
        
        # If only one result, return it directly
        if len(results) == 1:
            return list(results.values())[0]
        
        return results if results else None
    
    def _generate_summary(
        self,
        overall_result: EvaluationResult,
        completed: int,
        failed: int,
        skipped: int,
        total: int,
    ) -> str:
        """Generate human-readable summary."""
        if overall_result == EvaluationResult.SUCCESS:
            return f"Task completed successfully. All {total} subtasks completed."
        elif overall_result == EvaluationResult.PARTIAL:
            return f"Task partially completed. {completed}/{total} subtasks succeeded, {failed} failed, {skipped} skipped."
        elif overall_result == EvaluationResult.INCOMPLETE:
            return f"Task incomplete. {completed}/{total} subtasks completed, but some results are incomplete."
        elif overall_result == EvaluationResult.FAILED:
            return f"Task failed. {failed}/{total} subtasks failed, {completed} succeeded."
        else:
            return f"Task needs review. {completed}/{total} subtasks completed."


# Global evaluator instance
evaluator = ResultEvaluator()