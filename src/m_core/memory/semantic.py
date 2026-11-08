"""Semantic Memory implementation using ChromaDB for vector storage."""
import asyncio
import time
import uuid
import structlog
from typing import Optional, List, Dict, Any
from msgspec.structs import replace

from .base import MemoryTierBackend, MemoryConfig
from ..core.types import MemoryEntry, MemorySearchQuery, MemorySearchResult, MemoryTier
from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class SemanticMemory(MemoryTierBackend):
    """ChromaDB-based semantic memory for facts, concepts, and knowledge."""
    
    def __init__(self, config: Optional[MemoryConfig] = None):
        super().__init__(MemoryTier.SEMANTIC)
        self.config = config or MemoryConfig()
        self._client = None
        self._collection = None
        self._embedding_model = None
        self._lock = asyncio.Lock()
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize ChromaDB client and embedding model."""
        if self._initialized:
            return
        
        async with self._lock:
            if self._initialized:
                return
            
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings
                from .embedding import EmbeddingProvider, ChromaEmbeddingFunction
                
                # Fix ChromaDB/PostHog telemetry capture signature incompatibility
                try:
                    from chromadb.telemetry.product.posthog import Posthog
                    import posthog
                    def patched_direct_capture(self, event):
                        try:
                            posthog.capture(
                                event=event.name,
                                distinct_id=self.user_id,
                                properties={**event.properties, **self.context},
                            )
                        except Exception:
                            pass
                    Posthog._direct_capture = patched_direct_capture
                except Exception:
                    pass
                
                # Initialize ChromaDB client
                persist_path = settings.get_semantic_path()
                persist_path.mkdir(parents=True, exist_ok=True)
                
                self._client = chromadb.PersistentClient(
                    path=str(persist_path),
                    settings=ChromaSettings(anonymized_telemetry=False)
                )
                
                # Initialize embedding provider and Chroma adapter
                self._embedding_provider = EmbeddingProvider(
                    model_name=self.config.embedding_model,
                    dimension=self.config.embedding_dimension,
                )
                self._chroma_ef = ChromaEmbeddingFunction(self._embedding_provider)
                
                # Get or create collection with functional embedding function
                self._collection = self._client.get_or_create_collection(
                    name="semantic_memory",
                    embedding_function=self._chroma_ef,
                    metadata={"hnsw:space": "cosine"}
                )
                
                # Set legacy reference for backward compatibility
                self._embedding_model = self._embedding_provider
                
                self._initialized = True
                self._logger.info(
                    "Semantic memory initialized",
                    path=str(persist_path),
                    model=self.config.embedding_model,
                    dimension=self.config.embedding_dimension,
                )
                
            except ImportError as e:
                self._logger.error("Failed to import required dependencies", error=str(e))
                raise RuntimeError(
                    "Semantic memory requires 'chromadb' and 'sentence-transformers' packages. "
                    "Install with: pip install chromadb==1.0.8 sentence-transformers>=6.2.0"
                )
            except Exception as e:
                self._logger.error("Failed to initialize semantic memory", error=str(e))
                raise
    
    def _generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for text."""
        if hasattr(self, "_embedding_provider") and self._embedding_provider:
            return self._embedding_provider.embed(text)
        if hasattr(self, "_embedding_model") and self._embedding_model:
            if hasattr(self._embedding_model, "embed"):
                return self._embedding_model.embed(text)
            embedding = self._embedding_model.encode(text, normalize_embeddings=True)
            return embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        raise RuntimeError("Embedding model not initialized. Call initialize() first.")
    
    def _entry_to_chroma(self, entry: MemoryEntry) -> Dict[str, Any]:
        """Convert MemoryEntry to ChromaDB format."""
        return {
            "id": entry.id,
            "document": entry.content,
            "metadata": {
                **entry.metadata,
                "tier": entry.tier.value,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "expires_at": entry.expires_at or 0,
                "tags": ",".join(entry.tags) if entry.tags else "",
            },
            "embedding": entry.embedding or self._generate_embedding(entry.content),
        }
    
    def _chroma_to_entry(self, result: Dict[str, Any]) -> MemoryEntry:
        """Convert ChromaDB result to MemoryEntry."""
        metadata = result.get("metadata", {})
        tags_str = metadata.pop("tags", "")
        
        return MemoryEntry(
            id=result["id"],
            tier=MemoryTier.SEMANTIC,
            content=result["document"],
            metadata=metadata,
            embedding=result.get("embedding"),
            created_at=metadata.get("created_at", 0),
            updated_at=metadata.get("updated_at", 0),
            expires_at=metadata.get("expires_at") if metadata.get("expires_at", 0) > 0 else None,
            tags=tags_str.split(",") if tags_str else [],
        )
    
    async def save(self, entry: MemoryEntry) -> None:
        """Save a memory entry to semantic memory."""
        if not self._check_consent("write"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory write", capability="memory:write")
        
        if not self._initialized:
            await self.initialize()
        
        # Generate embedding if not provided
        if entry.embedding is None:
            entry = replace(entry, embedding=self._generate_embedding(entry.content))
        
        # Update timestamps - use replace since MemoryEntry is immutable
        now = int(time.time() * 1000)
        updates = {"updated_at": now}
        if entry.created_at == 0:
            updates["created_at"] = now
        entry = replace(entry, **updates)
        
        chroma_data = self._entry_to_chroma(entry)
        
        async with self._lock:
            self._collection.upsert(
                ids=[chroma_data["id"]],
                documents=[chroma_data["document"]],
                embeddings=[chroma_data["embedding"]],
                metadatas=[chroma_data["metadata"]],
            )
        
        self._logger.debug("Semantic entry saved", entry_id=entry.id)
    
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry from semantic memory."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._initialized:
            await self.initialize()
        
        async with self._lock:
            results = self._collection.get(
                ids=[entry_id],
                include=["documents", "metadatas", "embeddings"],
            )
            
            if results["ids"]:
                embeddings = results["embeddings"]
                embedding = None
                if embeddings is not None and len(embeddings) > 0:
                    embedding = embeddings[0]
                    # Handle numpy arrays
                    if hasattr(embedding, 'tolist'):
                        embedding = embedding.tolist()
                
                metadata = results["metadatas"][0] or {}
                return MemoryEntry(
                    id=results["ids"][0],
                    tier=MemoryTier.SEMANTIC,
                    content=results["documents"][0],
                    metadata=metadata,
                    embedding=embedding,
                    created_at=metadata.get("created_at", 0),
                    updated_at=metadata.get("updated_at", 0),
                    expires_at=metadata.get("expires_at") if metadata.get("expires_at", 0) > 0 else None,
                    tags=metadata.get("tags", "").split(",") if metadata.get("tags") else [],
                )
            return None
    
    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry from semantic memory."""
        if not self._check_consent("delete"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory delete", capability="memory:delete")
        
        if not self._initialized:
            await self.initialize()
        
        async with self._lock:
            self._collection.delete(ids=[entry_id])
        
        self._logger.debug("Semantic entry deleted", entry_id=entry_id)
        return True
    
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search semantic memory using vector similarity."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._initialized:
            await self.initialize()
        
        start_time = time.time()
        await self.clear_expired()
        
        if query.tiers and MemoryTier.SEMANTIC not in query.tiers:
            return MemorySearchResult(entries=[], total_count=0, query_time_ms=0)
        
        # Generate query embedding
        query_embedding = self._generate_embedding(query.query)
        
        # Build where clause for filters
        where_clause = {}
        if query.filters:
            for key, value in query.filters.items():
                where_clause[key] = value
        
        async with self._lock:
            if self._collection.count() == 0:
                return MemorySearchResult(
                    entries=[],
                    total_count=0,
                    query_time_ms=(time.time() - start_time) * 1000,
                )
            
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=query.limit,
                where=where_clause if where_clause else None,
                include=["documents", "metadatas", "embeddings", "distances"],
            )
        
        entries = []
        if results["ids"] and results["ids"][0]:
            for i, entry_id in enumerate(results["ids"][0]):
                distance = results["distances"][0][i] if results["distances"] else 0
                similarity = 1 - distance  # Cosine similarity
                
                if similarity >= query.threshold:
                    entry = MemoryEntry(
                        id=entry_id,
                        tier=MemoryTier.SEMANTIC,
                        content=results["documents"][0][i],
                        metadata=results["metadatas"][0][i] or {},
                        embedding=results["embeddings"][0][i] if results["embeddings"] else None,
                        created_at=results["metadatas"][0][i].get("created_at", 0),
                        updated_at=results["metadatas"][0][i].get("updated_at", 0),
                        expires_at=results["metadatas"][0][i].get("expires_at") if results["metadatas"][0][i].get("expires_at", 0) > 0 else None,
                        tags=results["metadatas"][0][i].get("tags", "").split(",") if results["metadatas"][0][i].get("tags") else [],
                    )
                    entries.append(entry)
        
        query_time = (time.time() - start_time) * 1000
        return MemorySearchResult(
            entries=entries,
            total_count=len(entries),
            query_time_ms=query_time,
        )
    
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """List all semantic memory entries with pagination."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        if not self._initialized:
            await self.initialize()
        
        async with self._lock:
            results = self._collection.get(
                limit=limit,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
        
        entries = []
        if results["ids"]:
            for i, entry_id in enumerate(results["ids"]):
                metadata = results["metadatas"][i] if results["metadatas"] else {}
                embeddings = results["embeddings"]
                embedding = None
                if embeddings is not None and len(embeddings) > i:
                    embedding = embeddings[i]
                    # Handle numpy arrays
                    if hasattr(embedding, 'tolist'):
                        embedding = embedding.tolist()
                
                entries.append(MemoryEntry(
                    id=entry_id,
                    tier=MemoryTier.SEMANTIC,
                    content=results["documents"][i] if results["documents"] else "",
                    metadata=metadata,
                    embedding=embedding,
                    created_at=metadata.get("created_at", 0),
                    updated_at=metadata.get("updated_at", 0),
                    expires_at=metadata.get("expires_at") if metadata.get("expires_at", 0) > 0 else None,
                    tags=metadata.get("tags", "").split(",") if metadata.get("tags") else [],
                ))
        
        return entries
    
    async def clear_expired(self) -> int:
        """Remove expired entries from semantic memory."""
        if not self._initialized:
            await self.initialize()
        
        now = int(time.time() * 1000)
        removed = 0
        
        async with self._lock:
            # Get all entries with expiration (expires_at > 0) and filter in Python
            # ChromaDB doesn't support multiple operators in a single where clause
            results = self._collection.get(
                where={"expires_at": {"$gt": 0}},
                include=["metadatas"],
            )
            
            if results["ids"]:
                expired_ids = [
                    entry_id for entry_id, metadata in zip(results["ids"], results["metadatas"])
                    if metadata.get("expires_at", 0) > 0 and metadata.get("expires_at", 0) < int(time.time() * 1000)
                ]
                if expired_ids:
                    self._collection.delete(ids=expired_ids)
                    removed = len(expired_ids)
        
        if removed:
            self._logger.debug("Semantic expired entries cleared", count=removed)
        return removed
    
    async def close(self) -> None:
        """Close semantic memory."""
        self._initialized = False
        self._logger.info("Semantic memory closed")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get semantic memory statistics."""
        if not self._initialized:
            return {"tier": self.tier.value, "initialized": False}
        
        count = self._collection.count()
        return {
            "tier": self.tier.value,
            "total_entries": count,
            "embedding_model": self.config.embedding_model,
            "embedding_dimension": self.config.embedding_dimension,
            "initialized": True,
        }