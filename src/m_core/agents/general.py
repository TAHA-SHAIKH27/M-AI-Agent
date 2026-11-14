"""General Assistant Agent for M AI Assistant."""
import asyncio
import time
import uuid
import structlog
from typing import Optional, List, Dict, Any, AsyncIterator
from dataclasses import dataclass, field

from ..model_providers import provider_factory
from ..model_providers.schemas import (
    CompletionRequest, CompletionResponse, StreamChunk,
    ChatMessage, MessageRole, ToolSchema, FinishReason,
)
from ..memory import memory_manager
from ..tools import tool_registry
from ..security import consent_manager, audit_logger, AuditEventType
from ..core.events import event_bus
from ..utils.logging import get_logger
from .schemas import (
    AgentManifest, AgentConfig, AgentState, AgentStatus,
    AgentMessage, AgentRequest, AgentResponse,
    ToolCallRequest, ToolCallResult, AgentEvent, AgentCapability,
)
from .base import BaseAgent

logger = structlog.get_logger(__name__)


class GeneralAssistantAgent(BaseAgent):
    """General-purpose AI assistant with tool use, memory, and reasoning capabilities."""

    manifest = {
        "id": "general_assistant",
        "name": "General Assistant",
        "version": "1.0.0",
        "description": "General-purpose AI assistant with tool use, memory, and reasoning capabilities.",
        "type": "general",
        "capabilities": [
            "chat",
            "tool_use",
            "reasoning",
            "planning",
            "memory_access",
            "file_access",
            "web_access",
            "system_access",
        ],
        "required_permissions": [
            "memory:read",
            "memory:write",
            "file:read",
            "file:list",
            "web:search",
            "system:info",
        ],
        "optional_permissions": [
            "file:write",
            "file:delete",
            "web:browse",
            "web:automate",
            "app:launch",
            "app:control",
            "system:admin",
            "voice:stt",
            "voice:tts",
        ],
        "entry_point": "general_assistant",
        "api_version": "1.0",
        "min_m_version": "0.1.0",
        "max_tool_calls_per_turn": 10,
        "max_conversation_turns": 50,
        "supports_streaming": True,
        "supports_tools": True,
        "system_prompt": "You are M, a helpful AI assistant. You have access to tools and memory. Use them when appropriate to help the user accomplish their goals. Be concise but thorough. Think step by step. When using tools, explain what you're doing and why.",
        "metadata": {
            "category": "general",
            "priority": 100,
        },
    }

    def __init__(self, config=None):
        super().__init__(config)

    async def _execute_impl(self, request: "AgentRequest") -> "AgentResponse":
        """Agent-specific execution logic - delegates to process_request."""
        return await self.process_request(request)


# Backwards compatibility alias
GENERAL_ASSISTANT_MANIFEST = GeneralAssistantAgent.manifest
