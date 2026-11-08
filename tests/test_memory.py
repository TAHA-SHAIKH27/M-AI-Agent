"""Tests for memory system."""
import pytest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from msgspec.structs import replace

from m_core.memory import (
    MemoryTier, MemoryEntry, MemorySearchQuery, MemorySearchResult,
    ShortTermMemory, EpisodicMemory, SemanticMemory, ProceduralMemory,
    MemoryManager, MemoryConfig,
)
from m_core.config import Settings
from m_core.security import consent_manager, Capability


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def settings(temp_dir):
    return Settings(
        debug=True,
        data_dir=temp_dir,
    )


@pytest.fixture
def memory_config():
    return MemoryConfig(
        stm_ttl_seconds=1,
        stm_max_entries=10,
        cleanup_interval_seconds=1,
    )


class TestShortTermMemory:
    """Tests for Short-Term Memory."""
    
    @pytest.fixture(autouse=True)
    def grant_consent(self):
        """Grant memory write consent before each test."""
        consent_manager.grant(Capability.MEMORY_WRITE)
        consent_manager.grant(Capability.MEMORY_READ)
        consent_manager.grant(Capability.MEMORY_DELETE)
        yield
        # Clean up after test
        consent_manager.revoke(Capability.MEMORY_WRITE)
        consent_manager.revoke(Capability.MEMORY_READ)
        consent_manager.revoke(Capability.MEMORY_DELETE)
    
    @pytest.fixture
    def stm(self, memory_config):
        stm = ShortTermMemory(memory_config)
        yield stm
        asyncio.run(stm.close())
    
    @pytest.mark.asyncio
    async def test_save_and_get(self, stm):
        entry = stm._create_entry("Test content", tags=["test"])
        await stm.save(entry)
        
        retrieved = await stm.get(entry.id)
        assert retrieved is not None
        assert retrieved.content == "Test content"
        assert retrieved.tags == ["test"]
    
    @pytest.mark.asyncio
    async def test_ttl_expiration(self, memory_config):
        memory_config.stm_ttl_seconds = 0  # Immediate expiration
        stm = ShortTermMemory(memory_config)
        
        entry = stm._create_entry("Expiring content")
        await stm.save(entry)
        
        # Immediately check - entry should be expired
        await stm.clear_expired()
        
        retrieved = await stm.get(entry.id)
        assert retrieved is None
        
        await stm.stop()
    
    @pytest.mark.asyncio
    async def test_lru_eviction(self, memory_config):
        memory_config.stm_max_entries = 3
        stm = ShortTermMemory(memory_config)
        
        entry_ids = []
        for i in range(5):
            entry = stm._create_entry(f"Content {i}")
            entry_ids.append(entry.id)
            await stm.save(entry)
        
        # Force LRU eviction to run
        await stm._enforce_max_entries()
        
        # Should have only 3 entries (the most recent 3)
        entries = await stm.list_all()
        assert len(entries) == 3
        
        # The oldest 2 entries should be evicted
        remaining_ids = {e.id for e in entries}
        assert entry_ids[0] not in remaining_ids  # First entry evicted
        assert entry_ids[1] not in remaining_ids  # Second entry evicted
        assert entry_ids[2] in remaining_ids  # Third entry kept
        assert entry_ids[3] in remaining_ids  # Fourth entry kept
        assert entry_ids[4] in remaining_ids  # Fifth entry kept
        
        await stm.stop()
    
    @pytest.mark.asyncio
    async def test_search(self, stm):
        entry1 = stm._create_entry("Hello world", tags=["greeting"])
        entry2 = stm._create_entry("Goodbye world", tags=["farewell"])
        entry3 = stm._create_entry("Hello there", tags=["greeting"])
        
        await stm.save(entry1)
        await stm.save(entry2)
        await stm.save(entry3)
        
        # Search for "world"
        query = MemorySearchQuery(query="world", tiers=[MemoryTier.STM], limit=10)
        result = await stm.search(query)
        
        assert result.total_count == 2
        assert len(result.entries) == 2
    
    @pytest.mark.asyncio
    async def test_delete(self, stm):
        entry = stm._create_entry("To be deleted")
        await stm.save(entry)
        
        deleted = await stm.delete(entry.id)
        assert deleted is True
        
        retrieved = await stm.get(entry.id)
        assert retrieved is None
    
    @pytest.mark.asyncio
    async def test_stats(self, stm):
        await stm.save(stm._create_entry("Test"))
        stats = stm.get_stats()
        
        assert stats["tier"] == "stm"
        assert stats["entry_count"] >= 1
        assert stats["max_entries"] == 10  # From test config


