"""Memory routes for REST API."""
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List, Dict, Any

from m_core.memory import memory_manager
from m_core.memory.base import MemoryTier
from m_core.security import consent_manager

from ..schemas import (
    MemorySearchRequest, MemorySearchResponse, MemoryEntryResponse,
    ErrorResponse,
)
from ..dependencies import (
    get_client_info, validate_localhost, require_consent, APIClientInfo,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


_memory_read_consent = require_consent("memory:read")


@router.post(
    "/search",
    response_model=MemorySearchResponse,
    summary="Search memory",
    description="Search across memory tiers for relevant entries",
)
async def search_memory(
    request: MemorySearchRequest,
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    _consent: None = Depends(_memory_read_consent),
) -> MemorySearchResponse:
    """Search memory across tiers."""
    logger.info(
        "memory_search_request",
        client=client.host,
        query=request.query[:100],
        tiers=request.tiers,
        limit=request.limit,
    )
    
    try:
        # Parse tiers
        tiers = None
        if request.tiers:
            tier_map = {
                "stm": MemoryTier.STM,
                "episodic": MemoryTier.EPISODIC,
                "semantic": MemoryTier.SEMANTIC,
                "procedural": MemoryTier.PROCEDURAL,
            }
            # Validate tiers
            invalid_tiers = [t for t in request.tiers if t not in tier_map]
            if invalid_tiers:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": f"Invalid memory tier(s): {', '.join(invalid_tiers)}",
                        "error_code": "INVALID_TIER",
                        "valid_tiers": list(tier_map.keys()),
                    },
                )
            tiers = [tier_map[t] for t in request.tiers]
        
        # Create search query
        from m_core.memory.base import MemorySearchQuery
        
        search_query = MemorySearchQuery(
            query=request.query,
            tiers=tiers or [MemoryTier.SEMANTIC, MemoryTier.EPISODIC],
            limit=request.limit,
            threshold=request.threshold,
            filters=request.filters,
        )
        
        # Execute search
        result = await memory_manager.search(search_query)
        
        # Convert to API response
        entries = []
        for entry in result.entries:
            entries.append(MemoryEntryResponse(
                id=entry.id,
                tier=entry.tier.value,
                content=entry.content,
                metadata=entry.metadata,
                tags=entry.tags,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                expires_at=entry.expires_at,
            ))
        
        logger.info(
            "memory_search_completed",
            client=client.host,
            results=len(entries),
            query_time_ms=result.query_time_ms,
        )
        
        return MemorySearchResponse(
            entries=entries,
            total_count=result.total_count,
            query_time_ms=result.query_time_ms,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("memory_search_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Memory search failed",
                "error_code": "MEMORY_SEARCH_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/entry/{entry_id}",
    response_model=MemoryEntryResponse,
    summary="Get memory entry",
    description="Retrieve a specific memory entry by ID",
)
async def get_memory_entry(
    entry_id: str,
    tier: Optional[str] = Query(None, description="Memory tier (stm, episodic, semantic, procedural)"),
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    _consent: None = Depends(_memory_read_consent),
) -> MemoryEntryResponse:
    """Get a memory entry by ID."""
    try:
        tier_enum = None
        if tier:
            tier_map = {
                "stm": MemoryTier.STM,
                "episodic": MemoryTier.EPISODIC,
                "semantic": MemoryTier.SEMANTIC,
                "procedural": MemoryTier.PROCEDURAL,
            }
            if tier not in tier_map:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"Invalid tier: {tier}",
                        "error_code": "INVALID_TIER",
                        "valid_tiers": list(tier_map.keys()),
                    },
                )
            tier_enum = tier_map[tier]
        
        entry = await memory_manager.get(entry_id, tier_enum)
        
        if not entry:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": f"Memory entry '{entry_id}' not found",
                    "error_code": "ENTRY_NOT_FOUND",
                },
            )
        
        return MemoryEntryResponse(
            id=entry.id,
            tier=entry.tier.value,
            content=entry.content,
            metadata=entry.metadata,
            tags=entry.tags,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
            expires_at=entry.expires_at,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_memory_entry_failed", entry_id=entry_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get memory entry",
                "error_code": "MEMORY_GET_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/conversation/{conversation_id}",
    response_model=List[MemoryEntryResponse],
    summary="Get conversation history",
    description="Retrieve conversation history from episodic memory",
)
async def get_conversation_history(
    conversation_id: str,
    limit: int = Query(50, ge=1, le=200),
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
    _consent: None = Depends(_memory_read_consent),
) -> List[MemoryEntryResponse]:
    """Get conversation history."""
    try:
        entries = await memory_manager.get_conversation_history(
            conversation_id=conversation_id,
            limit=limit,
        )
        
        return [
            MemoryEntryResponse(
                id=entry.id,
                tier=entry.tier.value,
                content=entry.content,
                metadata=entry.metadata,
                tags=entry.tags,
                created_at=entry.created_at,
                updated_at=entry.updated_at,
                expires_at=entry.expires_at,
            )
            for entry in entries
        ]
        
    except Exception as e:
        logger.error("get_conversation_history_failed", conversation_id=conversation_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get conversation history",
                "error_code": "CONVERSATION_HISTORY_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/stats",
    summary="Get memory statistics",
    description="Get statistics for all memory tiers",
)
async def get_memory_stats(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> Dict[str, Any]:
    """Get memory statistics."""
    try:
        stats = await memory_manager.get_stats()
        return stats
    except Exception as e:
        logger.error("memory_stats_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get memory stats",
                "error_code": "MEMORY_STATS_FAILED",
                "details": {"message": str(e)},
            },
        )