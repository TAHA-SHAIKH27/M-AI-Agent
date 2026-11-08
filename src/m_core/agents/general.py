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
    
    async def _process_with_tools(
        self,
        messages: list,
        available_tools: list,
        request: "AgentRequest",
        correlation_id: str,
    ) -> "AgentResponse":
        """Process messages with tool-calling loop."""
        from ..model_providers import provider_factory
        from ..model_providers.schemas import (
            CompletionRequest, CompletionResponse, ChatMessage,
            MessageRole, ToolSchema, FinishReason,
        )
        from ..tools import tool_registry
        from ..security import consent_manager, audit_logger
        from ..core.exceptions import ConsentRequiredError
        from .schemas import AgentResponse
        
        provider = provider_factory.get_primary()
        if not provider:
            raise RuntimeError("No model provider available")
        
        # Handle AgentRequest object (Pydantic model) - access attributes directly
        max_tool_calls = getattr(request, 'max_tool_calls', None) or self._manifest.max_tool_calls_per_turn
        max_turns = self._manifest.max_conversation_turns
        temperature = getattr(request, 'temperature', None) or 0.7
        max_tokens = getattr(request, 'max_tokens', None) or 4096
        
        total_tool_calls = 0
        turn = 0
        final_content = ""
        all_tool_calls = []
        
        messages = messages.copy()
        
        while turn < max_turns and total_tool_calls < max_tool_calls:
            turn += 1
            self._state.turn_count = turn
            
            # Build completion request using proper schema
            from ..model_providers.schemas import CompletionRequest, ChatMessage, MessageRole, ToolSchema, ToolFunction
            
            chat_messages = [
                ChatMessage(role=MessageRole(m["role"]), content=m["content"])
                for m in messages
            ]
            
            tools = None
            if available_tools:
                tools = []
                for t in available_tools:
                    # t is a dict from tool_registry.list_tools()
                    # We need to create a ToolFunction with name, description, and parameters
                    # Get the tool's manifest for the input_schema (parameters)
                    tool_manifest = tool_registry.get_manifest(t["name"])
                    parameters = {}
                    if tool_manifest and hasattr(tool_manifest, 'input_schema'):
                        parameters = tool_manifest.input_schema
                    
                    tool_function = ToolFunction(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters=parameters
                    )
                    tools.append(ToolSchema(
                        type="function",
                        function=tool_function
                    ))
            
            request_obj = CompletionRequest(
                model=provider.get_default_model(),
                messages=chat_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                tools=tools,
                tool_choice="auto" if available_tools else None,
            )
            
            response = await provider_factory.complete_with_fallback(request_obj)
            
            # Handle response
            if response.content:
                final_content = response.content
                messages.append({"role": "assistant", "content": response.content})
            
            # Handle tool calls
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    total_tool_calls += 1
                    
                    # Execute tool - handle both dict and object formats
                    if isinstance(tool_call, dict):
                        tool_call_id = tool_call.get("id", str(uuid.uuid4()))
                        tool_name = tool_call.get("function", {}).get("name", "")
                        tool_args = tool_call.get("function", {}).get("arguments", {})
                        tool_call_obj = type('ToolCall', (), {
                            'id': tool_call_id,
                            'function': type('Function', (), {
                                'name': tool_name,
                                'arguments': tool_args
                            })()
                        })()
                    else:
                        tool_call_obj = tool_call
                    
                    # Execute tool
                    tool_result = await self._execute_tool_call(
                        tool_call=tool_call_obj,
                        correlation_id=str(uuid.uuid4()),
                    )
                    
                    all_tool_calls.append({
                        "id": tool_call_obj.id,
                        "name": tool_call_obj.function.name,
                        "arguments": tool_call_obj.function.arguments,
                        "result": tool_result.result,
                        "success": tool_result.success,
                    })
                    
                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_obj.id,
                        "name": tool_call_obj.function.name,
                        "content": str(tool_result.result) if tool_result.success else tool_result.error,
                    })
            else:
                # No tool calls, we're done
                break
            
            # Check if we should continue
            if turn >= max_turns or total_tool_calls >= max_tool_calls:
                break
        
        # Build final response as AgentResponse
        return AgentResponse(
            content=final_content or "Task completed.",
            tool_calls=[tc for tc in all_tool_calls if tc.get("success")],
            finish_reason="stop" if turn < max_turns else "max_turns",
            usage={},
            conversation_id=getattr(self._state, 'conversation_id', None),
            turn_count=turn,
            tool_calls_made=total_tool_calls,
            metadata={},
        )
    
    def _build_conversation_messages(self, request):
        """Build message list for model."""
        messages = []
        
        # System prompt
        system_prompt = self._manifest.system_prompt
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # Conversation history
        for msg in self._conversation_history[-self._manifest.max_conversation_turns:]:
            if hasattr(msg, 'role'):
                # Pydantic model
                messages.append({
                    "role": msg.role.value if hasattr(msg.role, 'value') else msg.role,
                    "content": msg.content or "",
                })
            else:
                # Dict
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })
        
        # Current message
        message = request.message if hasattr(request, 'message') else request.get("message", "")
        messages.append({"role": "user", "content": message})
        
        return messages
    
    async def _get_available_tools(self, request):
        """Get available tools for this request."""
        allowed = getattr(request, 'allowed_tools', None)
        if allowed:
            allowed = set(allowed)
        else:
            allowed = None
        blocked = set(getattr(request, 'blocked_tools', [])) if getattr(request, 'blocked_tools', None) else set(self._manifest.optional_permissions)
        
        # If allowed is specified, only use those
        if allowed is not None:
            allowed = allowed - blocked
            return [
                t for t in tool_registry.list_tools()
                if t["name"] in allowed
            ]
        
        # Otherwise use all except blocked
        return [
            t for t in tool_registry.list_tools()
            if t["name"] not in blocked
        ]
    
    async def _execute_tool_call(self, tool_call, correlation_id):
        """Execute a tool call through the tool registry."""
        from ..tools import tool_registry
        
        name = tool_call["function"]["name"] if isinstance(tool_call, dict) else tool_call.function.name
        args = tool_call["function"]["arguments"] if isinstance(tool_call, dict) else tool_call.function.arguments
        
        result = await tool_registry.invoke(
            tool_name=name,
            arguments=args,
            correlation_id=correlation_id,
        )
        
        return result

    async def _execute_impl(self, request: "AgentRequest") -> "AgentResponse":
        """Agent-specific execution logic - delegates to process_request."""
        # Call process_request directly with the AgentRequest object
        result = await self.process_request(request)
        
        return result


# Backwards compatibility alias
GENERAL_ASSISTANT_MANIFEST = GeneralAssistantAgent.manifest