class TestEpisodicMemory:
    """Tests for Episodic Memory."""
    
    @pytest.fixture(autouse=True)
    def grant_consent(self):
        consent_manager.grant(Capability.MEMORY_WRITE)
        consent_manager.grant(Capability.MEMORY_READ)
        consent_manager.grant(Capability.MEMORY_DELETE)
        yield
        consent_manager.revoke(Capability.MEMORY_WRITE)
        consent_manager.revoke(Capability.MEMORY_READ)
        consent_manager.revoke(Capability.MEMORY_DELETE)
    
    @pytest.fixture
    def episodic(self, temp_dir):
        import uuid
        unique_db = temp_dir / f"episodic_{uuid.uuid4().hex}.db"
        episodic = EpisodicMemory()
        episodic._db_path = unique_db
        episodic._init_db()
        yield episodic
        asyncio.run(episodic.close())
        # Small delay to allow Windows to release file lock
        import time
        time.sleep(0.1)
    
    @pytest.mark.asyncio
    async def test_save_and_get(self, episodic):
        entry = episodic._create_entry(
            "User said hello",
            metadata={"conversation_id": "conv-1", "role": "user"},
            tags=["conversation"]
        )
        await episodic.save(entry)
        
        retrieved = await episodic.get(entry.id)
        assert retrieved is not None
        assert retrieved.content == "User said hello"
        assert retrieved.metadata["conversation_id"] == "conv-1"
    
    @pytest.mark.asyncio
    async def test_conversation_history(self, episodic):
        conv_id = "test-conv"
        
        # Add messages
        for i in range(3):
            entry = episodic._create_entry(
                f"Message {i}",
                metadata={"conversation_id": conv_id, "role": "user" if i % 2 == 0 else "assistant"},
                tags=["conversation"]
            )
            await episodic.save(entry)
        
        history = await episodic.get_conversation(conv_id)
        assert len(history) == 3
        assert history[0].content == "Message 0"
        assert history[2].content == "Message 2"
    
    @pytest.mark.asyncio
    async def test_recent_conversations(self, episodic):
        for conv_id in ["conv-1", "conv-2"]:
            entry = episodic._create_entry(
                f"First message in {conv_id}",
                metadata={"conversation_id": conv_id, "role": "user"},
                tags=["conversation"]
            )
            await episodic.save(entry)
        
        conversations = await episodic.get_recent_conversations(limit=5)
        assert len(conversations) >= 2
    
    @pytest.mark.asyncio
    async def test_search(self, episodic):
        entry1 = episodic._create_entry("User asked about weather", metadata={"conversation_id": "c1"})
        entry2 = episodic._create_entry("Assistant replied about rain", metadata={"conversation_id": "c2"})
        
        await episodic.save(entry1)
        await episodic.save(entry2)
        
        query = MemorySearchQuery(query="weather", tiers=[MemoryTier.EPISODIC])
        result = await episodic.search(query)
        
        assert result.total_count == 1
        assert "weather" in result.entries[0].content.lower()


