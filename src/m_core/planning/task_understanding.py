"""Task Understanding Engine for M AI Assistant."""
import asyncio
import uuid
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
    TaskType, TaskComplexity, TaskIntent, TaskEntity,
    TaskRequirement, TaskAnalysis, TaskStatus,
)

logger = get_logger(__name__)


# System prompt for task analysis
TASK_ANALYSIS_PROMPT = """You are a task analysis engine for an AI assistant. Analyze the user's request and produce a structured analysis.

Return a JSON object with these fields:
{
  "intent": {
    "primary": "main intent category",
    "secondary": ["additional intents"],
    "confidence": 0.95,
    "reasoning": "explanation"
  },
  "entities": [
    {"name": "entity_name", "type": "entity_type", "value": "extracted_value", "confidence": 0.9}
  ],
  "requirements": [
    {"capability": "required_capability", "description": "what's needed", "required": true, "alternatives": []}
  ],
  "task_type": "simple|tool_use|multi_step|complex",
  "complexity": "trivial|low|medium|high|critical",
  "needs_memory": false,
  "needs_tools": false,
  "estimated_subtasks": 1,
  "estimated_duration_seconds": 5.0,
  "risk_level": "low|medium|high|critical",
  "requires_approval": false
}

Intent categories: question_answering, information_retrieval, file_operation, system_control, web_search, code_generation, data_analysis, planning, automation, creative_writing, conversation, other

Entity types: file_path, url, date_time, person, organization, location, code_snippet, search_query, command, setting, other

Capabilities: model:chat, model:tools, file:read, file:write, file:list, file:search, file:delete, web:search, web:browse, web:automate, web:download, system:info, system:admin, system:power, system:volume, system:clipboard, app:launch, app:control, app:list, app:close, memory:read, memory:write, memory:delete, memory:search, voice:stt, voice:tts, voice:wake-word, voice:mic, phone:outbound, phone:inbound, phone:contacts, phone:sms, phone:record, screen:capture, screen:ocr, screen:analyze, screen:record, automation:input, automation:window, automation:macro, plugin:install, plugin:update, plugin:remove

Risk levels: low, medium, high, critical

Analyze carefully. Simple questions need minimal analysis. Complex multi-step tasks need detailed breakdown."""


