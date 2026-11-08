"""Mock provider for testing."""
import asyncio
import json
from typing import AsyncIterator, List, Optional
from m_core.model_providers.base import ModelProvider
from m_core.model_providers.schemas import (
    CompletionRequest, CompletionResponse, StreamChunk, CompletionUsage,
    ModelInfo, ModelCapability, ProviderHealth, ChatMessage, FinishReason,
    EmbeddingRequest, EmbeddingResponse, MessageRole,
)


class MockProvider(ModelProvider):
    """Mock model provider for testing."""
    
    id: str = "mock"
    name: str = "Mock Provider"
    type: str = "local"
    capabilities: ModelCapability = ModelCapability(
        chat=True,
        streaming=True,
        function_calling=True,
        vision=False,
        reasoning=True,
        max_context_tokens=4096,
    )
    limits: dict = {}
    pricing: Optional[dict] = None
    
    def __init__(self, response_content: str = "Mock response", should_fail: bool = False):
        super().__init__()
        self._response_content = response_content
        self._should_fail = should_fail
        self._call_count = 0
    
    def get_default_model(self) -> str:
        return "mock-model"
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self._call_count += 1
        if self._should_fail:
            raise Exception("Mock provider configured to fail")
        
        # Check if this is a task understanding request (contains TASK_ANALYSIS_PROMPT keywords)
        is_task_analysis = False
        for msg in request.messages:
            if msg.role == MessageRole.SYSTEM and "task analysis" in msg.content.lower():
                is_task_analysis = True
                break
        
        # Check if this is a planning request
        is_planning = False
        for msg in request.messages:
            if msg.role == MessageRole.SYSTEM and "task planner" in msg.content.lower():
                is_planning = True
                break
        
        # Check if user message explicitly requests tool use
        user_message = ""
        for msg in request.messages:
            if msg.role == MessageRole.USER:
                user_message = msg.content or ""
                break
        
        # Determine if we should generate tool calls
        # Only generate tool calls for explicit tool requests, task analysis, or planning
        should_use_tools = is_task_analysis or is_planning or any(
            keyword in user_message.lower() 
            for keyword in ["search", "find", "read file", "list files", "delete file", "system info", "web search"]
        )
        
        tool_calls = None
        content = self._response_content
        
        if is_task_analysis:
            # Return a valid task analysis JSON for "Delete all files in C:\"
            content = json.dumps({
                "intent": {"primary": "file_operation", "secondary": [], "confidence": 0.95, "reasoning": "User wants to delete files"},
                "entities": [{"name": "C:\\", "type": "file_path", "value": "C:\\", "confidence": 0.9}],
                "requirements": [{"capability": "file:delete", "description": "Delete files", "required": True}],
                "task_type": "tool_use",
                "complexity": "medium",
                "needs_memory": False,
                "needs_tools": True,
                "estimated_subtasks": 1,
                "estimated_duration_seconds": 10.0,
                "risk_level": "critical",
                "requires_approval": True
            })
        elif is_planning:
            # Return a valid plan JSON
            content = json.dumps({
                "subtasks": [{
                    "id": "task-1",
                    "name": "Delete files",
                    "description": "Delete all files in C:\\",
                    "task_type": "tool_use",
                    "assigned_tool": "file_delete",
                    "dependencies": [],
                    "priority": 10,
                    "estimated_duration_seconds": 10.0,
                    "max_retries": 2,
                    "timeout_seconds": 60.0,
                    "requires_approval": True
                }],
                "strategy": "sequential",
                "max_concurrency": 1,
                "total_estimated_duration": 10.0
            })
        elif should_use_tools and request.tools and len(request.tools) > 0:
            # Use the first requested tool that matches the user's intent
            tool_schema = request.tools[0]
            tool_name = tool_schema.function.name if tool_schema.function else "system_info"
            
            # Generate appropriate arguments based on tool
            if tool_name == "system_info":
                arguments = "{}"
            elif tool_name == "web_search":
                arguments = '{"query": "test search"}'
            elif tool_name == "file_list":
                arguments = '{"path": ".", "recursive": false}'
            elif tool_name == "file_read":
                arguments = '{"path": "test.txt"}'
            elif tool_name == "memory_read":
                arguments = '{"entry_id": "test"}'
            else:
                arguments = "{}"
            
            tool_calls = [{
                "id": "call_123",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }]
        
        return CompletionResponse(
            id="mock-response-123",
            model=self.get_default_model(),
            content=content,
            tool_calls=tool_calls,
            finish_reason=FinishReason.STOP if not tool_calls else FinishReason.TOOL_CALLS,
            usage=CompletionUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=10.0,
        )
    
    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        if self._should_fail:
            raise Exception("Mock provider configured to fail")
        
        words = ["Hello!", "Count", "1", "2", "3", "2 + 2 = 4"] if "Count to 3" in "".join(m.content for m in request.messages) else self._response_content.split()
        for i, word in enumerate(words):
            yield StreamChunk(
                id=f"mock-chunk-{i}",
                content=word + (" " if i < len(words) - 1 else ""),
                is_final=(i == len(words) - 1),
                finish_reason=FinishReason.STOP if i == len(words) - 1 else None,
            )
            await asyncio.sleep(0.01)
    
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        if self._should_fail:
            raise Exception("Mock provider configured to fail")
        
        # Return mock embeddings
        embeddings = []
        for _ in request.input:
            embeddings.append([0.1] * 384)  # Mock 384-dim embedding
        
        return EmbeddingResponse(
            data=[{"embedding": e, "index": i} for i, e in enumerate(embeddings)],
            model="mock-embedding-model",
            usage=CompletionUsage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
        )
    
    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(healthy=True, latency_ms=1.0, model_count=1)
    
    async def health_check_cached(self, ttl_seconds: int = 300) -> ProviderHealth:
        return await self.health_check()
    
    async def list_models(self) -> List[ModelInfo]:
        return [ModelInfo(
            id="mock-model",
            name="Mock Model",
            provider=self.id,
            capabilities=self.capabilities,
            context_window=4096,
        )]


class FailingMockProvider(MockProvider):
    """Mock provider that always fails."""
    
    def __init__(self):
        super().__init__(should_fail=True)
    
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise Exception("Provider unavailable")
    
    async def stream(self, request: CompletionRequest):
        raise Exception("Provider unavailable")


# Test response content for common scenarios
MOCK_CHAT_RESPONSE = "Hello! I'm a mock assistant. 2 + 2 = 4"
MOCK_SYSTEM_INFO = "System: Linux, CPU: 4 cores, Memory: 16GB"