class TestProceduralMemory:
    """Tests for Procedural Memory."""
    
    @pytest.fixture(autouse=True)
    def grant_consent(self):
        consent_manager.grant(Capability.MEMORY_WRITE)
        consent_manager.grant(Capability.MEMORY_READ)
        consent_manager.grant(Capability.MEMORY_DELETE)
        yield
        consent_manager.revoke(Capability.MEMORY_WRITE)
        consent_manager.revoke(Capability.MEMORY_READ)
        consent_manager.revoke(Capability.MEMORY_DELETE)
    
    @pytest.fixture
    def procedural(self, temp_dir):
        procedural = ProceduralMemory()
        procedural._base_path = temp_dir / "procedural"
        yield procedural
        asyncio.run(procedural.close())
    
    @pytest.mark.asyncio
    async def test_store_procedure(self, procedural):
        entry = procedural._create_entry(
            "Step 1: Do this\nStep 2: Do that",
            metadata={"name": "Test Procedure", "description": "A test procedure"},
            tags=["procedure", "test"]
        )
        await procedural.save(entry)
        
        retrieved = await procedural.get(entry.id)
        assert retrieved is not None
        assert retrieved.metadata["name"] == "Test Procedure"
        assert "Step 1" in retrieved.content
    
    @pytest.mark.asyncio
    async def test_procedure_versioning(self, procedural):
        # Save initial version
        entry = procedural._create_entry(
            "Version 1 content",
            metadata={"name": "Versioned Proc", "description": "Test versioning"},
        )
        await procedural.save(entry)
        
        # Update with new version - create new entry with updated content
        entry_v2 = replace(entry, content="Version 2 content")
        await procedural.save(entry_v2)
        
        proc = await procedural.get_procedure(entry.id)
        assert proc is not None
        assert proc.current_version == 2
        assert len(proc.versions) == 2
        assert proc.versions[0].content == "Version 1 content"
        assert proc.versions[1].content == "Version 2 content"
    
    @pytest.mark.asyncio
    async def test_get_version(self, procedural):
        entry_v1 = procedural._create_entry("V1", metadata={"name": "Test"})
        await procedural.save(entry_v1)
        entry_v2 = procedural._create_entry("V2", metadata={"name": "Test"})
        await procedural.save(entry_v2)
        
        v1 = await procedural.get_version(entry_v1.id, 1)
        assert v1 is not None
        assert v1.version == 1
        assert v1.content == "V1"


class TestSemanticMemory:
    """Tests for Semantic Memory (mocked since it needs ChromaDB)."""
    
    @pytest.fixture
    def semantic(self):
        semantic = SemanticMemory()
        # Mock the initialization
        semantic._initialized = True
        semantic._collection = MagicMock()
        semantic._embedding_model = MagicMock()
        semantic._embedding_model.encode.return_value = [0.1] * 384
        yield semantic
    
    @pytest.mark.asyncio
    async def test_initialize_without_deps(self):
        """Test that initialize raises helpful error without deps."""
        semantic = SemanticMemory()
        semantic._initialized = False
        semantic._client = None
        
        with patch('chromadb.PersistentClient') as mock_client:
            mock_client.side_effect = ImportError("No chromadb")
            
            with pytest.raises(RuntimeError) as exc_info:
                await semantic.initialize()
            
            assert "chromadb" in str(exc_info.value)