class TaskUnderstandingEngine:
    """Engine for understanding and analyzing user tasks."""
    
    def __init__(self):
        self._model_provider = None
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the engine."""
        if self._initialized:
            return
        self._model_provider = provider_factory.get_primary()
        if not self._model_provider:
            # Try fallback
            providers = provider_factory.get_all_providers()
            if providers:
                self._model_provider = providers[0]
        self._initialized = True
        logger.info("TaskUnderstandingEngine initialized")
    
    async def analyze(self, request: str, context: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        """Analyze a user request and produce structured task analysis."""
        await self.initialize()
        
        # Check for simple patterns first (fast path)
        simple_analysis = self._quick_analysis(request)
        if simple_analysis:
            await event_bus.publish_sync(
                "task.analyzed",
                "task_understanding",
                {"task_type": simple_analysis.task_type.value, "complexity": simple_analysis.complexity.value}
            )
            return simple_analysis
        
        # Use LLM for complex analysis
        analysis = await self._llm_analysis(request, context)
        
        await event_bus.publish_sync(
            "task.analyzed",
            "task_understanding",
            {"task_type": analysis.task_type.value, "complexity": analysis.complexity.value}
        )
        
        return analysis
    
    def _quick_analysis(self, request: str) -> Optional[TaskAnalysis]:
        """Fast-path analysis for simple requests."""
        request_lower = request.lower().strip()
        
        # Greetings and simple conversational
        greetings = ["hello", "hi ", "hey ", "hi.", "hey.", "hello.", "good morning", "good afternoon", "good evening"]
        if any(request_lower.startswith(g) for g in greetings) and len(request) < 50:
            return TaskAnalysis(
                intent=TaskIntent(primary="conversation", confidence=0.99),
                task_type=TaskType.SIMPLE,
                complexity=TaskComplexity.TRIVIAL,
                estimated_subtasks=1,
                estimated_duration_seconds=1.0,
            )
        
        # Simple factual questions
        question_starters = ["what is ", "who is ", "when is ", "where is ", "how many ", "define ", "meaning of "]
        if any(request_lower.startswith(q) for q in question_starters) and "?" in request and len(request) < 200:
            return TaskAnalysis(
                intent=TaskIntent(primary="question_answering", confidence=0.9),
                task_type=TaskType.SIMPLE,
                complexity=TaskComplexity.LOW,
                needs_memory=True,
                estimated_subtasks=1,
                estimated_duration_seconds=3.0,
            )
        
        # Simple web search
        search_patterns = ["search for ", "look up ", "find ", "google "]
        if any(request_lower.startswith(p) for p in search_patterns) and len(request) < 200:
            return TaskAnalysis(
                intent=TaskIntent(primary="information_retrieval", confidence=0.85),
                task_type=TaskType.TOOL_USE,
                complexity=TaskComplexity.LOW,
                needs_tools=True,
                requirements=[
                    TaskRequirement(capability="web:search", description="Search the web", required=True)
                ],
                estimated_subtasks=1,
                estimated_duration_seconds=5.0,
            )
        
        # File operations
        file_patterns = ["read file ", "open file ", "show file ", "cat ", "list files ", "ls ", "find file "]
        if any(request_lower.startswith(p) for p in file_patterns) and len(request) < 200:
            return TaskAnalysis(
                intent=TaskIntent(primary="file_operation", confidence=0.85),
                task_type=TaskType.TOOL_USE,
                complexity=TaskComplexity.LOW,
                needs_tools=True,
                requirements=[
                    TaskRequirement(capability="file:read", description="Read file", required=True)
                ],
                estimated_subtasks=1,
                estimated_duration_seconds=3.0,
            )
        
        # System info
        sys_patterns = ["system info", "system status", "cpu usage", "memory usage", "disk usage", "whoami", "hostname"]
        if any(p in request_lower for p in sys_patterns) and len(request) < 100:
            return TaskAnalysis(
                intent=TaskIntent(primary="system_control", confidence=0.9),
                task_type=TaskType.TOOL_USE,
                complexity=TaskComplexity.LOW,
                needs_tools=True,
                requirements=[
                    TaskRequirement(capability="system:info", description="Get system info", required=True)
                ],
                estimated_subtasks=1,
                estimated_duration_seconds=2.0,
            )
        
        # App launch/close
        app_launch_patterns = ["launch ", "open ", "start ", "run "]
        if any(request_lower.startswith(p) for p in app_launch_patterns) and len(request) < 200:
            return TaskAnalysis(
                intent=TaskIntent(primary="system_control", confidence=0.85),
                task_type=TaskType.TOOL_USE,
                complexity=TaskComplexity.LOW,
                needs_tools=True,
                requirements=[
                    TaskRequirement(capability="app:launch", description="Launch application", required=True)
                ],
                estimated_subtasks=1,
                estimated_duration_seconds=3.0,
            )
        
        app_close_patterns = ["close ", "exit ", "quit "]
        if any(request_lower.startswith(p) for p in app_close_patterns) and len(request) < 200:
            return TaskAnalysis(
                intent=TaskIntent(primary="system_control", confidence=0.85),
                task_type=TaskType.TOOL_USE,
                complexity=TaskComplexity.LOW,
                needs_tools=True,
                requirements=[
                    TaskRequirement(capability="app:control", description="Control application", required=True)
                ],
                estimated_subtasks=1,
                estimated_duration_seconds=2.0,
            )
        
        return None
    
    async def _llm_analysis(self, request: str, context: Optional[Dict[str, Any]] = None) -> TaskAnalysis:
        """Use LLM for detailed task analysis."""
        if not self._model_provider:
            # Fallback to basic analysis
            return self._fallback_analysis(request)
        
        try:
            # Build context from memory if available
            memory_context = ""
            if context and context.get("conversation_id"):
                try:
                    history = await memory_manager.get_conversation_history(
                        context["conversation_id"], limit=5
                    )
                    if history:
                        memory_context = "\nRecent conversation:\n" + "\n".join(
                            f"{h.metadata.get('role', 'user')}: {h.content[:200]}"
                            for h in history[-5:]
                        )
                except Exception:
                    pass
            
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=TASK_ANALYSIS_PROMPT),
                ChatMessage(role=MessageRole.USER, content=f"Request: {request}\n{memory_context}"),
            ]
            
            req = CompletionRequest(
                messages=messages,
                temperature=0.1,  # Low temperature for consistent analysis
                max_tokens=1000,
                stream=False,
            )
            
            response = await provider_factory.complete_with_fallback(req)
            
            if response.content:
                return self._parse_analysis_response(response.content, request)
            
        except Exception as e:
            logger.warning(f"LLM analysis failed, using fallback: {e}")
        
        return self._fallback_analysis(request)
    
    def _parse_analysis_response(self, content: str, original_request: str) -> TaskAnalysis:
        """Parse LLM analysis response."""
        import json
        import re
        
        # Try to extract JSON from response
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            return self._fallback_analysis(original_request)
        
        json_str = json_match.group()
        
        # Fix common JSON escape issues in LLM responses
        # Replace invalid escape sequences like \C, \U, etc.
        json_str = re.sub(r'\\([^"\\/bfnrtu])', r'\\\\\1', json_str)
        
        try:
            data = json.loads(json_str)
            
            # Parse intent
            intent_data = data.get("intent", {})
            intent = TaskIntent(
                primary=intent_data.get("primary", "other"),
                secondary=intent_data.get("secondary", []),
                confidence=float(intent_data.get("confidence", 0.8)),
                reasoning=intent_data.get("reasoning", ""),
            )
            
            # Parse entities
            entities = []
            for e in data.get("entities", []):
                entities.append(TaskEntity(
                    name=e.get("name", ""),
                    type=e.get("type", "other"),
                    value=e.get("value", ""),
                    confidence=float(e.get("confidence", 0.8)),
                ))
            
            # Parse requirements
            requirements = []
            for r in data.get("requirements", []):
                requirements.append(TaskRequirement(
                    capability=r.get("capability", ""),
                    description=r.get("description", ""),
                    required=r.get("required", True),
                    alternatives=r.get("alternatives", []),
                ))
            
            # Map task type
            task_type_map = {
                "simple": TaskType.SIMPLE,
                "tool_use": TaskType.TOOL_USE,
                "multi_step": TaskType.MULTI_STEP,
                "complex": TaskType.COMPLEX,
            }
            task_type = task_type_map.get(data.get("task_type", "simple"), TaskType.SIMPLE)
            
            # Map complexity
            complexity_map = {
                "trivial": TaskComplexity.TRIVIAL,
                "low": TaskComplexity.LOW,
                "medium": TaskComplexity.MEDIUM,
                "high": TaskComplexity.HIGH,
                "critical": TaskComplexity.CRITICAL,
            }
            complexity = complexity_map.get(data.get("complexity", "trivial"), TaskComplexity.TRIVIAL)
            
            return TaskAnalysis(
                intent=intent,
                entities=entities,
                requirements=requirements,
                task_type=task_type,
                complexity=complexity,
                needs_memory=data.get("needs_memory", False),
                needs_tools=data.get("needs_tools", False),
                estimated_subtasks=int(data.get("estimated_subtasks", 1)),
                estimated_duration_seconds=float(data.get("estimated_duration_seconds", 5.0)),
                risk_level=data.get("risk_level", "low"),
                requires_approval=data.get("requires_approval", False),
            )
            
        except Exception as e:
            logger.warning(f"Failed to parse analysis response: {e}")
            return self._fallback_analysis(original_request)
    
    def _fallback_analysis(self, request: str) -> TaskAnalysis:
        """Fallback analysis when LLM fails."""
        # Heuristic analysis based on request length and keywords
        request_lower = request.lower()
        word_count = len(request.split())
        
        # Determine if tools needed
        tool_keywords = ["search", "find", "read", "write", "list", "delete", "create", "run", "execute", "launch", "open", "close", "get", "set", "install", "update", "remove"]
        needs_tools = any(kw in request_lower for kw in tool_keywords)
        
        # Determine if memory needed
        memory_keywords = ["remember", "recall", "previous", "earlier", "last time", "before", "history", "context"]
        needs_memory = any(kw in request_lower for kw in memory_keywords)
        
        # Determine complexity
        if word_count < 20 and not needs_tools:
            complexity = TaskComplexity.TRIVIAL
            task_type = TaskType.SIMPLE
            estimated_subtasks = 1
        elif word_count < 50 and needs_tools:
            complexity = TaskComplexity.LOW
            task_type = TaskType.TOOL_USE
            estimated_subtasks = 2
        elif word_count < 150:
            complexity = TaskComplexity.MEDIUM
            task_type = TaskType.MULTI_STEP
            estimated_subtasks = 3
        else:
            complexity = TaskComplexity.HIGH
            task_type = TaskType.COMPLEX
            estimated_subtasks = 5
        
        return TaskAnalysis(
            intent=TaskIntent(primary="other", confidence=0.5),
            task_type=task_type,
            complexity=complexity,
            needs_memory=needs_memory,
            needs_tools=needs_tools,
            estimated_subtasks=estimated_subtasks,
            estimated_duration_seconds=estimated_subtasks * 10.0,
            risk_level="low",
        )


# Global task understanding engine instance
task_understanding = TaskUnderstandingEngine()