"""Base agent class for M AI Assistant."""
import asyncio
import json
import uuid
import structlog
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, AsyncIterator, AsyncIterable
from dataclasses import dataclass, field
from datetime import datetime

from ..core.types import ToolResult, ToolCall
from ..model_providers import provider_factory
from ..model_providers.schemas import (
    CompletionRequest, CompletionResponse, StreamChunk,
    ChatMessage, MessageRole, ToolSchema, ToolFunction, FinishReason,
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

logger = get_logger(__name__)


@dataclass
class AgentExecutionMetrics:
    """Metrics for agent execution."""
    start_time: float = 0.0
    end_time: float = 0.0
    total_turns: int = 0
    total_tool_calls: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    tool_calls_failed: int = 0
    errors: List[str] = field(default_factory=list)


class BaseAgent(ABC):
    """Abstract base class for all agents."""
    
    # Class attributes to be overridden by subclasses
    manifest: "AgentManifest"
    
    def __init__(self, config: Optional["AgentConfig"] = None):
        if not hasattr(self.__class__, 'manifest') or self.__class__.manifest is None:
            raise NotImplementedError("Subclasses must define a manifest")
        
        self._manifest = self._normalize_manifest(self.__class__.manifest)
        self._config = config or self._default_config()
        self._state = AgentState()
        self._metrics = AgentExecutionMetrics()
        self._conversation_history: List[Dict[str, Any]] = []
        self._running = False
        
        # Register with event bus for lifecycle events
        self._register_event_handlers()
    
    def _normalize_manifest(self, manifest: Any) -> Any:
        """Convert dict manifest to object with attribute access."""
        if isinstance(manifest, dict):
            class ManifestObj:
                def __init__(self, data):
                    for k, v in data.items():
                        setattr(self, k, v)
            return ManifestObj(manifest)
        return manifest
    
    def _register_event_handlers(self) -> None:
        """Register event handlers for lifecycle events."""
        pass
    
    def _default_config(self) -> "AgentConfig":
        """Default configuration for the agent."""
        from .schemas import AgentConfig, AgentManifest
        return AgentConfig(
            manifest=self.__class__.manifest,
        )
    
    @property
    def manifest(self) -> Any:
        return self._manifest
    
    @property
    def state(self) -> "AgentState":
        return self._state
    
    @property
    def config(self) -> "AgentConfig":
        if self._config is None:
            self._config = self._default_config()
        return self._config
    
    @config.setter
    def config(self, value: Optional["AgentConfig"]):
        self._config = value or self._default_config()
    
    @property
    def id(self) -> str:
        return self._manifest.id
    
    @property
    def name(self) -> str:
        return self._manifest.name
    
    @property
    def manifest_obj(self) -> "AgentManifest":
        return self._manifest
    
    def get_capabilities(self) -> List[str]:
        """Get agent capabilities."""
        return [c.value for c in self._manifest.capabilities]
    
    def get_required_permissions(self) -> List[str]:
        """Get required permissions."""
        return self._manifest.required_permissions
    
    def get_optional_permissions(self) -> List[str]:
        """Get optional permissions."""
        return self._manifest.optional_permissions
    
    async def start(self) -> None:
        """Start the agent."""
        self._running = True
        await event_bus.publish_sync("agent_started", self.id, {"agent_id": self.id})
        logger.info(f"Agent {self.name} started")
    
    async def stop(self) -> None:
        """Stop the agent."""
        self._running = False
        await event_bus.publish_sync("agent_stopped", self.id, {"agent_id": self.id})
        logger.info(f"Agent {self.name} stopped")
    
    async def process_request(self, request: "AgentRequest") -> "AgentResponse":
        """Process a user request and return response."""
        from ..model_providers import provider_factory
        from ..model_providers.schemas import CompletionRequest, ChatMessage, MessageRole, ToolSchema
        from ..tools import tool_registry
        from ..memory import memory_manager
        from ..security import consent_manager, audit_logger
        from ..core.exceptions import ConsentRequiredError
        import time
        
        self._metrics.start_time = time.time()
        correlation_id = request.conversation_id or str(uuid.uuid4())
        
        # Initialize conversation
        if request.conversation_id:
            self._state.conversation_id = request.conversation_id
        
        # Add user message to history
        user_message = {
            "role": "user",
            "content": request.message,
            "timestamp": datetime.now().isoformat(),
        }
        self._conversation_history.append(user_message)
        
        # Build conversation for model
        messages = self._build_conversation_messages(request)
        
        # Get available tools
        available_tools = await self._get_available_tools(request)
        
        # Process with tool-calling loop
        response = await self._process_with_tools(
            messages=messages,
            available_tools=available_tools,
            request=request,
            correlation_id=correlation_id,
        )
        
        # Add assistant response to history
        assistant_message = {
            "role": "assistant",
            "content": response.content,
            "tool_calls": response.tool_calls,
            "timestamp": datetime.now().isoformat(),
        }
        self._conversation_history.append(assistant_message)
        
        # Update state
        self._state.turn_count += 1
        self._state.last_error = None
        
        # Store in episodic memory
        await self._store_conversation_turn(request, response)
        
        return response
    
    async def process_stream(self, request: "AgentRequest") -> AsyncIterator["StreamChunk"]:
        """Process request with genuine token-by-token streaming."""
        from ..model_providers import provider_factory
        from ..model_providers.schemas import CompletionRequest, ChatMessage, MessageRole, StreamChunk
        
        correlation_id = request.conversation_id or str(uuid.uuid4())
        if request.conversation_id:
            self._state.conversation_id = request.conversation_id
            
        primary = provider_factory.get_primary()
        model_name = primary.get_default_model() if primary and hasattr(primary, "get_default_model") else None

        messages = self._build_conversation_messages(request)
        request_obj = CompletionRequest(
            model=model_name,
            messages=self._to_chat_messages(messages),
            temperature=request.temperature or self.config.temperature,
            stream=True,
            max_tokens=request.max_tokens or self.config.max_tokens,
        )
        
        full_content = []
        async for chunk in provider_factory.stream_with_fallback(request_obj):
            if chunk.content:
                full_content.append(chunk.content)
            yield chunk
            
        # Record assistant response once stream completes
        complete_text = "".join(full_content)
        user_message = {
            "role": "user",
            "content": request.message,
            "timestamp": datetime.now().isoformat(),
        }
        self._conversation_history.append(user_message)
        assistant_message = {
            "role": "assistant",
            "content": complete_text,
            "tool_calls": [],
            "timestamp": datetime.now().isoformat(),
        }
        self._conversation_history.append(assistant_message)
        self._state.turn_count += 1
    
    async def _process_with_tools(
        self,
        messages: List[Dict[str, Any]],
        available_tools: List[Dict[str, Any]],
        request: "AgentRequest",
        correlation_id: str,
    ) -> "AgentResponse":
        """Process messages with tool-calling loop."""
        from ..model_providers import provider_factory
        from ..model_providers.schemas import (
            CompletionRequest, CompletionResponse, ChatMessage,
            MessageRole, ToolSchema, ToolFunction, FinishReason
        )
        from ..tools import tool_registry
        from ..security import consent_manager, audit_logger
        from ..core.exceptions import ConsentRequiredError
        
        provider = provider_factory.get_primary()
        if not provider:
            raise RuntimeError("No model provider available")
        
        max_tool_calls = request.max_tool_calls or self.config.max_tool_calls_per_turn
        max_turns = getattr(request, "max_conversation_turns", None) or self.config.max_conversation_turns
        
        total_tool_calls = 0
        turn = 0
        final_content = ""
        all_tool_calls = []
        
        messages = messages.copy()
        
        while turn < max_turns and total_tool_calls < max_tool_calls:
            turn += 1
            self._state.turn_count = turn
            
            # Build completion request
            tools = None
            if available_tools:
                tools = []
                for t in available_tools:
                    # t is a dict from tool_registry.list_tools()
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
            
            model_name = provider.get_default_model() if hasattr(provider, "get_default_model") else None
            request_obj = CompletionRequest(
                model=model_name,
                messages=self._to_chat_messages(messages),
                temperature=request.temperature or self.config.temperature,
                tools=tools,
                tool_choice="auto" if available_tools else None,
                stream=False,
                max_tokens=request.max_tokens or self.config.max_tokens,
            )
            
            # Get completion from model
            response = await provider_factory.complete_with_fallback(request_obj)
            
            # Record assistant turn (preserving tool_calls when present)
            if response.content:
                final_content = response.content
            if response.content or response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": response.tool_calls,
                })
            
            # Handle tool calls
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    if total_tool_calls >= max_tool_calls:
                        break
                    
                    total_tool_calls += 1
                    self._state.tool_calls_this_turn += 1
                    tc_id, tc_name, tc_args = self._extract_tool_call_info(tool_call)
                    
                    # Execute tool
                    tool_result = await self._execute_tool_call(
                        tool_call=tool_call,
                        correlation_id=correlation_id,
                    )
                    
                    all_tool_calls.append({
                        "id": tc_id,
                        "name": tc_name,
                        "arguments": tc_args,
                        "result": tool_result.result,
                        "success": tool_result.success,
                    })
                    
                    # Add tool result to messages
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tc_name,
                        "content": str(tool_result.result) if tool_result.success else (tool_result.error or "Tool execution failed"),
                    })
            else:
                # No tool calls, we're done
                break
            
            # Check if we should continue
            if turn >= max_turns or total_tool_calls >= max_tool_calls:
                break
        
        # Build final response
        return AgentResponse(
            content=final_content or "Task completed.",
            tool_calls=[tc for tc in all_tool_calls if tc.get("success")],
            finish_reason="stop" if turn < max_turns else "max_turns",
            usage={},
            conversation_id=self._state.conversation_id,
            turn_count=turn,
            tool_calls_made=total_tool_calls,
        )

    def _to_chat_messages(self, messages: List[Dict[str, Any]]) -> List[ChatMessage]:
        """Convert dict message history to ChatMessage structs preserving tool metadata."""
        chat_messages: List[ChatMessage] = []
        for m in messages:
            role_raw = m.get("role", "user")
            role_str = role_raw.value if hasattr(role_raw, "value") else str(role_raw)
            chat_messages.append(
                ChatMessage(
                    role=MessageRole(role_str),
                    content=m.get("content"),
                    name=m.get("name"),
                    tool_calls=m.get("tool_calls"),
                    tool_call_id=m.get("tool_call_id"),
                )
            )
        return chat_messages

    def _extract_tool_call_info(self, tool_call: Any) -> tuple[str, str, Dict[str, Any]]:
        """Normalize dict/object OpenAI-style tool call into (id, name, parsed_arguments)."""
        if isinstance(tool_call, dict):
            tc_id = str(tool_call.get("id") or uuid.uuid4())
            fn = tool_call.get("function") or {}
            name = fn.get("name") or tool_call.get("name") or ""
            raw_args = fn.get("arguments") if "arguments" in fn else tool_call.get("arguments", {})
        else:
            tc_id = str(getattr(tool_call, "id", None) or uuid.uuid4())
            fn = getattr(tool_call, "function", None)
            name = getattr(fn, "name", "") if fn else getattr(tool_call, "name", "")
            raw_args = getattr(fn, "arguments", {}) if fn else getattr(tool_call, "arguments", {})

        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                parsed_args = {}
        elif isinstance(raw_args, dict):
            parsed_args = raw_args
        else:
            parsed_args = {}
        return tc_id, str(name), parsed_args

    async def _execute_tool_call(
        self,
        tool_call: Any,
        correlation_id: str,
    ) -> "ToolResult":
        """Execute a single tool call through the tool registry."""
        from ..tools import tool_registry

        _, name, args = self._extract_tool_call_info(tool_call)
        return await tool_registry.invoke(
            tool_name=name,
            arguments=args,
            correlation_id=correlation_id,
        )
    
    async def _store_conversation_turn(self, request: "AgentRequest", response: "AgentResponse") -> None:
        """Store conversation turn in episodic memory."""
        try:
            if self._state.conversation_id:
                await memory_manager.add_conversation_message(
                    conversation_id=self._state.conversation_id,
                    role="user",
                    content=request.message,
                    metadata={"correlation_id": str(uuid.uuid4())},
                )
                await memory_manager.add_conversation_message(
                    conversation_id=self._state.conversation_id,
                    role="assistant",
                    content=response.content,
                    metadata={
                        "tool_calls": [tc.get("name") for tc in response.tool_calls],
                        "turn_count": self._state.turn_count,
                    },
                )
        except Exception as e:
            logger.warning(f"Failed to store conversation turn: {e}")
    
    def _build_conversation_messages(self, request: "AgentRequest") -> List[Dict[str, Any]]:
        """Build message list for model."""
        messages = []
        
        # System prompt
        system_prompt = self.config.system_prompt_override or self.manifest.system_prompt
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        
        # Conversation history
        for msg in request.conversation_history[-self.config.max_conversation_turns:]:
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
        messages.append({"role": "user", "content": request.message})
        
        return messages
    
    async def _get_available_tools(self, request: "AgentRequest") -> List[Dict[str, Any]]:
        """Get available tools for this request."""
        allowed = set(request.allowed_tools) if request.allowed_tools else None
        blocked = set(request.blocked_tools) if request.blocked_tools else set(self.config.blocked_tools)
        
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
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get execution metrics."""
        return {
            "total_turns": self._metrics.total_turns,
            "total_tool_calls": self._metrics.total_tool_calls,
            "total_tokens": self._metrics.total_tokens,
            "total_latency_ms": self._metrics.total_latency_ms,
            "tool_calls_failed": self._metrics.tool_calls_failed,
            "errors": self._metrics.errors,
        }
    
    def get_state(self) -> "AgentState":
        return self._state
    
    def reset(self) -> None:
        """Reset agent state."""
        self._state = AgentState()
        self._conversation_history = []
        self._metrics = AgentExecutionMetrics()
    
    @abstractmethod
    async def _execute_impl(self, request: "AgentRequest") -> "AgentResponse":
        """Agent-specific execution logic."""
        pass