class TestMemoryManager:
    """Tests for MemoryManager."""
    
    @pytest.fixture(autouse=True)
    def grant_consent(self):
        consent_manager.grant(Capability.MEMORY_WRITE)
        consent_manager.grant(Capability.MEMORY_READ)
        consent_manager.grant(Capability.MEMORY_DELETE)
        yield
        consent_manager.revoke(Capability.MEMORY_WRITE)
        consent_manager.revoke(Capability.MEMORY_READ)
        consent_manager.revoke(Capability.MEMORY_DELETE)
    
    @pytest.fixture
    def memory_manager(self, temp_dir, memory_config):
        # Create a custom config with temp paths
        config = MemoryConfig(
            stm_ttl_seconds=3600,
            stm_max_entries=100,
        )
        
        # We'll test with mocked dependencies
        manager = MemoryManager(memory_config)
        
        # Mock the semantic memory since it requires ChromaDB
        manager.semantic = MagicMock()
        manager.semantic.initialize = AsyncMock()
        manager.semantic.save = AsyncMock()
        manager.semantic.get = AsyncMock(return_value=None)
        manager.semantic.delete = AsyncMock(return_value=True)
        manager.semantic.search = AsyncMock(return_value=MemorySearchResult(entries=[]))
        manager.semantic.list_all = AsyncMock(return_value=[])
        manager.semantic.clear_expired = AsyncMock(return_value=0)
        manager.semantic.close = AsyncMock()
        manager.semantic.get_stats = lambda: {"tier": "semantic", "total_entries": 0}
        
        # Mock procedural memory
        manager.procedural = MagicMock()
        manager.procedural.initialize = AsyncMock()
        manager.procedural.save = AsyncMock()
        manager.procedural.get = AsyncMock(return_value=None)
        manager.procedural.delete = AsyncMock(return_value=True)
        manager.procedural.search = AsyncMock(return_value=MemorySearchResult(entries=[]))
        manager.procedural.list_all = AsyncMock(return_value=[])
        manager.procedural.clear_expired = AsyncMock(return_value=0)
        manager.procedural.close = AsyncMock()
        manager.procedural.get_stats = lambda: {"tier": "procedural", "procedure_count": 0}
        
        # Mock episodic memory
        manager.episodic = MagicMock()
        manager.episodic.initialize = AsyncMock()
        manager.episodic.save = AsyncMock()
        manager.episodic.get = AsyncMock(return_value=None)
        manager.episodic.delete = AsyncMock(return_value=True)
        manager.episodic.search = AsyncMock(return_value=MemorySearchResult(entries=[]))
        manager.episodic.list_all = AsyncMock(return_value=[])
        manager.episodic.clear_expired = AsyncMock(return_value=0)
        manager.episodic.close = AsyncMock()
        manager.episodic.get_stats = lambda: {"tier": "episodic", "total_entries": 0}
        
        # Use real STM
        manager.stm = ShortTermMemory(memory_config)
        
        yield manager
        asyncio.run(manager.stop())
    
    @pytest.mark.asyncio
    async def test_save_and_get(self, memory_manager):
        entry = await memory_manager.save(
            content="Test content",
            tier=MemoryTier.STM,
            tags=["test"],
        )
        
        assert entry.content == "Test content"
        assert entry.tier == MemoryTier.STM
        assert entry.tags == ["test"]
    
    @pytest.mark.asyncio
    async def test_get_from_stm(self, memory_manager):
        # Save directly to STM
        entry = memory_manager.stm._create_entry("Direct STM content")
        await memory_manager.stm.save(entry)
        
        # Get through manager
        retrieved = await memory_manager.get(entry.id)
        assert retrieved is not None
        assert retrieved.content == "Direct STM content"
    
    @pytest.mark.asyncio
    async def test_save_with_tier(self, memory_manager):
        entry = await memory_manager.save(
            content="Episodic content",
            tier=MemoryTier.EPISODIC,
            tags=["conversation"],
        )
        
        # Verify it called the episodic mock
        memory_manager.episodic.save.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_consent_required(self, memory_manager):
        consent_manager.revoke(Capability.MEMORY_WRITE)
        
        with pytest.raises(Exception) as exc_info:
            await memory_manager.save("Test", tier=MemoryTier.STM)
        
        # Should be ConsentRequiredError
        assert "Consent required" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_search(self, memory_manager):
        # Save to STM
        entry = memory_manager.stm._create_entry("Searchable content", tags=["searchable"])
        await memory_manager.stm.save(entry)
        
        # Search through manager
        query = MemorySearchQuery(query="searchable", tiers=[MemoryTier.STM])
        result = await memory_manager.search(query)
        
        assert result.total_count >= 1
        assert any("Searchable" in e.content for e in result.entries)
    
    @pytest.mark.asyncio
    async def test_delete(self, memory_manager):
        entry = memory_manager.stm._create_entry("To delete")
        await memory_manager.stm.save(entry)
        
        deleted = await memory_manager.delete(entry.id)
        assert deleted is True
        
        retrieved = await memory_manager.get(entry.id)
        assert retrieved is None


class TestMemoryConfig:
    """Tests for MemoryConfig."""
    
    def test_default_config(self):
        config = MemoryConfig()
        assert config.stm_ttl_seconds == 3600
        assert config.stm_max_entries == 1000
        assert config.embedding_model == "all-MiniLM-L6-v2"
        assert config.embedding_dimension == 384
    
    def test_custom_config(self):
        config = MemoryConfig(
            stm_ttl_seconds=100,
            stm_max_entries=50,
            embedding_model="custom-model",
        )
        assert config.stm_ttl_seconds == 100
        assert config.stm_max_entries == 50
        assert config.embedding_model == "custom-model"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])