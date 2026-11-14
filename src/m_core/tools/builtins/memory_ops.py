"""Memory operations tools - read and search memory."""
import structlog
from typing import Dict, Any, List, Optional

from m_core.common_schemas import ToolExecutionContext, ToolResult
from ..base import BaseTool

logger = structlog.get_logger(__name__)


class MemoryReadTool(BaseTool):
    """Read a memory entry by ID."""
    
    manifest = {
        "name": "memory_read",
        "display_name": "Read Memory",
        "description": "Read a memory entry by ID from any tier.",
        "category": "memory",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "Memory entry ID to read"},
                "tier": {"type": "string", "enum": ["stm", "episodic", "semantic", "procedural"], "description": "Memory tier to search"},
            },
            "required": ["entry_id"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "entry": {"type": "object"},
            },
        },
        "required_permissions": ["memory:read"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only memory access. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 10,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["memory", "read", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        from ...memory import memory_manager, MemoryTier
        
        args = context.arguments
        entry_id = args.get("entry_id")
        tier_str = args.get("tier")
        
        tier = None
        if tier_str:
            from ...memory import MemoryTier
            try:
                tier = MemoryTier(tier_str.lower())
            except ValueError:
                return {"success": False, "error": f"Invalid tier: {tier_str}", "error_code": "INVALID_TIER"}
        
        entry = await memory_manager.get(entry_id, tier)
        
        if entry is None:
            return {"success": False, "error": f"Entry not found: {entry_id}", "error_code": "NOT_FOUND"}
        
        return {
            "success": True,
            "result": {
                "entry": {
                    "id": entry.id,
                    "tier": entry.tier.value,
                    "content": entry.content,
                    "metadata": entry.metadata,
                    "tags": entry.tags,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                    "expires_at": entry.expires_at,
                },
            },
            "error": None,
        }


class MemorySearchTool(BaseTool):
    """Search memories across tiers."""
    
    manifest = {
        "name": "memory_search",
        "display_name": "Search Memory",
        "description": "Search memories across tiers using semantic or text search.",
        "category": "memory",
        "risk_level": "low",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "tiers": {"type": "array", "items": {"type": "string", "enum": ["stm", "episodic", "semantic", "procedural"]}, "description": "Memory tiers to search"},
                "limit": {"type": "integer", "default": 10, "description": "Maximum results"},
                "threshold": {"type": "number", "default": 0.7, "description": "Similarity threshold"},
                "filters": {"type": "object", "description": "Metadata filters"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "entries": {"type": "array", "items": {"type": "object"}},
                "total_count": {"type": "integer"},
                "query_time_ms": {"type": "number"},
            },
        },
        "required_permissions": ["memory:read", "memory:search"],
        "optional_permissions": [],
        "risk_level": "low",
        "risk_description": "Read-only memory search. No modifications.",
        "requires_approval": False,
        "timeout_seconds": 15,
        "max_retries": 1,
        "version": "1.0.0",
        "author": "M Core",
        "tags": ["memory", "search", "read-only"],
    }
    
    async def _execute_impl(self, context) -> dict:
        from ...memory import memory_manager, MemoryTier, MemorySearchQuery
        
        args = context.arguments
        query = args.get("query")
        tiers = args.get("tiers")
        limit = args.get("limit", 10)
        threshold = args.get("threshold", 0.7)
        filters = args.get("filters", {})
        
        tier_enums = None
        if tiers:
            from ...memory import MemoryTier
            tier_enums = [MemoryTier(t.lower()) for t in tiers]
        
        query_obj = MemorySearchQuery(
            query=query,
            tiers=tier_enums,
            limit=limit,
            threshold=threshold,
            filters=filters,
        )
        
        result = await memory_manager.search(query_obj)
        
        entries = []
        for entry in result.entries:
            entries.append({
                "id": entry.id,
                "tier": entry.tier.value,
                "content": entry.content,
                "metadata": entry.metadata,
                "tags": entry.tags,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
            })
        
        return {
            "success": True,
            "result": {
                "entries": entries,
                "total_count": result.total_count,
                "query_time_ms": result.query_time_ms,
            },
            "error": None,
        }