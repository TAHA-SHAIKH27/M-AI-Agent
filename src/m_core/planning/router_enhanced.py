"""
Enhanced Intelligent Control Router for M AI Assistant.
Automatically selects between direct automation and visual automation.
"""
import asyncio
import structlog
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

from ..model_providers import provider_factory
from ..agents import agent_registry
from ..agents.schemas import AgentManifest
from ..tools import tool_registry
from ..tools.schemas import ToolManifest, ToolRiskLevel
from ..security import consent_manager
from ..core.events import event_bus
from ..utils.logging import get_logger
from ..planning.schemas import (
    RoutingTargetType, RoutingDecision, RoutingPlan,
    ExecutionPlan, Subtask, TaskAnalysis,
    ApprovalStatus,
)
from .schemas import TaskExecutionStatus

logger = get_logger(__name__)


class ControlMethod(str, Enum):
    """Type of computer control method."""
    DIRECT = "direct"          # Direct API/automation (preferred)
    VISUAL = "visual"          # Visual/UI automation (fallback)
    HYBRID = "hybrid"          # Combination of both


class ControlMethodSelector:
    """Intelligently selects the best control method for a task."""
    
    # Direct automation capabilities (preferred when available)
    DIRECT_CAPABILITIES = {
        "app:launch": ["app_launch"],
        "app:control": ["app_control"],
        "system:admin": ["powershell", "system_command"],
        "system:info": ["process_list", "window_list", "system_info"],
        "file:read": ["file_read", "file_list", "file_search"],
        "file:write": ["file_write", "file_delete", "shortcut_create"],
        "web:search": ["web_search"],
        "memory:read": ["memory_read", "memory_search"],
    }
    
    # Visual automation capabilities (when direct not available)
    VISUAL_CAPABILITIES = {
        "screen:capture": ["screen_capture"],
        "screen:ocr": ["screen_ocr"],
        "screen:find": ["visual_find"],
        "automation:input": ["mouse_control", "keyboard_input"],
        "automation:window": ["window_interact"],
        "screen:verify": ["visual_verify"],
    }
    
    # Tasks that strongly prefer visual automation
    VISUAL_PREFERRED_PATTERNS = [
        "click", "drag", "scroll", "type in", "fill", "select from dropdown",
        "verify visually", "check if visible", "find button", "find text on screen",
        "dialog", "popup", "menu", "tooltip", "hover", "right-click",
        "drag and drop", "screenshot", "ocr", "read screen", "read window",
    ]
    
    # Tasks that strongly prefer direct automation
    DIRECT_PREFERRED_PATTERNS = [
        "launch", "start", "open app", "run command", "execute",
        "list processes", "list windows", "get info", "search file",
        "read file", "write file", "delete file", "create shortcut",
        "run powershell", "run cmd", "system info",
    ]

    def __init__(self):
        self._tool_cache: Dict[str, ToolManifest] = {}
        self._initialized = False
    
    async def initialize(self):
        if self._initialized:
            return
        tools = tool_registry.list_tools()
        for tool in tools:
            self._tool_cache[tool["name"]] = tool
        self._initialized = True
    
    def _check_tool_available(self, tool_name: str) -> bool:
        """Check registry availability and required consent for a tool."""
        tool = self._tool_cache.get(tool_name)
        if not tool or not tool.get("enabled", True):
            return False
        for permission in tool.get("required_permissions", []):
            if not consent_manager.is_granted(permission):
                return False
        return True
    
    def _analyze_task_intent(self, subtask: "Subtask") -> Dict[str, Any]:
        """Analyze subtask to determine preferred control method."""
        description = subtask.description.lower()
        name = subtask.name.lower()
        text = f"{name} {description}"
        
        # Count visual vs direct indicators
        visual_score = sum(1 for pattern in self.VISUAL_PREFERRED_PATTERNS if pattern in text)
        direct_score = sum(1 for pattern in self.DIRECT_PREFERRED_PATTERNS if pattern in text)
        
        # Check for explicit visual keywords
        visual_keywords = ["visual", "screen", "ui", "interface", "button", "dialog", 
                          "click", "drag", "scroll", "screenshot", "ocr", "verify"]
        direct_keywords = ["launch", "run", "execute", "command", "powershell", 
                          "process", "file", "system", "list", "info"]
        
        for kw in visual_keywords:
            if kw in text:
                visual_score += 2
        for kw in direct_keywords:
            if kw in text:
                direct_score += 2
        
        return {
            "visual_score": visual_score,
            "direct_score": direct_score,
            "preferred_method": ControlMethod.VISUAL if visual_score > direct_score else ControlMethod.DIRECT,
            "confidence": abs(visual_score - direct_score) / max(visual_score + direct_score, 1)
        }
    
    def _get_available_direct_tools(self, capability: str) -> List[str]:
        """Get available direct automation tools for a capability."""
        tools = self.DIRECT_CAPABILITIES.get(capability, [])
        return [t for t in tools if self._check_tool_available(t)]
    
    def _get_available_visual_tools(self, capability: str) -> List[str]:
        """Get available visual automation tools for a capability."""
        tools = self.VISUAL_CAPABILITIES.get(capability, [])
        return [t for t in tools if self._check_tool_available(t)]
    
    async def select_control_method(
        self,
        subtask: "Subtask",
        execution_plan: "ExecutionPlan",
    ) -> Dict[str, Any]:
        """
        Intelligently select the best control method for a subtask.
        
        Returns:
            Dict with method, primary_tool, fallback_tools, reasoning
        """
        await self.initialize()
        
        # Analyze task intent
        analysis = self._analyze_task_intent(subtask)
        preferred = analysis["preferred_method"]
        
        # Get required capability from subtask
        capability = self._infer_capability(subtask)
        
        # Get available tools for each method
        direct_tools = self._get_available_direct_tools(capability)
        visual_tools = self._get_available_visual_tools(capability)
        
        # Decision logic
        if preferred == ControlMethod.DIRECT and direct_tools:
            method = ControlMethod.DIRECT
            primary = direct_tools[0]
            fallbacks = direct_tools[1:] + visual_tools[:2]
            reasoning = f"Direct automation preferred (score: {analysis['direct_score']}) and available: {primary}"
        elif preferred == ControlMethod.VISUAL and visual_tools:
            method = ControlMethod.VISUAL
            primary = visual_tools[0]
            fallbacks = visual_tools[1:] + direct_tools[:2]
            reasoning = f"Visual automation preferred (score: {analysis['visual_score']}) and available: {primary}"
        elif direct_tools:
            method = ControlMethod.DIRECT
            primary = direct_tools[0]
            fallbacks = direct_tools[1:] + visual_tools[:2]
            reasoning = f"Fallback to direct automation (available: {primary})"
        elif visual_tools:
            method = ControlMethod.VISUAL
            primary = visual_tools[0]
            fallbacks = visual_tools[1:] + direct_tools[:2]
            reasoning = f"Fallback to visual automation (available: {primary})"
        else:
            method = ControlMethod.DIRECT
            primary = "general_assistant"
            fallbacks = []
            reasoning = "No automation tools available, falling back to agent"
        
        return {
            "method": method,
            "primary_tool": primary,
            "fallback_tools": fallbacks[:3],
            "reasoning": reasoning,
            "analysis": analysis,
            "capability": capability,
        }
    
    def _infer_capability(self, subtask: "Subtask") -> str:
        """Infer required capability from subtask description."""
        text = f"{subtask.name} {subtask.description}".lower()
        
        capability_keywords = {
            "app:launch": ["launch", "start", "open", "run app"],
            "app:control": ["focus", "minimize", "maximize", "close", "control"],
            "system:admin": ["powershell", "command", "execute", "run script", "admin"],
            "system:info": ["process", "window", "system info", "list"],
            "file:read": ["read file", "open file", "cat", "view"],
            "file:write": ["write file", "create file", "save", "write"],
            "file:delete": ["delete file", "remove file"],
            "web:search": ["search", "find", "look up", "google"],
            "screen:capture": ["screenshot", "capture screen", "screen shot"],
            "screen:ocr": ["ocr", "read text", "extract text", "read screen"],
            "screen:find": ["find on screen", "locate", "find button", "find text"],
            "automation:input": ["click", "type", "press", "drag", "scroll", "hover"],
            "automation:window": ["dialog", "menu", "dropdown", "select", "fill"],
            "screen:verify": ["verify", "check", "assert", "confirm"],
        }
        
        for cap, keywords in capability_keywords.items():
            if any(kw in text for kw in keywords):
                return cap
        
        # Default based on task type
        if hasattr(subtask, 'task_type'):
            if subtask.task_type.value == "tool_use":
                return "system:info"
        
        return "system:info"


