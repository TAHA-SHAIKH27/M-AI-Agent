"""Base memory interfaces and shared types."""
import abc
import structlog
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from ..core.types import MemoryTier, MemoryEntry, MemorySearchQuery, MemorySearchResult
from ..utils.logging import get_logger

logger = get_logger(__name__)


class MemoryBackend(abc.ABC):
    """Abstract base class for memory storage backends."""
    
    @abc.abstractmethod
    async def save(self, entry: MemoryEntry) -> None:
        """Save a memory entry."""
        pass
    
    @abc.abstractmethod
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry by ID."""
        pass
    
    @abc.abstractmethod
    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry by ID."""
        pass
    
    @abc.abstractmethod
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search memory entries."""
        pass
    
    @abc.abstractmethod
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """List all memory entries with pagination."""
        pass
    
    @abc.abstractmethod
    async def clear_expired(self) -> int:
        """Remove expired entries. Returns count of removed entries."""
        pass
    
    @abc.abstractmethod
    async def close(self) -> None:
        """Close the backend and release resources."""
        pass


class MemoryTierBackend(MemoryBackend):
    """Base class for tier-specific memory backends."""
    
    def __init__(self, tier: MemoryTier):
        self.tier = tier
        self._logger = logger.bind(tier=tier.value)
    
    def _create_entry(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        expires_at: Optional[int] = None,
        embedding: Optional[List[float]] = None,
    ) -> MemoryEntry:
        """Create a new memory entry with the correct tier."""
        import uuid
        import time
        
        now = int(time.time() * 1000)
        return MemoryEntry(
            id=str(uuid.uuid4()),
            tier=self.tier,
            content=content,
            metadata=metadata or {},
            embedding=embedding,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
            tags=tags or [],
        )
    
    def _check_consent(self, capability: str) -> bool:
        """Check if the required memory consent is granted."""
        from ..security import consent_manager, Capability
        
        cap_map = {
            "read": Capability.MEMORY_READ,
            "write": Capability.MEMORY_WRITE,
            "delete": Capability.MEMORY_DELETE,
        }
        cap = cap_map.get(capability)
        if cap:
            return consent_manager.is_granted(cap)
        return True


@dataclass
class MemoryConfig:
    """Configuration for memory system."""
    stm_ttl_seconds: int = 3600
    stm_max_entries: int = 1000
    episodic_db_path: str = "sqlite:///data/memory/episodic.db"
    semantic_db_path: str = "data/memory/semantic"
    procedural_path: str = "data/memory/procedural"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384
    semantic_similarity_threshold: float = 0.7
    semantic_max_results: int = 10
    procedural_version_keep: int = 5
    cleanup_interval_seconds: int = 300
    auto_consolidate: bool = True
    consolidation_interval_seconds: int = 3600
    retention_days: int = 365