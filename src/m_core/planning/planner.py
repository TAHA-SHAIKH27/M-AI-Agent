"""Intelligent Planner for M AI Assistant."""
import asyncio
import uuid
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
from ..tools.schemas import ToolManifest
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
PLANNING_PROMPT = """You are a task planner for an AI assistant. Given a task analysis, create a detailed execution plan with subtasks.

Return a JSON object with this structure:
{
  "subtasks": [
    {
      "id": "unique_id",
      "name": "Subtask name",
      "description": "What this subtask does",
      "task_type": "tool_use|simple|multi_step|complex",
      "assigned_agent": "agent_id_or_null",
      "assigned_tool": "tool_name_or_null",
      "dependencies": ["subtask_id_1", "subtask_id_2"],
      "priority": 10,
      "estimated_duration_seconds": 10.0,
      "max_retries": 2,
      "timeout_seconds": 60.0,
      "requires_approval": false
    }
  ],
  "strategy": "sequential|parallel|dependency_aware",
  "max_concurrency": 3,
  "total_estimated_duration": 30.0
}

Rules:
1. Break complex tasks into logical subtasks
2. Identify dependencies between subtasks (what must complete before what)
3. Assign appropriate agents/tools based on capabilities
4. Set priorities (higher = more important/earlier)
5. Estimate realistic durations
6. Mark subtasks requiring approval (system admin, file delete, etc.)
7. Use dependency_aware strategy for complex tasks
8. Max 10 subtasks per plan
9. Avoid circular dependencies
10. Consider available agents and tools

Available agents and their capabilities will be provided.
Available tools and their capabilities will be provided."""


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
        
        # Simple tasks don't need complex planning
        if analysis.task_type == TaskType.SIMPLE:
            plan = self._create_simple_plan(task_id, analysis, original_request)
            await event_bus.publish_sync(
                "task.planned",
                "planner",
                {"task_id": task_id, "subtask_count": len(plan.subtasks), "strategy": plan.strategy.value}
            )
            return plan
        
        # Tool-use tasks
        if analysis.task_type == TaskType.TOOL_USE:
            plan = await self._create_tool_plan(task_id, analysis, original_request)
            await event_bus.publish_sync(
                "task.planned",
                "planner",
                {"task_id": task_id, "subtask_count": len(plan.subtasks), "strategy": plan.strategy.value}
            )
            return plan
        
        # Multi-step and complex tasks use LLM planning
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
        """Create plan for tool-use tasks."""
        subtasks = []
        
        # Determine which tools are needed
        required_tools = []
        for req in analysis.requirements:
            if req.required:
                # Find matching tool
                tool_name = self._find_tool_for_capability(req.capability)
                if tool_name:
                    required_tools.append((tool_name, req))
        
        # If no specific tools found, use general search
        if not required_tools and analysis.needs_tools:
            required_tools.append(("web_search", TaskRequirement(
                capability="web:search", description="Search the web", required=True
            )))
        
        # Create subtasks for each tool
        for i, (tool_name, req) in enumerate(required_tools):
            requires_approval = self._requires_approval(tool_name)
            subtask = Subtask(
                id=f"{task_id}-{i+1}",
                name=f"Execute {tool_name}",
                description=req.description,
                task_type=TaskType.TOOL_USE,
                assigned_tool=tool_name,
                priority=10 - i,
                estimated_duration_seconds=10.0,
                max_retries=2,
                timeout_seconds=60.0,
                requires_approval=requires_approval,
                approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
            )
            subtasks.append(subtask)
        
        # Add final synthesis subtask if multiple tools
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
            max_concurrency=min(3, len(subtasks)),
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
        import re
        
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            return self._create_fallback_plan(task_id, analysis, original_request)
        
        try:
            data = json.loads(json_match.group())
            
            raw_subtasks = data.get("subtasks", [])
            if not isinstance(raw_subtasks, list):
                raise ValueError("Plan 'subtasks' must be a list")

            subtasks = []
            seen_ids = set()
            for index, item in enumerate(raw_subtasks[:10]):
                if not isinstance(item, dict):
                    raise ValueError(f"Subtask at index {index} must be an object")

                subtask_id = str(item.get("id") or f"{task_id}-{index + 1}").strip()
                if not subtask_id or subtask_id in seen_ids:
                    raise ValueError(f"Missing or duplicate subtask id: {subtask_id!r}")
                seen_ids.add(subtask_id)

                deps = item.get("dependencies", [])
                if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
                    raise ValueError(f"Invalid dependencies for subtask {subtask_id}")

                try:
                    task_type = TaskType(item.get("task_type", "tool_use"))
                except (ValueError, TypeError):
                    task_type = TaskType.TOOL_USE

                requires_approval = bool(item.get("requires_approval", False))
                subtasks.append(Subtask(
                    id=subtask_id,
                    name=str(item.get("name") or "Unnamed subtask"),
                    description=str(item.get("description") or ""),
                    task_type=task_type,
                    assigned_agent=item.get("assigned_agent"),
                    assigned_tool=item.get("assigned_tool"),
                    dependencies=deps,
                    priority=int(item.get("priority", 5)),
                    estimated_duration_seconds=max(0.0, float(item.get("estimated_duration_seconds", 10.0))),
                    max_retries=max(0, int(item.get("max_retries", 2))),
                    timeout_seconds=max(0.1, float(item.get("timeout_seconds", 60.0))),
                    requires_approval=requires_approval,
                    approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                ))

            if not subtasks:
                return self._create_fallback_plan(task_id, analysis, original_request)

            # Validate dependencies only after all IDs are known so forward
            # references remain valid. Drop self/unknown references safely.
            valid_ids = {subtask.id for subtask in subtasks}
            validated_subtasks = []
            for subtask in subtasks:
                filtered_deps = [
                    dep for dep in subtask.dependencies
                    if dep in valid_ids and dep != subtask.id
                ]
                if filtered_deps != subtask.dependencies:
                    subtask.dependencies = filtered_deps
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
        """Create fallback plan when LLM fails."""
        subtasks = []
        
        # Create subtasks based on requirements
        for i, req in enumerate(analysis.requirements[:5]):
            tool_name = self._find_tool_for_capability(req.capability)
            requires_approval = self._requires_approval(tool_name) if tool_name else False
            
            # Generate appropriate arguments for the tool
            arguments = self._generate_tool_arguments(tool_name, req, original_request)
            
            subtask = Subtask(
                id=f"{task_id}-{i+1}",
                name=f"Execute {req.capability}",
                description=req.description,
                task_type=TaskType.TOOL_USE,
                assigned_tool=tool_name,
                priority=10 - i,
                estimated_duration_seconds=15.0,
                requires_approval=requires_approval,
                approval_status=ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED,
                metadata={"arguments": arguments},
            )
            subtasks.append(subtask)
        
        # Add synthesis if multiple
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
        
        # If no subtasks created, add default
        if not subtasks:
            subtasks.append(Subtask(
                id=f"{task_id}-1",
                name="Process request",
                description="Process the user request",
                task_type=TaskType.SIMPLE,
                priority=10,
                estimated_duration_seconds=5.0,
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
    
    def _generate_tool_arguments(self, tool_name: Optional[str], req: "TaskRequirement", original_request: str) -> Dict[str, Any]:
        """Generate appropriate arguments for a tool based on the request."""
        if not tool_name:
            return {}
        
        request_lower = original_request.lower()
        
        # Tool-specific argument generation
        if tool_name == "app_launch":
            # Try to extract app name from request
            app_keywords = {
                "notepad": "notepad",
                "calculator": "calc",
                "calc": "calc",
                "explorer": "explorer",
                "cmd": "cmd",
                "powershell": "powershell",
                "terminal": "wt",
                "chrome": "chrome",
                "firefox": "firefox",
                "edge": "msedge",
                "vscode": "code",
                "code": "code",
            }
            app_name = "notepad"  # default
            for kw, app in app_keywords.items():
                if kw in original_request.lower():
                    app_name = app
                    break
            return {"app_name": app_name}
        
        elif tool_name == "app_control":
            return {"action": "list"}
        
        elif tool_name == "powershell":
            return {"command": "Get-Date"}
        
        elif tool_name == "system_command":
            return {"command": "echo", "arguments": "Hello from M"}
        
        elif tool_name == "process_list":
            return {"limit": 20, "sort_by": "cpu"}
        
        elif tool_name == "window_list":
            return {"limit": 20}
        
        elif tool_name == "shortcut_create":
            return {
                "target_path": "C:\\Windows\\notepad.exe",
                "shortcut_path": "C:\\Temp\\notepad.lnk",
            }
        
        elif tool_name == "system_info":
            return {"include_cpu": True, "include_memory": True, "include_disk": True}
        
        elif tool_name == "file_read":
            return {"path": "."}
        
        elif tool_name == "file_list":
            return {"path": ".", "recursive": False}
        
        elif tool_name == "file_search":
            return {"path": ".", "pattern": "*"}
        
        elif tool_name == "web_search":
            return {"query": original_request, "max_results": 10}
        
        elif tool_name == "memory_read":
            return {"entry_id": "latest"}
        
        elif tool_name == "memory_search":
            return {"query": original_request, "limit": 10}
        
        elif tool_name == "screen_capture":
            return {"mode": "full", "output_format": "png"}
        
        elif tool_name == "screen_ocr":
            return {"source": "screen", "language": "eng"}
        
        elif tool_name == "visual_find":
            return {"method": "text", "search_text": "the"}
        
        elif tool_name == "visual_verify":
            return {"check_type": "text_present", "search_text": "Windows"}
        
        elif tool_name == "window_interact":
            return {"action": "list"}
        
        # Default empty arguments
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
            "memory:read": "memory_read",
            "memory:search": "memory_search",
            "app:launch": "app_launch",
            "app:control": "app_control",
        }
        return capability_to_tool.get(capability)
    
    def _requires_approval(self, tool_name: Optional[str]) -> bool:
        """Check if tool requires approval."""
        if not tool_name:
            return False
        high_risk_tools = {"file_delete", "file_write", "system_admin", "web_automate", "app_control", "app_launch"}
        return tool_name in high_risk_tools
    
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
            for dep in graph[node]:
                # Temporarily remove edge and check
                graph[node].remove(dep)
                if not visit(node, set(), set()):
                    bad_edges.add((node, dep))
                graph[node].add(dep)
        
        # Remove bad edges
        for subtask in subtasks:
            subtask.dependencies = [d for d in subtask.dependencies if (subtask.id, d) not in bad_edges]
        
        return subtasks
    
    async def replan(
        self,
        original_plan: ExecutionPlan,
        completed_subtasks: List[str],
        failed_subtasks: List[str],
        new_info: Optional[str] = None,
    ) -> ExecutionPlan:
        """Create a revised plan based on execution results."""
        # Filter out completed subtasks
        remaining_subtasks = [
            s for s in original_plan.subtasks
            if s.id not in completed_subtasks and s.id not in failed_subtasks
        ]
        
        # Reset dependencies to only include remaining subtasks
        remaining_ids = {s.id for s in remaining_subtasks}
        for s in remaining_subtasks:
            s.dependencies = [d for d in s.dependencies if d in remaining_ids]
        
        # If there are failed subtasks, we might need to add recovery steps
        if failed_subtasks:
            for failed_id in failed_subtasks:
                # Find the failed subtask
                failed = next((s for s in original_plan.subtasks if s.id == failed_id), None)
                if failed and failed.max_retries > 0:
                    # Create retry subtask
                    retry = Subtask(
                        id=f"{failed_id}-retry",
                        name=f"Retry: {failed.name}",
                        description=failed.description,
                        task_type=failed.task_type,
                        assigned_agent=failed.assigned_agent,
                        assigned_tool=failed.assigned_tool,
                        dependencies=failed.dependencies,
                        priority=failed.priority + 100,  # High priority for retry
                        estimated_duration_seconds=failed.estimated_duration_seconds * 1.5,
                        max_retries=failed.max_retries - 1,
                        timeout_seconds=failed.timeout_seconds,
                        requires_approval=failed.requires_approval,
                        approval_status=ApprovalStatus.PENDING if failed.requires_approval else ApprovalStatus.NOT_REQUIRED,
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
            metadata={"replanned_from": original_plan.task_id, "new_info": new_info},
        )


# Global planner instance
planner = IntelligentPlanner()