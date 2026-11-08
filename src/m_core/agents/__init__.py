"""Agent package for M AI Assistant."""
from .schemas import (
    AgentStatus,
    AgentType,
    AgentCapability,
    AgentManifest,
    AgentState,
    AgentConfig,
    AgentMessage,
    AgentRequest,
    AgentResponse,
    ToolCallRequest,
    ToolCallResult,
    AgentEvent,
)
from .registry import AgentRegistry, agent_registry
from .base import BaseAgent
from .general import GeneralAssistantAgent, GENERAL_ASSISTANT_MANIFEST

# Auto-register General Assistant Agent
agent_registry.register_agent_class(GeneralAssistantAgent)

__all__ = [
    # Schemas
    "AgentStatus",
    "AgentType",
    "AgentCapability",
    "AgentManifest",
    "AgentState",
    "AgentConfig",
    "AgentMessage",
    "AgentRequest",
    "AgentResponse",
    "ToolCallRequest",
    "ToolCallResult",
    "AgentEvent",
    # Registry
    "AgentRegistry",
    "agent_registry",
    # Base
    "BaseAgent",
    # Agents
    "GeneralAssistantAgent",
    "GENERAL_ASSISTANT_MANIFEST",
]