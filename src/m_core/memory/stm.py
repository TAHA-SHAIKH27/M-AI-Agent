"""Short-Term Memory (STM) implementation."""
import asyncio
import time
import structlog
from typing import Optional, List, Dict, Any
from collections import OrderedDict
from msgspec.structs import replace

from .base import MemoryBackend, MemoryTierBackend, MemoryConfig
from ..core.types import MemoryEntry, MemorySearchQuery, MemorySearchResult, MemoryTier
from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class ShortTermMemory(MemoryTierBackend):
    """In-memory short-term memory with TTL and LRU eviction."""
    
    def __init__(self, config: Optional[MemoryConfig] = None):
        super().__init__(MemoryTier.STM)
        self.config = config or MemoryConfig()
        self._entries: OrderedDict[str, MemoryEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def start(self) -> None:
        """Start the STM cleanup task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._logger.info("STM started", ttl_seconds=self.config.stm_ttl_seconds)
    
    async def stop(self) -> None:
        """Stop the STM cleanup task."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._logger.info("STM stopped")
    
    async def _cleanup_loop(self) -> None:
        """Periodic cleanup of expired entries."""
        while self._running:
            try:
                await asyncio.sleep(self.config.cleanup_interval_seconds)
                if self._running:
                    await self.clear_expired()
                    await self._enforce_max_entries()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error("STM cleanup error", error=str(e))
    
    async def _enforce_max_entries(self) -> int:
        """Enforce maximum entry limit using LRU eviction."""
        removed = 0
        while len(self._entries) > self.config.stm_max_entries:
            self._entries.popitem(last=False)  # Remove oldest (LRU)
            removed += 1
        if removed:
            self._logger.debug("STM LRU eviction", removed=removed)
        return removed
    
    async def save(self, entry: MemoryEntry) -> None:
        """Save a memory entry to STM."""
        if not self._check_consent("write"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory write", capability="memory:write")
        
        async with self._lock:
            # Set TTL if not already set by creating a new entry with updated expires_at
            if entry.expires_at is None:
                entry = replace(entry, expires_at=int(time.time() * 1000) + (self.config.stm_ttl_seconds * 1000))
            
            # Move to end (most recently used)
            if entry.id in self._entries:
                del self._entries[entry.id]
            
            self._entries[entry.id] = entry
            self._logger.debug("STM entry saved", entry_id=entry.id, ttl_ms=entry.expires_at - int(time.time() * 1000) if entry.expires_at else None)
    
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry from STM."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        async with self._lock:
            entry = self._entries.get(entry_id)
            if entry:
                # Check expiration (<= for immediate expiration)
                now = int(time.time() * 1000)
                if entry.expires_at and entry.expires_at <= now:
                    del self._entries[entry_id]
                    return None
                # Move to end (most recently used)
                self._entries.move_to_end(entry_id)
            return entry
    
    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry from STM."""
        if not self._check_consent("delete"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory delete", capability="memory:delete")
        
        async with self._lock:
            if entry_id in self._entries:
                del self._entries[entry_id]
                self._logger.debug("STM entry deleted", entry_id=entry_id)
                return True
            return False
    
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search STM entries (simple text matching)."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        start_time = time.time()
        async with self._lock:
            now = int(time.time() * 1000)
            
            results = []
            query_lower = query.query.lower()
            
            # Iterate over a copy of entries to avoid modification during iteration
            entries_copy = list(self._entries.values())
            
            for entry in entries_copy:
                if query.tiers and MemoryTier.STM not in query.tiers:
                    continue
                
                # Check expiration inline
                if entry.expires_at and entry.expires_at < now:
                    continue
                
                # Simple text search in content and tags
                if (query_lower in entry.content.lower() or 
                    any(query_lower in tag.lower() for tag in entry.tags)):
                    # Apply filters
                    if query.filters:
                        match = True
                        for key, value in query.filters.items():
                            if entry.metadata.get(key) != value:
                                match = False
                                break
                        if not match:
                            continue
                        
                    results.append(entry)
                    if len(results) >= query.limit:
                        break
            
            query_time = (time.time() - start_time) * 1000
            return MemorySearchResult(
                entries=results,
                total_count=len(results),
                query_time_ms=query_time,
            )
    
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """List all STM entries with pagination."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        async with self._lock:
            now = int(time.time() * 1000)
            
            # Filter out expired entries
            valid_entries = [
                entry for entry in self._entries.values()
                if not (entry.expires_at and entry.expires_at < now)
            ]
            
            return valid_entries[offset:offset + limit]
    
    async def clear_expired(self) -> int:
        """Remove expired entries from STM."""
        now = int(time.time() * 1000)
        removed = 0
        async with self._lock:
            expired_ids = [
                entry_id for entry_id, entry in self._entries.items()
                if entry.expires_at and entry.expires_at <= now
            ]
            for entry_id in expired_ids:
                del self._entries[entry_id]
                removed += 1
        
        if removed:
            self._logger.debug("STM expired entries cleared", count=removed)
        return removed
    
    async def close(self) -> None:
        """Close STM and release resources."""
        await self.stop()
        async with self._lock:
            self._entries.clear()
        self._logger.info("STM closed")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get STM statistics."""
        return {
            "tier": self.tier.value,
            "entry_count": len(self._entries),
            "max_entries": self.config.stm_max_entries,
            "ttl_seconds": self.config.stm_ttl_seconds,
            "running": self._running,
        }