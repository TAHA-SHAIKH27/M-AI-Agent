"""Intelligent Planner for M AI Assistant."""
import asyncio
import re
import uuid
import msgspec
import structlog
from typing import Optional, List, Dict, Any, Set
from datetime import datetime

from ..model_providers import provider_factory
from ..model_providers.schemas import (
    CompletionRequest, ChatMessage, MessageRole,
)
from ..agents import agent_registry
from ..agents.schemas import AgentManifest, AgentCapability
from ..tools import tool_registry
from m_core.common_schemas import ToolManifest
from ..memory import memory_manager
from ..memory.base import MemoryTier
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    TaskType, TaskComplexity, TaskStatus, SubtaskStatus,
    ExecutionStrategy, TaskAnalysis, Subtask, ExecutionPlan,
    TaskContext, ApprovalStatus, TaskRequirement,
)

logger = get_logger(__name__)


# System prompt for planning
PLANNING_PROMPT = """You are a task planner for M, an autonomous Windows AI assistant. Given a task analysis, create a clear execution plan with logical subtasks.

Return a JSON object with this structure:
{
  "subtasks": [
    {
      "id": "unique_id",
      "name": "Subtask name",
      "description": "What this subtask must accomplish",
      "task_type": "tool_use|simple|multi_step|complex",
      "execution_mode": "direct|autonomous_desktop|autonomous_browser",
      "assigned_agent": "agent_id_or_null",
      "assigned_tool": "tool_name_or_null",
      "arguments": {},
      "dependencies": ["subtask_id_1"],
      "priority": 10,
      "estimated_duration_seconds": 15.0,
      "max_retries": 2,
      "timeout_seconds": 180.0,
      "requires_approval": false,
      "expected_output": "Observable end-state or deliverable"
    }
  ],
  "strategy": "sequential|parallel|dependency_aware",
  "max_concurrency": 3,
  "total_estimated_duration": 30.0
}

Rules:
1. NEVER hardcode screen coordinates (x, y) — dynamic GUI/visual identification discovers elements at runtime.
2. For Windows UI workflows (clicking buttons/menus, typing into live apps, switching apps, handling Save dialogs), prefer a cohesive subtask with `"execution_mode": "autonomous_desktop"` describing the concrete goal and `expected_output` (or separate logical phases for distinct apps/phases).
3. For live web page interaction requiring DOM/clicking/forms, use `"execution_mode": "autonomous_browser"`.
4. Use direct `assigned_tool` + `arguments` only for self-contained operations (e.g., `system_info`, `process_list`, `web_search`, `file_read`, `file_write`, `file_delete`, `app_launch`).
5. Mark subtasks `requires_approval: true` when deleting files, modifying system settings, running administrative/PowerShell commands, launching/controlling apps, or saving/overwriting files.
6. Avoid circular dependencies; max 10 subtasks per plan."""


