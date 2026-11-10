"""Intelligent Router for M AI Assistant."""
import asyncio
import uuid
import structlog
from typing import Optional, List, Dict, Any
from datetime import datetime

from ..model_providers import provider_factory
from ..model_providers.base import ModelProvider
from ..agents import agent_registry
from ..agents.schemas import AgentManifest, AgentCapability
from ..tools import tool_registry
from m_core.common_schemas import ToolManifest, ToolRiskLevel
from ..security import consent_manager
from ..security.consent import Capability
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    RoutingTargetType, RoutingDecision, RoutingPlan,
    ExecutionPlan, Subtask, TaskAnalysis,
    ApprovalStatus,
)

logger = get_logger(__name__)


class IntelligentRouter:
    """Intelligent router that selects agents, tools, and providers for subtasks."""
    
    def __init__(self):
        self._initialized = False
        self._agent_capabilities: Dict[str, List[str]] = {}
        self._tool_capabilities: Dict[str, List[str]] = {}
    
    async def initialize(self) -> None:
        """Initialize the router."""
        if self._initialized:
            return
        
        # Cache agent capabilities
        agents = agent_registry.list_agents(enabled_only=True)
        for agent in agents:
            self._agent_capabilities[agent["id"]] = agent.get("capabilities", [])
        
        # Cache tool capabilities
        tools = tool_registry.list_tools()
        for tool in tools:
            self._tool_capabilities[tool["name"]] = tool.get("capabilities", [])
        
        self._initialized = True
        logger.info("IntelligentRouter initialized")
    
    async def create_routing_plan(
        self,
        execution_plan: ExecutionPlan,
    ) -> RoutingPlan:
        """Create routing plan for an execution plan."""
        await self.initialize()
        
        decisions = []
        
        for subtask in execution_plan.subtasks:
            decision = await self._route_subtask(subtask, execution_plan)
            decisions.append(decision)
        
        # Determine model provider
        primary_provider = provider_factory.get_primary()
        model_provider = primary_provider.id if primary_provider else None
        
        # Determine fallback provider
        fallback_provider = None
        providers = provider_factory.get_all_providers()
        if len(providers) > 1:
            fallback_provider = providers[1].id
        
        routing_plan = RoutingPlan(
            execution_plan_id=execution_plan.task_id,
            decisions=decisions,
            model_provider=model_provider,
            fallback_provider=fallback_provider,
        )
        
        await event_bus.publish_sync(
            "task.routed",
            "router",
            {"execution_plan_id": execution_plan.task_id, "decision_count": len(decisions)}
        )
        
        return routing_plan
    
    async def _route_subtask(
        self,
        subtask: Subtask,
        execution_plan: ExecutionPlan,
    ) -> RoutingDecision:
        """Route a single subtask to the best target."""
        
        # If subtask already has assigned tool, use it
        if subtask.assigned_tool:
            try:
                tool = tool_registry.get_tool(subtask.assigned_tool)
                if tool and await self._check_tool_availability(subtask.assigned_tool):
                    return RoutingDecision(
                        subtask_id=subtask.id,
                        target_type=RoutingTargetType.TOOL,
                        target_id=subtask.assigned_tool,
                        confidence=0.95,
                        reasoning=f"Pre-assigned tool: {subtask.assigned_tool}",
                    )
            except Exception:
                # Tool not found or unavailable, continue to find alternative
                logger.warning(f"Pre-assigned tool '{subtask.assigned_tool}' not available, searching for alternative")
        
        # If subtask already has assigned agent, use it
        if subtask.assigned_agent:
            agent = agent_registry.get_agent(subtask.assigned_agent)
            if agent and await self._check_agent_availability(subtask.assigned_agent):
                return RoutingDecision(
                    subtask_id=subtask.id,
                    target_type=RoutingTargetType.AGENT,
                    target_id=subtask.assigned_agent,
                    confidence=0.9,
                    reasoning=f"Pre-assigned agent: {subtask.assigned_agent}",
                )
        
        # Find best tool for the subtask
        tool_decision = await self._find_best_tool(subtask)
        if tool_decision:
            return tool_decision
        
        # Find best agent for the subtask
        agent_decision = await self._find_best_agent(subtask, execution_plan)
        if agent_decision:
            return agent_decision
        
        # Fallback to general assistant
        return RoutingDecision(
            subtask_id=subtask.id,
            target_type=RoutingTargetType.AGENT,
            target_id="general_assistant",
            confidence=0.5,
            reasoning="Fallback to general assistant",
            fallback_targets=[
                {"type": "agent", "id": "general_assistant", "reason": "Default fallback"}
            ],
        )
    
    async def _find_best_tool(self, subtask: Subtask) -> Optional[RoutingDecision]:
        """Find the best tool for a subtask."""
        # Get keywords from subtask description
        keywords = subtask.description.lower().split()
        
        # Score tools based on match
        tool_scores = []
        for tool_name, capabilities in self._tool_capabilities.items():
            if not await self._check_tool_availability(tool_name):
                continue
            
            score = 0
            tool = tool_registry.get_tool(tool_name)
            if not tool:
                continue
            
            # Check name match
            if any(kw in tool_name.lower() for kw in keywords):
                score += 10
            
            # Check description match
            if tool.manifest and hasattr(tool.manifest, 'description'):
                desc = tool.manifest.description.lower()
                if any(kw in desc for kw in keywords):
                    score += 5
            
            # Check capability match
            for cap in capabilities:
                if any(kw in cap.lower() for kw in keywords):
                    score += 3
            
            # Prefer lower risk tools
            if tool.manifest and hasattr(tool.manifest, 'risk_level'):
                if tool.manifest.risk_level == ToolRiskLevel.LOW:
                    score += 2
                elif tool.manifest.risk_level == ToolRiskLevel.HIGH:
                    score -= 5
            
            if score > 0:
                tool_scores.append((score, tool_name))
        
        if tool_scores:
            tool_scores.sort(reverse=True)
            best_tool = tool_scores[0][1]
            return RoutingDecision(
                subtask_id=subtask.id,
                target_type=RoutingTargetType.TOOL,
                target_id=best_tool,
                confidence=min(0.9, 0.5 + tool_scores[0][0] * 0.05),
                reasoning=f"Best matching tool: {best_tool}",
                fallback_targets=[
                    {"type": "tool", "id": t[1], "score": t[0]}
                    for t in tool_scores[1:3]
                ],
            )
        
        return None
    
    async def _find_best_agent(
        self,
        subtask: Subtask,
        execution_plan: ExecutionPlan,
    ) -> Optional[RoutingDecision]:
        """Find the best agent for a subtask."""
        # Check if any specialized agent matches
        required_caps = set()
        for req in execution_plan.analysis.requirements:
            required_caps.add(req.capability)
        
        agent_scores = []
        for agent_id, capabilities in self._agent_capabilities.items():
            if not await self._check_agent_availability(agent_id):
                continue
            
            score = 0
            agent_caps = set(capabilities)
            
            # Match required capabilities
            matches = len(required_caps & agent_caps)
            score += matches * 10
            
            # Check subtask type match
            if subtask.task_type.value in agent_caps:
                score += 5
            
            # Prefer general assistant for simple tasks
            if agent_id == "general_assistant":
                score += 2
            
            if score > 0:
                agent_scores.append((score, agent_id))
        
        if agent_scores:
            agent_scores.sort(reverse=True)
            best_agent = agent_scores[0][1]
            return RoutingDecision(
                subtask_id=subtask.id,
                target_type=RoutingTargetType.AGENT,
                target_id=best_agent,
                confidence=min(0.9, 0.4 + agent_scores[0][0] * 0.05),
                reasoning=f"Best matching agent: {best_agent}",
                fallback_targets=[
                    {"type": "agent", "id": a[1], "score": a[0]}
                    for a in agent_scores[1:3]
                ],
            )
        
        return None
    
    async def _check_tool_availability(self, tool_name: str) -> bool:
        """Check if tool is available and consent is granted."""
        tool = tool_registry.get_tool(tool_name)
        if not tool:
            return False
        
        # Check consent for tool's required permissions
        if hasattr(tool.manifest, 'required_permissions'):
            for perm in tool.manifest.required_permissions:
                if not consent_manager.is_granted(perm):
                    return False
        
        return True
    
    async def _check_agent_availability(self, agent_id: str) -> bool:
        """Check if agent is available and its required permissions are granted."""
        agent_info = agent_registry.get_agent_info(agent_id)
        if not agent_info:
            return False
        
        manifest = agent_info.get("manifest")
        if not manifest:
            return False
        
        # Check required permissions
        for perm in getattr(manifest, 'required_permissions', []):
            if not consent_manager.is_granted(perm):
                return False
        
        return True
    
    async def get_model_provider(self, preferred: Optional[str] = None) -> Optional[ModelProvider]:
        """Get the best available model provider."""
        if preferred:
            provider = provider_factory.get_provider(preferred)
            if provider:
                health = await provider.health_check_cached()
                if health.healthy:
                    return provider
        
        # Try primary
        primary = provider_factory.get_primary()
        if primary:
            health = await primary.health_check_cached()
            if health.healthy:
                return primary
        
        # Try fallbacks
        for provider in provider_factory.get_all_providers()[1:]:
            health = await provider.health_check_cached()
            if health.healthy:
                logger.info(f"Using fallback provider: {provider.id}")
                return provider
        
        return None
    
    async def route_with_fallback(
        self,
        subtask: Subtask,
        execution_plan: ExecutionPlan,
        failed_targets: List[str] = None,
    ) -> RoutingDecision:
        """Route a subtask with fallback handling."""
        failed_targets = failed_targets or []
        
        # Get primary decision
        decision = await self._route_subtask(subtask, execution_plan)
        
        # If primary failed, try fallbacks
        if decision.target_id in failed_targets:
            for fallback in decision.fallback_targets:
                fb_id = fallback.get("id")
                fb_type = fallback.get("type")
                if fb_id and fb_id not in failed_targets:
                    if fb_type == "tool" and await self._check_tool_availability(fb_id):
                        return RoutingDecision(
                            subtask_id=subtask.id,
                            target_type=RoutingTargetType.TOOL,
                            target_id=fb_id,
                            confidence=0.6,
                            reasoning=f"Fallback tool after {decision.target_id} failed",
                        )
                    elif fb_type == "agent" and await self._check_agent_availability(fb_id):
                        return RoutingDecision(
                            subtask_id=subtask.id,
                            target_type=RoutingTargetType.AGENT,
                            target_id=fb_id,
                            confidence=0.5,
                            reasoning=f"Fallback agent after {decision.target_id} failed",
                        )
        
        return decision


# Global router instance
router = IntelligentRouter()