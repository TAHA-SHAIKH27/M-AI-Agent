"""Tests for security module."""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from m_core.security import (
    APIKeyVault,
    ConsentManager,
    Capability,
    ConsentScope,
    ConsentRecord,
)
from m_core.security.encryption import (
    generate_key, derive_key, encrypt_data, decrypt_data,
    encrypt_string, decrypt_string, EncryptionContext,
)
from m_core.config import Settings


class TestEncryption:
    """Test encryption utilities."""
    
    def test_generate_key(self):
        key = generate_key()
        assert len(key) == 44  # Fernet key is 32 bytes, base64 encoded = 44 chars
    
    def test_derive_key(self):
        key, salt = derive_key("test-password")
        assert len(key) == 44
        assert len(salt) == 16
        
        # Same password + same salt = same key
        key2, _ = derive_key("test-password", salt)
        assert key == key2
    
    def test_encrypt_decrypt_data(self):
        key = generate_key()
        data = b"Hello, World!"
        
        encrypted = encrypt_data(data, key)
        decrypted = decrypt_data(encrypted, key)
        
        assert decrypted == data
    
    def test_encrypt_decrypt_string(self):
        key = generate_key()
        text = "Hello, World! 🎉"
        
        encrypted = encrypt_string(text, key)
        decrypted = decrypt_string(encrypted, key)
        
        assert decrypted == text
    
    def test_encryption_context(self):
        ctx = EncryptionContext(password="test-password")
        
        encrypted = ctx.encrypt_str("Secret message")
        decrypted = ctx.decrypt_str(encrypted)
        
        assert decrypted == "Secret message"
    
    def test_encryption_context_with_key(self):
        key = generate_key()
        ctx = EncryptionContext(key=key)
        
        encrypted = ctx.encrypt_str("Secret message")
        decrypted = ctx.decrypt_str(encrypted)
        
        assert decrypted == "Secret message"


class TestConsentManager:
    """Test consent management."""
    
    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            yield tmp
    
    @pytest.fixture
    def settings(self, temp_dir):
        return Settings(
            debug=True,
            data_dir=temp_dir,
            consent_required=True,
        )
    
    @pytest.fixture
    def consent_manager(self, temp_dir):
        # Create a fresh ConsentManager with isolated data directory
        return ConsentManager(data_dir=Path(temp_dir))
    
    def test_initial_state(self, consent_manager):
        assert consent_manager.is_granted(Capability.MODEL_CHAT) is False
        assert consent_manager.is_granted("model:chat") is False
    
    def test_grant_and_check(self, consent_manager):
        consent_manager.grant(Capability.MODEL_CHAT)
        assert consent_manager.is_granted(Capability.MODEL_CHAT) is True
        assert consent_manager.is_granted("model:chat") is True
    
    def test_grant_with_scope(self, consent_manager):
        consent_manager.grant(Capability.FILE_READ, ConsentScope.SESSION)
        record = consent_manager.get_record(Capability.FILE_READ)
        assert record.scope == ConsentScope.SESSION.value
    
    def test_grant_with_ttl(self, consent_manager):
        consent_manager.grant(Capability.FILE_WRITE, ttl_hours=1)
        record = consent_manager.get_record(Capability.FILE_WRITE)
        assert record.expires_at is not None
    
    def test_revoke(self, consent_manager):
        consent_manager.grant(Capability.MODEL_CHAT)
        assert consent_manager.is_granted(Capability.MODEL_CHAT) is True
        
        consent_manager.revoke(Capability.MODEL_CHAT)
        assert consent_manager.is_granted(Capability.MODEL_CHAT) is False
    
    def test_list_granted(self, consent_manager):
        consent_manager.grant(Capability.MODEL_CHAT)
        consent_manager.grant(Capability.FILE_READ)
        
        granted = consent_manager.list_granted()
        assert len(granted) == 2
        caps = {r.capability for r in granted}
        assert Capability.MODEL_CHAT.value in caps
        assert Capability.FILE_READ.value in caps
    
    def test_persistence(self, temp_dir, settings):
        """Test consent persistence across instances."""
        mgr1 = ConsentManager()
        mgr1.grant(Capability.MODEL_CHAT, ConsentScope.GLOBAL)
        
        # Create new manager with same data dir
        mgr2 = ConsentManager()
        assert mgr2.is_granted(Capability.MODEL_CHAT) is True
    
    def test_clear_expired(self, consent_manager):
        # Grant with very short TTL (simulate expired)
        from datetime import datetime, timedelta
        from m_core.security.consent import ConsentRecord
        
        expired_record = ConsentRecord(
            capability=Capability.MODEL_CHAT.value,
            granted=True,
            expires_at=(datetime.now() - timedelta(hours=1)).isoformat(),
        )
        consent_manager._consents[Capability.MODEL_CHAT.value] = expired_record
        consent_manager._save()
        
        cleared = consent_manager.clear_expired()
        assert cleared == 1
        assert consent_manager.is_granted(Capability.MODEL_CHAT) is False


class TestAPIKeyVault:
    """Test API key vault."""
    
    @pytest.fixture
    def vault(self):
        return APIKeyVault()
    
    def test_store_and_retrieve(self, vault):
        provider = "test-provider"
        key = "sk-test-1234567890abcdef"
        
        result = vault.store(provider, key)
        assert result is True
        
        retrieved = vault.retrieve(provider)
        assert retrieved == key
    
    def test_retrieve_nonexistent(self, vault):
        retrieved = vault.retrieve("nonexistent")
        assert retrieved is None
    
    def test_delete(self, vault):
        provider = "test-provider"
        key = "sk-test-1234567890abcdef"
        
        vault.store(provider, key)
        assert vault.has_key(provider) is True
        
        result = vault.delete(provider)
        assert result is True
        assert vault.has_key(provider) is False
        assert vault.retrieve(provider) is None
    
    def test_list_providers(self, vault):
        vault.store("provider1", "key1")
        vault.store("provider2", "key2")
        
        providers = vault.list_providers()
        assert "provider1" in providers
        assert "provider2" in providers
        assert len(providers) >= 2
    
    def test_overwrite_key(self, vault):
        provider = "test-provider"
        vault.store(provider, "key1")
        vault.store(provider, "key2")  # Should overwrite
        
        retrieved = vault.retrieve(provider)
        assert retrieved == "key2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])