class IntelligentPlanner:
    """Intelligent planner that creates execution plans from task analysis."""
    
    def __init__(self):
        self._model_provider = None
        self._initialized = False
        self._agent_cache: Dict[str, AgentManifest] = {}
        self._tool_cache: Dict[str, ToolManifest] = {}
    
    async def initialize(self) -> None:
        """Initialize the planner."""
        if self._initialized:
            return
        self._model_provider = provider_factory.get_primary()
        if not self._model_provider:
            providers = provider_factory.get_all_providers()
            if providers:
                self._model_provider = providers[0]
        
        # Cache agent manifests
        agents = agent_registry.list_agents(enabled_only=True)
        for agent in agents:
            self._agent_cache[agent["id"]] = agent
        
        # Cache tool manifests
        tools = tool_registry.list_tools()
        for tool in tools:
            self._tool_cache[tool["name"]] = tool
        
        self._initialized = True
        logger.info("IntelligentPlanner initialized")
    
    async def create_plan(
        self,
        analysis: TaskAnalysis,
        context: Optional[TaskContext] = None,
        original_request: str = "",
    ) -> ExecutionPlan:
        """Create an execution plan from task analysis."""
        await self.initialize()
        
        task_id = str(uuid.uuid4())[:8]
        
        # Simple conversational/factual tasks don't need complex planning
        if analysis.task_type == TaskType.SIMPLE and not analysis.metadata.get("autonomous_execution"):
            plan = self._create_simple_plan(task_id, analysis, original_request)
            await event_bus.publish_sync(
                "task.planned",
                "planner",
                {"task_id": task_id, "subtask_count": len(plan.subtasks), "strategy": plan.strategy.value}
            )
            return plan
        
        # Single-step deterministic tool-use tasks (not needing UI autonomy)
        if analysis.task_type == TaskType.TOOL_USE and not analysis.metadata.get("autonomous_execution"):
            plan = await self._create_tool_plan(task_id, analysis, original_request)
            await event_bus.publish_sync(
                "task.planned",
                "planner",
                {"task_id": task_id, "subtask_count": len(plan.subtasks), "strategy": plan.strategy.value}
            )
            return plan
        
        # Multi-step, complex, and autonomous desktop/browser tasks use LLM planning with structured fallback
        plan = await self._llm_plan(task_id, analysis, original_request, context)
        
        await event_bus.publish_sync(
            "task.planned",
            "planner",
            {"task_id": task_id, "subtask_count": len(plan.subtasks), "strategy": plan.strategy.value}
        )
        
        return plan
    
    def _create_simple_plan(
        self,
        task_id: str,
        analysis: TaskAnalysis,
        original_request: str,
    ) -> ExecutionPlan:
        """Create plan for simple tasks (direct answer)."""
        subtask = Subtask(
            id=f"{task_id}-1",
            name="Generate response",
            description="Generate direct response to user query",
            task_type=TaskType.SIMPLE,
            priority=10,
            estimated_duration_seconds=analysis.estimated_duration_seconds,
        )
        
        return ExecutionPlan(
            task_id=task_id,
            original_request=original_request,
            analysis=analysis,
            subtasks=[subtask],
            strategy=ExecutionStrategy.SEQUENTIAL,
            max_concurrency=1,
            total_estimated_duration=analysis.estimated_duration_seconds,
        )
    
    async def _create_tool_plan(
        self,
        task_id: str,
        analysis: TaskAnalysis,
        original_request: str,
    ) -> ExecutionPlan:
        """Create plan for deterministic tool-use tasks."""
        subtasks = []
        
        # Determine which tools are needed
        required_tools = []
        for req in analysis.requirements:
            if req.required:
                tool_name = self._find_tool_for_capability(req.capability)
                if tool_name:
                    required_tools.append((tool_name, req))
        
        # If no specific tools found, use general search unless destructive
        if not required_tools and analysis.needs_tools and not analysis.metadata.get("is_destructive"):
            required_tools.append(("web_search", TaskRequirement(
                capability="web:search", description="Search the web", required=True
            )))
        
        gui_tools_requiring_observation = {"keyboard_input", "mouse_control", "window_interact", "visual_find", "web_automate"}
        
        for i, (tool_name, req) in enumerate(required_tools):
            requires_approval = self._requires_approval(tool_name, original_request, analysis)
            arguments = self._generate_tool_arguments(tool_name, req, original_request)
            
            # If tool requires dynamic UI state and static args aren't concrete, hand off to autonomous loop
            if tool_name in gui_tools_requiring_observation and not arguments:
                execution_mode = "autonomous_browser" if tool_name == "web_automate" else "autonomous_desktop"
                subtask = Subtask(
                    id=f"{task_id}-{i+1}",
                    name=f"Execute UI step: {req.description}",
                    description=original_request or req.description,
                    task_type=TaskType.MULTI_STEP,
                    priority=10 - i,
                    estimated_duration_seconds=30.0,
                    max_retries=2,
                    timeout_seconds=180.0,
                    requires_approval=requires_approval,
                    approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                    metadata={
                        "execution_mode": execution_mode,
                        "goal": original_request or req.description,
                        "original_request": original_request,
                    },
                )
            else:
                subtask = Subtask(
                    id=f"{task_id}-{i+1}",
                    name=f"Execute {tool_name}",
                    description=req.description or original_request,
                    task_type=TaskType.TOOL_USE,
                    assigned_tool=tool_name,
                    priority=10 - i,
                    estimated_duration_seconds=10.0,
                    max_retries=2,
                    timeout_seconds=60.0,
                    requires_approval=requires_approval,
                    approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                    metadata={"arguments": arguments} if arguments else {},
                )
            subtasks.append(subtask)
        
        if len(subtasks) > 1:
            synthesis = Subtask(
                id=f"{task_id}-synthesis",
                name="Synthesize results",
                description="Combine tool results into final response",
                task_type=TaskType.SIMPLE,
                priority=1,
                estimated_duration_seconds=3.0,
                dependencies=[s.id for s in subtasks],
            )
            subtasks.append(synthesis)
        
        total_duration = sum(s.estimated_duration_seconds for s in subtasks)
        
        return ExecutionPlan(
            task_id=task_id,
            original_request=original_request,
            analysis=analysis,
            subtasks=subtasks,
            strategy=ExecutionStrategy.DEPENDENCY_AWARE if len(subtasks) > 1 else ExecutionStrategy.SEQUENTIAL,
            max_concurrency=min(3, max(1, len(subtasks))),
            total_estimated_duration=total_duration,
        )
    
    async def _llm_plan(
        self,
        task_id: str,
        analysis: TaskAnalysis,
        original_request: str,
        context: Optional[TaskContext],
    ) -> ExecutionPlan:
        """Use LLM to create detailed plan for complex tasks."""
        if not self._model_provider:
            return self._create_fallback_plan(task_id, analysis, original_request)
        
        try:
            # Build agent and tool info for prompt
            agent_info = "\n".join([
                f"- {aid}: {info.get('name', aid)} - capabilities: {info.get('capabilities', [])}"
                for aid, info in self._agent_cache.items()
            ])
            
            tool_info = "\n".join([
                f"- {tid}: {info.get('description', '')} - risk: {info.get('risk_level', 'low')}"
                for tid, info in self._tool_cache.items()
            ])
            
            memory_context = ""
            if context and context.memory_references:
                try:
                    for mem_id in context.memory_references[:3]:
                        entry = await memory_manager.get(mem_id)
                        if entry:
                            memory_context += f"\nMemory: {entry.content[:200]}"
                except Exception:
                    pass
            
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=PLANNING_PROMPT),
                ChatMessage(role=MessageRole.USER, content=f"""
Task Analysis:
- Intent: {analysis.intent.primary}
- Type: {analysis.task_type.value}
- Complexity: {analysis.complexity.value}
- Needs memory: {analysis.needs_memory}
- Needs tools: {analysis.needs_tools}
- Estimated subtasks: {analysis.estimated_subtasks}
- Risk level: {analysis.risk_level}
- Requires approval: {analysis.requires_approval}

Original Request: {original_request}
{memory_context}

Available Agents:
{agent_info}

Available Tools:
{tool_info}
"""),
            ]
            
            req = CompletionRequest(
                messages=messages,
                temperature=0.2,
                max_tokens=2000,
                stream=False,
            )
            
            response = await provider_factory.complete_with_fallback(req)
            
            if response.content:
                return self._parse_plan_response(task_id, analysis, original_request, response.content)
            
        except Exception as e:
            logger.warning(f"LLM planning failed, using fallback: {e}")
        
        return self._create_fallback_plan(task_id, analysis, original_request)
    
    def _parse_plan_response(
        self,
        task_id: str,
        analysis: TaskAnalysis,
        original_request: str,
        content: str,
    ) -> ExecutionPlan:
        """Parse LLM planning response."""
        import json
        
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            return self._create_fallback_plan(task_id, analysis, original_request)
        
        try:
            data = json.loads(json_match.group())
            
            raw_subtasks = data.get("subtasks", [])
            if not isinstance(raw_subtasks, list):
                raise ValueError("Plan 'subtasks' must be a list")

            raw_subtasks = raw_subtasks[:10]
            if not raw_subtasks:
                return self._create_fallback_plan(task_id, analysis, original_request)

            subtasks: List[Subtask] = []
            seen_ids = set()
            gui_tools = {"keyboard_input", "mouse_control", "window_interact", "visual_find", "web_automate"}

            for idx, s in enumerate(raw_subtasks):
                if not isinstance(s, dict):
                    raise ValueError(f"Subtask at index {idx} must be an object")

                subtask_id = str(s.get("id") or f"{task_id}-{idx + 1}").strip()
                if not subtask_id or subtask_id in seen_ids:
                    raise ValueError(f"Missing or duplicate subtask id: {subtask_id!r}")
                seen_ids.add(subtask_id)

                deps = s.get("dependencies", [])
                if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
                    raise ValueError(f"Invalid dependencies for subtask {subtask_id}")
                deps = list(deps)

                task_type_str = s.get("task_type", "tool_use")
                try:
                    task_type = TaskType(task_type_str)
                except (ValueError, TypeError):
                    task_type = TaskType.TOOL_USE

                assigned_tool = s.get("assigned_tool")
                assigned_agent = s.get("assigned_agent")
                if isinstance(assigned_tool, str) and assigned_tool.lower() in ("null", "none", ""):
                    assigned_tool = None
                if isinstance(assigned_agent, str) and assigned_agent.lower() in ("null", "none", ""):
                    assigned_agent = None

                execution_mode = s.get("execution_mode")
                llm_args = s.get("arguments") or s.get("tool_arguments") or {}
                if not isinstance(llm_args, dict):
                    llm_args = {}
                # Never trust LLM-injected approval bypass flags
                llm_args.pop("approved", None)
                llm_args.pop("approval_token", None)

                if not llm_args and assigned_tool:
                    llm_args = self._generate_tool_arguments(assigned_tool, None, s.get("description") or original_request)

                # Detect autonomous UI subtasks: convert coordinate-less GUI tools or explicit autonomous mode
                if not execution_mode:
                    if analysis.metadata.get("autonomous_execution") and (
                        assigned_tool in gui_tools or task_type in (TaskType.MULTI_STEP, TaskType.COMPLEX)
                    ):
                        execution_mode = (
                            "autonomous_browser"
                            if analysis.intent.primary == "browser_automation" or assigned_tool == "web_automate"
                            else "autonomous_desktop"
                        )
                    elif assigned_tool in gui_tools and not llm_args:
                        execution_mode = "autonomous_browser" if assigned_tool == "web_automate" else "autonomous_desktop"

                if execution_mode in ("autonomous_desktop", "autonomous_browser"):
                    # Keep autonomous subtasks routed cleanly to autonomous executor rather than dummy tool calls
                    assigned_tool = None

                subtask_desc = s.get("description", "") or original_request
                requires_approval = bool(s.get("requires_approval", False)) or self._requires_approval(
                    assigned_tool, f"{s.get('name', '')} {subtask_desc} {original_request}", analysis
                )

                metadata: Dict[str, Any] = {}
                if llm_args:
                    metadata["arguments"] = llm_args
                if execution_mode and execution_mode != "direct":
                    metadata["execution_mode"] = execution_mode
                    metadata["goal"] = subtask_desc
                    metadata["original_request"] = original_request
                if s.get("expected_output"):
                    metadata["expected_output"] = s["expected_output"]
                if s.get("target_app"):
                    metadata["target_app"] = s["target_app"]

                subtask = Subtask(
                    id=subtask_id,
                    name=str(s.get("name") or f"Subtask {idx+1}"),
                    description=subtask_desc,
                    task_type=task_type,
                    assigned_agent=assigned_agent,
                    assigned_tool=assigned_tool,
                    dependencies=deps,
                    priority=int(s.get("priority", max(1, 10 - idx))),
                    estimated_duration_seconds=max(0.0, float(s.get("estimated_duration_seconds", 15.0))),
                    max_retries=max(0, int(s.get("max_retries", 2))),
                    timeout_seconds=max(0.1, float(s.get("timeout_seconds", 180.0 if execution_mode in ("autonomous_desktop", "autonomous_browser") else 60.0))),
                    requires_approval=requires_approval,
                    approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                    metadata=metadata,
                )
                subtasks.append(subtask)

            # Validate dependencies
            subtask_ids = {s.id for s in subtasks}
            validated_subtasks = []
            for subtask in subtasks:
                filtered_deps = [d for d in subtask.dependencies if d in subtask_ids and d != subtask.id]
                if filtered_deps != subtask.dependencies:
                    validated_subtasks.append(msgspec.structs.replace(subtask, dependencies=filtered_deps))
                else:
                    validated_subtasks.append(subtask)
            subtasks = validated_subtasks

            if self._has_cycles(subtasks):
                logger.warning("Plan has circular dependencies; removing cyclic dependencies")
                subtasks = self._remove_cycles(subtasks)

            strategy_map = {
                "sequential": ExecutionStrategy.SEQUENTIAL,
                "parallel": ExecutionStrategy.PARALLEL,
                "dependency_aware": ExecutionStrategy.DEPENDENCY_AWARE,
            }
            strategy = strategy_map.get(data.get("strategy", "dependency_aware"), ExecutionStrategy.DEPENDENCY_AWARE)
            
            max_concurrency = int(data.get("max_concurrency", 3))
            total_duration = float(data.get("total_estimated_duration", sum(s.estimated_duration_seconds for s in subtasks)))
            
            return ExecutionPlan(
                task_id=task_id,
                original_request=original_request,
                analysis=analysis,
                subtasks=subtasks,
                strategy=strategy,
                max_concurrency=max_concurrency,
                total_estimated_duration=total_duration,
            )
            
        except Exception as e:
            logger.warning(f"Failed to parse plan response: {e}")
            return self._create_fallback_plan(task_id, analysis, original_request)
    
    def _create_fallback_plan(
        self,
        task_id: str,
        analysis: TaskAnalysis,
        original_request: str,
    ) -> ExecutionPlan:
        """Create structured fallback plan without hardcoded app-specific scripts."""
        subtasks: List[Subtask] = []
        
        # If task requires multi-step UI or browser automation, plan cohesive autonomous execution
        is_autonomous = bool(analysis.metadata.get("autonomous_execution")) or (
            analysis.intent.primary in ("desktop_automation", "browser_automation", "automation")
        )
        if is_autonomous:
            execution_mode = (
                "autonomous_browser"
                if analysis.intent.primary == "browser_automation"
                else "autonomous_desktop"
            )
            requires_approval = analysis.requires_approval or any(
                self._requires_approval(self._find_tool_for_capability(r.capability), original_request, analysis)
                for r in analysis.requirements
            )
            subtasks.append(Subtask(
                id=f"{task_id}-auto-1",
                name="Execute autonomous workflow",
                description=original_request or analysis.intent.primary,
                task_type=TaskType.MULTI_STEP,
                priority=10,
                estimated_duration_seconds=max(30.0, analysis.estimated_duration_seconds),
                max_retries=2,
                timeout_seconds=300.0,
                requires_approval=requires_approval,
                approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                metadata={
                    "execution_mode": execution_mode,
                    "goal": original_request,
                    "original_request": original_request,
                    "requirements": [r.capability for r in analysis.requirements],
                },
            ))
            return ExecutionPlan(
                task_id=task_id,
                original_request=original_request,
                analysis=analysis,
                subtasks=subtasks,
                strategy=ExecutionStrategy.SEQUENTIAL,
                max_concurrency=1,
                total_estimated_duration=subtasks[0].estimated_duration_seconds,
            )
        
        # Direct capability-based fallback for non-GUI multi-step or tool tasks
        prev_id: Optional[str] = None
        for i, req in enumerate(analysis.requirements[:8]):
            tool_name = self._find_tool_for_capability(req.capability)
            requires_approval = self._requires_approval(tool_name, f"{req.description} {original_request}", analysis)
            arguments = self._generate_tool_arguments(tool_name, req, original_request)
            subtask_id = f"{task_id}-{i+1}"
            
            subtask = Subtask(
                id=subtask_id,
                name=f"Execute {req.capability}",
                description=req.description or original_request,
                task_type=TaskType.TOOL_USE if tool_name else TaskType.SIMPLE,
                assigned_tool=tool_name,
                dependencies=[prev_id] if prev_id else [],
                priority=10 - i,
                estimated_duration_seconds=15.0,
                requires_approval=requires_approval,
                approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                metadata={"arguments": arguments} if arguments else {},
            )
            subtasks.append(subtask)
            prev_id = subtask_id
        
        if len(subtasks) > 1:
            synthesis = Subtask(
                id=f"{task_id}-synthesis",
                name="Synthesize results",
                description="Combine results into final response",
                task_type=TaskType.SIMPLE,
                priority=1,
                estimated_duration_seconds=3.0,
                dependencies=[s.id for s in subtasks],
            )
            subtasks.append(synthesis)
        
        if not subtasks:
            requires_approval = self._requires_approval(None, original_request, analysis)
            subtasks.append(Subtask(
                id=f"{task_id}-1",
                name="Process request",
                description=original_request or "Process the user request",
                task_type=TaskType.SIMPLE,
                priority=10,
                estimated_duration_seconds=5.0,
                requires_approval=requires_approval,
                approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
            ))
        
        total_duration = sum(s.estimated_duration_seconds for s in subtasks)
        
        return ExecutionPlan(
            task_id=task_id,
            original_request=original_request,
            analysis=analysis,
            subtasks=subtasks,
            strategy=ExecutionStrategy.DEPENDENCY_AWARE if len(subtasks) > 1 else ExecutionStrategy.SEQUENTIAL,
            max_concurrency=min(3, len(subtasks)),
            total_estimated_duration=total_duration,
        )
    
    def _extract_app_name(self, text: str) -> Optional[str]:
        """Extract concrete application name from request text."""
        text_lower = text.lower()
        app_keywords = {
            "microsoft word": "winword",
            "ms word": "winword",
            "word": "winword",
            "microsoft excel": "excel",
            "excel": "excel",
            "powerpoint": "powerpnt",
            "notepad++": "notepad++",
            "notepad": "notepad",
            "calculator": "calc",
            "calc": "calc",
            "file explorer": "explorer",
            "explorer": "explorer",
            "powershell": "powershell",
            "windows terminal": "wt",
            "terminal": "wt",
            "cmd": "cmd",
            "google chrome": "chrome",
            "chrome": "chrome",
            "microsoft edge": "msedge",
            "edge": "msedge",
            "firefox": "firefox",
            "vs code": "code",
            "vscode": "code",
            "paint": "mspaint",
            "settings": "ms-settings:",
            "task manager": "taskmgr",
            "outlook": "outlook",
        }
        for kw, app in app_keywords.items():
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                return app
        m = re.search(r"\b(?:launch|open|start)\s+([a-zA-Z0-9_.\-]{2,40})", text_lower)
        if m:
            candidate = m.group(1)
            if candidate not in {"a", "an", "the", "file", "folder", "website", "url", "page"}:
                return candidate
        return None
    
    def _extract_path_arg(self, text: str) -> Optional[str]:
        """Extract a Windows/relative path from request text if explicitly mentioned."""
        m = re.search(r'([A-Za-z]:\\[^\s"\'<>|]+|"[^"]+"|\'[^\']+\')', text)
        if m:
            return m.group(1).strip('"\'')
        return None
    
    def _generate_tool_arguments(self, tool_name: Optional[str], req: Optional["TaskRequirement"], original_request: str) -> Dict[str, Any]:
        """Generate concrete non-fabricated arguments for a tool based on the request."""
        if not tool_name:
            return {}
        
        combined = f"{req.description if req else ''} {original_request}".strip()
        combined_lower = combined.lower()
        
        if tool_name == "app_launch":
            app_name = self._extract_app_name(combined)
            return {"app_name": app_name} if app_name else {}
        
        if tool_name == "app_control":
            app_name = self._extract_app_name(combined)
            action = "list"
            if any(word in combined_lower for word in ["close", "exit", "quit", "kill"]):
                action = "close"
            elif any(word in combined_lower for word in ["focus", "activate", "bring to front", "switch to"]):
                action = "focus"
            elif any(word in combined_lower for word in ["minimize", "minimise"]):
                action = "minimize"
            elif any(word in combined_lower for word in ["maximize", "maximise"]):
                action = "maximize"
            elif "restore" in combined_lower:
                action = "restore"
            args: Dict[str, Any] = {"action": action}
            if app_name and action != "list":
                args["window_title"] = app_name
            return args
        
        if tool_name == "process_list":
            return {"limit": 20, "sort_by": "cpu"}
        
        if tool_name == "window_list":
            return {"limit": 20}
        
        if tool_name == "system_info":
            return {"include_cpu": True, "include_memory": True, "include_disk": True}
        
        if tool_name in ("file_read", "file_list", "file_delete"):
            path = self._extract_path_arg(original_request)
            if path:
                return {"path": path}
            return {}
        
        if tool_name == "file_search":
            path = self._extract_path_arg(original_request)
            return {"path": path, "pattern": "*"} if path else {}
        
        if tool_name == "web_search":
            return {"query": original_request, "max_results": 10}
        
        if tool_name == "memory_search":
            return {"query": original_request, "limit": 10}
        
        if tool_name == "screen_capture":
            return {"mode": "full", "output_format": "png"}
        
        if tool_name == "screen_ocr":
            return {"source": "screen", "language": "eng"}
        
        # Dynamic GUI/admin tools must not invent bogus defaults upfront
        return {}
    
    def _find_tool_for_capability(self, capability: str) -> Optional[str]:
        """Find a tool that provides the given capability."""
        capability_to_tool = {
            "web:search": "web_search",
            "web:browse": "web_browse",
            "web:automate": "web_automate",
            "file:read": "file_read",
            "file:write": "file_write",
            "file:list": "file_list",
            "file:search": "file_search",
            "file:delete": "file_delete",
            "system:info": "system_info",
            "system:admin": "powershell",
            "memory:read": "memory_read",
            "memory:search": "memory_search",
            "app:launch": "app_launch",
            "app:control": "app_control",
            "screen:capture": "screen_capture",
            "screen:ocr": "screen_ocr",
            "screen:find": "visual_find",
            "screen:verify": "visual_verify",
            "automation:input": "keyboard_input",
            "automation:mouse": "mouse_control",
            "window:list": "window_list",
            "window:interact": "window_interact",
        }
        return capability_to_tool.get(capability)
    
    def _requires_approval(
        self,
        tool_name: Optional[str],
        text: str = "",
        analysis: Optional[TaskAnalysis] = None,
    ) -> bool:
        """Check if tool or request requires explicit user approval."""
        high_risk_tools = {
            "file_delete", "file_write", "system_admin", "powershell",
            "system_command", "web_automate", "app_control", "app_launch",
            "shortcut_create", "mouse_control", "keyboard_input", "window_interact",
        }
        if tool_name and tool_name in high_risk_tools:
            return True
        if analysis and analysis.requires_approval and tool_name not in ("system_info", "process_list", "window_list", "screen_capture", "screen_ocr"):
            return True
        text_lower = text.lower()
        if any(kw in text_lower for kw in ["delete", "wipe", "format", "powershell", "regedit", "shutdown"]):
            return True
        return False
    
    def _has_cycles(self, subtasks: List[Subtask]) -> bool:
        """Check if subtask dependencies have cycles."""
        graph = {s.id: set(s.dependencies) for s in subtasks}
        
        def visit(node: str, path: Set[str]) -> bool:
            if node in path:
                return True
            path.add(node)
            for dep in graph.get(node, []):
                if visit(dep, path):
                    return True
            path.remove(node)
            return False
        
        for node in graph:
            if visit(node, set()):
                return True
        return False
    
    def _remove_cycles(self, subtasks: List[Subtask]) -> List[Subtask]:
        """Remove cycles by dropping problematic dependencies."""
        # Simple approach: remove all dependencies that cause cycles
        # In production, this would be more sophisticated
        graph = {s.id: set(s.dependencies) for s in subtasks}
        
        def visit(node: str, path: Set[str], visited: Set[str]) -> bool:
            if node in path:
                return True
            if node in visited:
                return False
            path.add(node)
            for dep in graph.get(node, []):
                if visit(dep, path, visited):
                    return True
            path.remove(node)
            visited.add(node)
            return False
        
        # Find edges that cause cycles
        bad_edges = set()
        for node in graph:
            for dep in list(graph[node]):
                # Temporarily remove edge and check
                graph[node].remove(dep)
                if not visit(node, set(), set()):
                    bad_edges.add((node, dep))
                graph[node].add(dep)
        
        # Remove bad edges cleanly on frozen Subtask instances
        cleaned: List[Subtask] = []
        for subtask in subtasks:
            new_deps = [d for d in subtask.dependencies if (subtask.id, d) not in bad_edges]
            if new_deps != subtask.dependencies:
                cleaned.append(msgspec.structs.replace(subtask, dependencies=new_deps))
            else:
                cleaned.append(subtask)
        
        return cleaned
    
    async def replan(
        self,
        original_plan: ExecutionPlan,
        completed_subtasks: List[str],
        failed_subtasks: List[str],
        new_info: Optional[str] = None,
    ) -> ExecutionPlan:
        """Create a revised plan based on execution results."""
        filtered_subtasks = [
            s for s in original_plan.subtasks
            if s.id not in completed_subtasks and s.id not in failed_subtasks
        ]
        
        remaining_ids = {s.id for s in filtered_subtasks}
        remaining_subtasks: List[Subtask] = []
        for s in filtered_subtasks:
            new_deps = [d for d in s.dependencies if d in remaining_ids]
            if new_deps != s.dependencies:
                remaining_subtasks.append(msgspec.structs.replace(s, dependencies=new_deps))
            else:
                remaining_subtasks.append(s)
        
        if failed_subtasks:
            for failed_id in failed_subtasks:
                failed = next((s for s in original_plan.subtasks if s.id == failed_id), None)
                if failed and failed.max_retries > 0:
                    retry_deps = [d for d in failed.dependencies if d in remaining_ids]
                    retry_meta = dict(failed.metadata)
                    if new_info:
                        retry_meta["recovery_hint"] = new_info
                    retry = Subtask(
                        id=f"{failed_id}-retry",
                        name=f"Retry: {failed.name}",
                        description=failed.description,
                        task_type=failed.task_type,
                        assigned_agent=failed.assigned_agent,
                        assigned_tool=failed.assigned_tool,
                        dependencies=retry_deps,
                        priority=failed.priority + 100,
                        estimated_duration_seconds=failed.estimated_duration_seconds * 1.5,
                        max_retries=failed.max_retries - 1,
                        timeout_seconds=failed.timeout_seconds,
                        requires_approval=failed.requires_approval,
                        approval_status=failed.approval_status if failed.approval_status == ApprovalStatus.GRANTED else (
                            ApprovalStatus.PENDING if failed.requires_approval else ApprovalStatus.NOT_REQUIRED
                        ),
                        metadata=retry_meta,
                    )
                    remaining_subtasks.append(retry)
        
        new_task_id = f"{original_plan.task_id}-replan"
        
        return ExecutionPlan(
            task_id=new_task_id,
            original_request=original_plan.original_request,
            analysis=original_plan.analysis,
            subtasks=remaining_subtasks,
            strategy=original_plan.strategy,
            max_concurrency=original_plan.max_concurrency,
            total_estimated_duration=sum(s.estimated_duration_seconds for s in remaining_subtasks),
            metadata={
                **original_plan.metadata,
                "replanned_from": original_plan.task_id,
                "new_info": new_info,
            },
        )


# Global planner instance
planner = IntelligentPlanner()