"""Unified Memory Manager for M AI Assistant."""
import asyncio
import time
import uuid
import structlog
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from .base import MemoryTier, MemoryEntry, MemorySearchQuery, MemorySearchResult, MemoryConfig
from .stm import ShortTermMemory
from .episodic import EpisodicMemory
from .semantic import SemanticMemory
from .procedural import ProceduralMemory
from ..config import settings
from ..core.exceptions import ConsentRequiredError
from ..security import consent_manager, audit_logger, AuditEventType
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ConsolidationResult:
    """Result of memory consolidation."""
    stm_to_episodic: int = 0
    stm_to_semantic: int = 0
    semantic_to_procedural: int = 0
    errors: List[str] = None
    
    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class MemoryManager:
    """Unified memory manager providing access to all four memory tiers."""
    
    def __init__(self, config: Optional[MemoryConfig] = None):
        self.config = config or MemoryConfig()
        
        # Initialize memory tiers
        self.stm = ShortTermMemory(self.config)
        self.episodic = EpisodicMemory(self.config)
        self.semantic = SemanticMemory(self.config)
        self.procedural = ProceduralMemory(self.config)
        
        self._running = False
        self._consolidation_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start all memory tiers and background tasks."""
        if self._running:
            return
        
        self._running = True
        
        # Start STM
        await self.stm.start()
        
        # Initialize other tiers
        await self.episodic.initialize() if hasattr(self.episodic, 'initialize') else None
        await self.semantic.initialize() if hasattr(self.semantic, 'initialize') else None
        await self.procedural.initialize() if hasattr(self.procedural, 'initialize') else None
        
        # Start background tasks
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        if self.config.auto_consolidate:
            self._consolidation_task = asyncio.create_task(self._consolidation_loop())
        
        logger.info("MemoryManager started")
    
    async def stop(self) -> None:
        """Stop all memory tiers and background tasks."""
        self._running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        if self._consolidation_task:
            self._consolidation_task.cancel()
            try:
                await self._consolidation_task
            except asyncio.CancelledError:
                pass
        
        # Stop all tiers
        await self.stm.stop()
        await self.episodic.close()
        await self.semantic.close()
        await self.procedural.close()
        
        logger.info("MemoryManager stopped")
    
    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of expired entries across all tiers."""
        while self._running:
            try:
                await asyncio.sleep(self.config.cleanup_interval_seconds)
                if self._running:
                    await self.clear_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Memory cleanup error", error=str(e))
    
    async def _consolidation_loop(self) -> None:
        """Periodic memory consolidation."""
        while self._running:
            try:
                await asyncio.sleep(self.config.consolidation_interval_seconds)
                if self._running:
                    await self.consolidate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Memory consolidation error", error=str(e))
    
    # =========================================================================
    # Unified Interface Methods
    # =========================================================================
    
    async def save(
        self,
        content: str,
        tier: MemoryTier = MemoryTier.STM,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        expires_at: Optional[int] = None,
        entry_id: Optional[str] = None,
    ) -> MemoryEntry:
        """Save a memory entry to the specified tier."""
        # Check consent
        if not consent_manager.is_granted("memory:write"):
            raise ConsentRequiredError("Consent required for memory write", capability="memory:write")
        
        # Create entry
        entry = MemoryEntry(
            id=entry_id or str(uuid.uuid4()),
            tier=tier,
            content=content,
            metadata=metadata or {},
            tags=tags or [],
            expires_at=expires_at,
        )
        
        # Log audit event
        audit_logger.log(
            event_type=AuditEventType.MEMORY_WRITE,
            capability="memory:write",
            resource=tier.value,
            action="save",
            details={"entry_id": entry.id, "tier": tier.value, "tags": tags},
        )
        
        # Route to appropriate tier
        if tier == MemoryTier.STM:
            await self.stm.save(entry)
        elif tier == MemoryTier.EPISODIC:
            await self.episodic.save(entry)
        elif tier == MemoryTier.SEMANTIC:
            await self.semantic.save(entry)
        elif tier == MemoryTier.PROCEDURAL:
            await self.procedural.save(entry)
        else:
            raise ValueError(f"Unknown memory tier: {tier}")
        
        return entry
    
    async def get(self, entry_id: str, tier: Optional[MemoryTier] = None) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by ID."""
        if not consent_manager.is_granted("memory:read"):
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        # If tier specified, search only that tier
        if tier:
            return await self._get_from_tier(entry_id, tier)
        
        # Search all tiers in priority order (STM -> Episodic -> Semantic -> Procedural)
        for t in [MemoryTier.STM, MemoryTier.EPISODIC, MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL]:
            entry = await self._get_from_tier(entry_id, t)
            if entry:
                return entry
        
        return None
    
    async def _get_from_tier(self, entry_id: str, tier: MemoryTier) -> Optional[MemoryEntry]:
        """Get entry from specific tier."""
        if tier == MemoryTier.STM:
            return await self.stm.get(entry_id)
        elif tier == MemoryTier.EPISODIC:
            return await self.episodic.get(entry_id)
        elif tier == MemoryTier.SEMANTIC:
            return await self.semantic.get(entry_id)
        elif tier == MemoryTier.PROCEDURAL:
            return await self.procedural.get(entry_id)
        return None
    
    async def delete(self, entry_id: str, tier: Optional[MemoryTier] = None) -> bool:
        """Delete a memory entry."""
        if not consent_manager.is_granted("memory:delete"):
            raise ConsentRequiredError("Consent required for memory delete", capability="memory:delete")
        
        audit_logger.log(
            event_type=AuditEventType.MEMORY_DELETE,
            capability="memory:delete",
            resource=entry_id,
            action="delete",
        )
        
        if tier:
            return await self._delete_from_tier(entry_id, tier)
        
        # Try all tiers
        deleted = False
        for t in [MemoryTier.STM, MemoryTier.EPISODIC, MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL]:
            if await self._delete_from_tier(entry_id, t):
                deleted = True
        return deleted
    
    async def _delete_from_tier(self, entry_id: str, tier: MemoryTier) -> bool:
        if tier == MemoryTier.STM:
            return await self.stm.delete(entry_id)
        elif tier == MemoryTier.EPISODIC:
            return await self.episodic.delete(entry_id)
        elif tier == MemoryTier.SEMANTIC:
            return await self.semantic.delete(entry_id)
        elif tier == MemoryTier.PROCEDURAL:
            return await self.procedural.delete(entry_id)
        return False
    
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search across memory tiers."""
        if not consent_manager.is_granted("memory:read"):
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        # Search specified tiers or all
        tiers_to_search = query.tiers or [
            MemoryTier.STM, MemoryTier.EPISODIC, MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL
        ]
        
        all_results = []
        total_count = 0
        max_query_time = 0.0
        
        for tier in tiers_to_search:
            if tier == MemoryTier.STM:
                result = await self.stm.search(query)
            elif tier == MemoryTier.EPISODIC:
                result = await self.episodic.search(query)
            elif tier == MemoryTier.SEMANTIC:
                result = await self.semantic.search(query)
            elif tier == MemoryTier.PROCEDURAL:
                result = await self.procedural.search(query)
            else:
                continue
            
            all_results.extend(result.entries)
            total_count += result.total_count
            max_query_time = max(max_query_time, result.query_time_ms)
        
        # Sort by relevance (could be improved with scoring)
        all_results = all_results[:query.limit]
        
        return MemorySearchResult(
            entries=all_results,
            total_count=total_count,
            query_time_ms=max_query_time,
        )
    
    async def list_all(
        self,
        tier: Optional[MemoryTier] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[MemoryEntry]:
        """List memory entries."""
        if not consent_manager.is_granted("memory:read"):
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if tier:
            return await self._list_tier(tier, limit, offset)
        
        # Combine all tiers
        all_entries = []
        for t in [MemoryTier.STM, MemoryTier.EPISODIC, MemoryTier.SEMANTIC, MemoryTier.PROCEDURAL]:
            entries = await self._list_tier(t, limit, offset)
            all_entries.extend(entries)
        
        return all_entries[:limit]
    
    async def _list_tier(self, tier: MemoryTier, limit: int, offset: int) -> List[MemoryEntry]:
        if tier == MemoryTier.STM:
            return await self.stm.list_all(limit, offset)
        elif tier == MemoryTier.EPISODIC:
            return await self.episodic.list_all(limit, offset)
        elif tier == MemoryTier.SEMANTIC:
            return await self.semantic.list_all(limit, offset)
        elif tier == MemoryTier.PROCEDURAL:
            return await self.procedural.list_all(limit, offset)
        return []
    
    async def clear_expired(self) -> Dict[str, int]:
        """Clear expired entries from all tiers."""
        results = {}
        
        for name, tier in [
            ("stm", self.stm),
            ("episodic", self.episodic),
            ("semantic", self.semantic),
            ("procedural", self.procedural),
        ]:
            try:
                count = await tier.clear_expired()
                results[name] = count
                if count > 0:
                    logger.info(f"Cleared expired entries from {name}", count=count)
            except Exception as e:
                logger.error(f"Failed to clear expired from {name}", error=str(e))
                results[name] = 0
        
        return results
    
    async def consolidate(self) -> ConsolidationResult:
        """Consolidate memories across tiers.
        
        This moves important memories from STM to Episodic/Semantic,
        and extracts procedures from Episodic to Procedural.
        """
        if not self.config.auto_consolidate:
            return ConsolidationResult()
        
        result = ConsolidationResult()
        
        try:
            # Move important STM entries to Episodic
            stm_entries = await self.stm.list_all(limit=100)
            for entry in stm_entries:
                # Check if entry is important (has certain tags or metadata)
                if self._should_consolidate_to_episodic(entry):
                    await self.episodic.save(entry)
                    await self.stm.delete(entry.id)
                    result.stm_to_episodic += 1
            
            # Move factual STM entries to Semantic
            for entry in stm_entries:
                if self._should_consolidate_to_semantic(entry):
                    await self.semantic.save(entry)
                    await self.stm.delete(entry.id)
                    result.stm_to_semantic += 1
            
            # Extract procedures from Episodic
            # This would be more sophisticated in production
            
        except Exception as e:
            result.errors.append(str(e))
            logger.error("Consolidation error", error=str(e))
        
        if any([result.stm_to_episodic, result.stm_to_semantic, result.semantic_to_procedural]):
            logger.info(
                "Memory consolidation completed",
                stm_to_episodic=result.stm_to_episodic,
                stm_to_semantic=result.stm_to_semantic,
            )
        
        return result
    
    def _should_consolidate_to_episodic(self, entry: MemoryEntry) -> bool:
        """Determine if STM entry should move to Episodic."""
        # Conversation entries, events, user interactions
        return (
            "conversation" in entry.tags or
            "event" in entry.tags or
            entry.metadata.get("type") in ("conversation", "event", "interaction")
        )
    
    def _should_consolidate_to_semantic(self, entry: MemoryEntry) -> bool:
        """Determine if STM entry should move to Semantic."""
        # Facts, preferences, learned knowledge
        return (
            "fact" in entry.tags or
            "preference" in entry.tags or
            "knowledge" in entry.tags or
            entry.metadata.get("type") in ("fact", "preference", "knowledge")
        )
    
    # =========================================================================
    # Convenience Methods
    # =========================================================================
    
    async def add_conversation_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEntry:
        """Add a message to conversation history (Episodic)."""
        meta = metadata or {}
        meta["conversation_id"] = conversation_id
        meta["role"] = role
        meta["type"] = "conversation"
        
        return await self.save(
            content=content,
            tier=MemoryTier.EPISODIC,
            metadata=meta,
            tags=["conversation", role],
        )
    
    async def learn_fact(
        self,
        fact: str,
        category: str = "general",
        confidence: float = 1.0,
        source: str = "user",
    ) -> MemoryEntry:
        """Store a fact in Semantic memory."""
        return await self.save(
            content=fact,
            tier=MemoryTier.SEMANTIC,
            metadata={
                "type": "fact",
                "category": category,
                "confidence": confidence,
                "source": source,
            },
            tags=["fact", category],
        )
    
    async def store_procedure(
        self,
        name: str,
        steps: str,
        description: str = "",
        tags: Optional[List[str]] = None,
    ) -> MemoryEntry:
        """Store a procedure/workflow in Procedural memory."""
        return await self.save(
            content=steps,
            tier=MemoryTier.PROCEDURAL,
            metadata={
                "name": name,
                "description": description,
                "type": "procedure",
            },
            tags=tags or ["procedure"],
        )
    
    async def get_conversation_history(
        self,
        conversation_id: str,
        limit: int = 50,
    ) -> List[MemoryEntry]:
        """Get conversation history from Episodic memory."""
        query = MemorySearchQuery(
            query="",
            tiers=[MemoryTier.EPISODIC],
            limit=limit,
            filters={"conversation_id": conversation_id},
        )
        result = await self.search(query)
        # Sort by timestamp
        result.entries.sort(key=lambda e: e.created_at)
        return result.entries
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get statistics for all memory tiers."""
        return {
            "stm": self.stm.get_stats(),
            "episodic": self.episodic.get_stats(),
            "semantic": self.semantic.get_stats(),
            "procedural": self.procedural.get_stats(),
            "config": {
                "stm_ttl_seconds": self.config.stm_ttl_seconds,
                "stm_max_entries": self.config.stm_max_entries,
                "retention_days": self.config.retention_days,
                "auto_consolidate": self.config.auto_consolidate,
            },
        }


# Global memory manager instance
memory_manager = MemoryManager()