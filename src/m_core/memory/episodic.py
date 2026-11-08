"""Episodic Memory implementation using SQLite."""
import asyncio
import json
import sqlite3
import time
import uuid
import structlog
from typing import Optional, List, Dict, Any
from pathlib import Path
from contextlib import asynccontextmanager
from msgspec.structs import replace

from .base import MemoryTierBackend, MemoryConfig
from ..core.types import MemoryEntry, MemorySearchQuery, MemorySearchResult, MemoryTier
from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class EpisodicMemory(MemoryTierBackend):
    """SQLite-based episodic memory for conversation history and events."""
    
    def __init__(self, config: Optional[MemoryConfig] = None):
        super().__init__(MemoryTier.EPISODIC)
        self.config = config or MemoryConfig()
        self._db_path = settings.get_episodic_db_path()
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
    
    def _init_db(self) -> None:
        """Initialize the SQLite database with WAL mode for better Windows compatibility."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Create a persistent connection with WAL mode for better Windows compatibility
        self._conn = sqlite3.connect(
            self._db_path, 
            check_same_thread=False,
            isolation_level=None  # Autocommit mode
        )
        self._conn.row_factory = sqlite3.Row
        
        # Enable WAL mode for better Windows compatibility and concurrent access
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS episodic_memory (
                    id TEXT PRIMARY KEY,
                    tier TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    embedding TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    tags TEXT NOT NULL DEFAULT '[]'
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_episodic_created 
                ON episodic_memory(created_at)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_episodic_expires 
                ON episodic_memory(expires_at)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_episodic_tags 
                ON episodic_memory(tags)
            """)
        
        self._logger.info("Episodic memory initialized", db_path=str(self._db_path))
    
    def _execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a query with the persistent connection."""
        if self._conn is None:
            raise RuntimeError("Database connection not initialized")
        return self._conn.execute(query, params)
    
    def _execute_many(self, query: str, params_list: list) -> None:
        """Execute many queries with the persistent connection."""
        if self._conn is None:
            raise RuntimeError("Database connection not initialized")
        self._conn.executemany(query, params_list)
    
    def _commit(self) -> None:
        """Commit the current transaction."""
        if self._conn is not None:
            self._conn.commit()
    
    def _rollback(self) -> None:
        """Rollback the current transaction."""
        if self._conn is not None:
            self._conn.rollback()
    
    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """Convert database row to MemoryEntry."""
        return MemoryEntry(
            id=row["id"],
            tier=MemoryTier(row["tier"]),
            content=row["content"],
            metadata=json.loads(row["metadata"]),
            embedding=json.loads(row["embedding"]) if row["embedding"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            tags=json.loads(row["tags"]),
        )
    
    async def save(self, entry: MemoryEntry) -> None:
        """Save a memory entry to episodic memory."""
        if not self._check_consent("write"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory write", capability="memory:write")
        
        now = int(time.time() * 1000)
        # Create new entry with updated timestamp since MemoryEntry is immutable
        if entry.updated_at == entry.created_at:
            entry = replace(entry, updated_at=now)
        
        async with self._lock:
            cursor = self._execute("""
                INSERT OR REPLACE INTO episodic_memory 
                (id, tier, content, metadata, embedding, created_at, updated_at, expires_at, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id,
                entry.tier.value,
                entry.content,
                json.dumps(entry.metadata),
                json.dumps(entry.embedding) if entry.embedding else None,
                entry.created_at,
                entry.updated_at,
                entry.expires_at,
                json.dumps(entry.tags),
            ))
            self._commit()
        
        self._logger.debug("Episodic entry saved", entry_id=entry.id)
    
    async def get(self, entry_id: str) -> Optional[MemoryEntry]:
        """Retrieve a memory entry from episodic memory."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        async with self._lock:
            cursor = self._execute(
                "SELECT * FROM episodic_memory WHERE id = ?", (entry_id,)
            )
            row = cursor.fetchone()
            
            if row:
                entry = self._row_to_entry(row)
                # Check expiration
                if entry.expires_at:
                    now = int(time.time() * 1000)
                    if entry.expires_at < now:
                        await self.delete(entry_id)
                        return None
                return entry
            return None
    
    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry from episodic memory."""
        if not self._check_consent("delete"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory delete", capability="memory:delete")
        
        async with self._lock:
            cursor = self._execute(
                "DELETE FROM episodic_memory WHERE id = ?", (entry_id,)
            )
            deleted = cursor.rowcount > 0
            self._commit()
        
        if deleted:
            self._logger.debug("Episodic entry deleted", entry_id=entry_id)
        return deleted
    
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """Search episodic memory entries."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        start_time = time.time()
        await self.clear_expired()
        
        async with self._lock:
            # Build query
            sql = "SELECT * FROM episodic_memory WHERE 1=1"
            params = []
            
            if query.tiers and MemoryTier.EPISODIC not in query.tiers:
                return MemorySearchResult(entries=[], total_count=0, query_time_ms=0)
            
            if query.query:
                sql += " AND (content LIKE ? OR tags LIKE ?)"
                like_pattern = f"%{query.query}%"
                params.extend([like_pattern, like_pattern])
            
            # Apply filters
            if query.filters:
                for key, value in query.filters.items():
                    sql += f" AND json_extract(metadata, '$.{key}') = ?"
                    params.append(value)
            
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(query.limit)
            
            rows = self._execute(sql, params).fetchall()
            results = [self._row_to_entry(row) for row in rows]
            
            query_time = (time.time() - start_time) * 1000
            return MemorySearchResult(
                entries=results,
                total_count=len(results),
                query_time_ms=query_time,
            )
    
    async def list_all(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """List all episodic memory entries with pagination."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        await self.clear_expired()
        
        async with self._lock:
            rows = self._execute(
                "SELECT * FROM episodic_memory ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            
            return [self._row_to_entry(row) for row in rows]
    
    async def clear_expired(self) -> int:
        """Remove expired entries from episodic memory."""
        now = int(time.time() * 1000)
        
        async with self._lock:
            cursor = self._execute(
                "DELETE FROM episodic_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,)
            )
            removed = cursor.rowcount
            self._commit()
        
        if removed:
            self._logger.debug("Episodic expired entries cleared", count=removed)
        return removed
    
    async def close(self) -> None:
        """Close the database connection."""
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        self._logger.info("Episodic memory closed")
    
    async def get_conversation(self, conversation_id: str) -> List[MemoryEntry]:
        """Get all entries for a specific conversation."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        async with self._lock:
            rows = self._execute(
                "SELECT * FROM episodic_memory WHERE json_extract(metadata, '$.conversation_id') = ? ORDER BY created_at ASC",
                (conversation_id,)
            ).fetchall()
            
            return [self._row_to_entry(row) for row in rows]
    
    async def get_recent_conversations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent conversation summaries."""
        if not self._check_consent("read"):
            from ..core.exceptions import ConsentRequiredError
            raise ConsentRequiredError("Consent required for memory read", capability="memory:read")
        
        async with self._lock:
            rows = self._execute("""
                SELECT 
                    json_extract(metadata, '$.conversation_id') as conversation_id,
                    MIN(created_at) as started_at,
                    MAX(updated_at) as last_activity,
                    COUNT(*) as message_count,
                    MAX(CASE WHEN json_extract(metadata, '$.role') = 'user' THEN content END) as last_user_message
                FROM episodic_memory
                WHERE json_extract(metadata, '$.conversation_id') IS NOT NULL
                GROUP BY json_extract(metadata, '$.conversation_id')
                ORDER BY last_activity DESC
                LIMIT ?
            """, (limit,)).fetchall()
            
            return [dict(row) for row in rows]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get episodic memory statistics."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT COUNT(*) as count FROM episodic_memory").fetchone()
            total_count = row["count"] if row else 0
            
            row = conn.execute("""
                SELECT COUNT(DISTINCT json_extract(metadata, '$.conversation_id')) as count
                FROM episodic_memory
                WHERE json_extract(metadata, '$.conversation_id') IS NOT NULL
            """).fetchone()
            conversation_count = row["count"] if row else 0
        
        return {
            "tier": self.tier.value,
            "total_entries": total_count,
            "conversation_count": conversation_count,
            "db_path": str(self._db_path),
        }