class EnhancedIntelligentRouter:
    """Enhanced router with intelligent control method selection."""
    
    def __init__(self):
        self._base_router = None
        self._selector = ControlMethodSelector()
        self._initialized = False
    
    async def initialize(self):
        if self._initialized:
            return
        from .router import router as base_router
        self._base_router = base_router
        await self._base_router.initialize()
        await self._selector.initialize()
        self._initialized = True
    
    async def create_routing_plan(
        self,
        execution_plan: "ExecutionPlan",
    ) -> "RoutingPlan":
        """Create enhanced routing plan with control method selection."""
        await self.initialize()
        
        # Get base routing plan
        base_plan = await self._base_router.create_routing_plan(execution_plan)
        
        # Enhance each decision with control method selection
        enhanced_decisions = []
        for decision in base_plan.decisions:
            subtask = next((s for s in execution_plan.subtasks if s.id == decision.subtask_id), None)
            if subtask:
                selection = await self._selector.select_control_method(subtask, execution_plan)
                # Use the selected control tool as the primary route when it is
                # actually registered and permitted. The base router remains the
                # fallback for tasks that need an agent or lack a usable tool.
                selected_tool = selection["primary_tool"]
                use_selected_tool = (
                    selected_tool != "general_assistant"
                    and self._selector._check_tool_available(selected_tool)
                )
                target_type = RoutingTargetType.TOOL if use_selected_tool else decision.target_type
                target_id = selected_tool if use_selected_tool else decision.target_id

                # Enhance the decision with an executable primary target and
                # preserve the original router targets as fallbacks.
                enhanced_decision = RoutingDecision(
                    subtask_id=decision.subtask_id,
                    target_type=target_type,
                    target_id=target_id,
                    confidence=max(decision.confidence, 0.8) if use_selected_tool else decision.confidence,
                    reasoning=f"{decision.reasoning} | Control: {selection['reasoning']}",
                    fallback_targets=list(decision.fallback_targets or []) + [
                        {"type": "tool", "id": t, "reason": "alternate control method"}
                        for t in selection["fallback_tools"]
                    ],
                    metadata={
                        **decision.metadata,
                        "control_method": selection["method"].value,
                        "control_primary_tool": selected_tool if use_selected_tool else None,
                        "control_analysis": selection["analysis"],
                        "fallback_chain": selection["fallback_tools"],
                    }
                )
                enhanced_decisions.append(enhanced_decision)
            else:
                enhanced_decisions.append(decision)
        
        return RoutingPlan(
            execution_plan_id=base_plan.execution_plan_id,
            decisions=enhanced_decisions,
            model_provider=base_plan.model_provider,
            fallback_provider=base_plan.fallback_provider,
            metadata={
                **base_plan.metadata,
                "enhanced_routing": True,
                "control_methods_used": list(set(
                    d.metadata.get("control_method", "direct") for d in enhanced_decisions
                )),
            }
        )
    
    async def route_with_fallback(
        self,
        subtask: "Subtask",
        execution_plan: "ExecutionPlan",
        failed_targets: List[str] = None,
    ) -> "RoutingDecision":
        """Enhanced routing with control method fallback."""
        await self.initialize()
        
        # Get base decision with fallback
        decision = await self._base_router.route_with_fallback(subtask, execution_plan, failed_targets)
        
        # If all targets failed, try control method switching
        if decision.target_id in (failed_targets or []):
            selection = await self._selector.select_control_method(subtask, execution_plan)
            for fallback_tool in selection["fallback_tools"]:
                if fallback_tool not in (failed_targets or []):
                    if await self._selector._check_tool_available(fallback_tool):
                        return RoutingDecision(
                            subtask_id=subtask.id,
                            target_type=RoutingTargetType.TOOL,
                            target_id=fallback_tool,
                            confidence=0.6,
                            reasoning=f"Control method fallback to {selection['method'].value}: {fallback_tool}",
                        )
        
        return decision


# Global enhanced router instance
enhanced_router = EnhancedIntelligentRouter()

# Export for backward compatibility
from .router import router, IntelligentRouter

__all__ = [
    "IntelligentRouter",
    "router",
    "ControlMethod",
    "ControlMethodSelector",
    "EnhancedIntelligentRouter",
    "enhanced_router",
]