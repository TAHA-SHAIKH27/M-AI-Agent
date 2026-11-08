"""Secure API key storage using Windows Credential Manager."""
import keyring
import structlog
from typing import Optional, List

from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)

SERVICE_NAME = "M-AI-Assistant"


class APIKeyVault:
    """Secure API key storage using Windows Credential Manager (DPAPI) + Fernet encryption, with fallback for restricted environments."""
    
    def __init__(self):
        self._memory_store = {}
        self._keyring_available = True
        self._fernet = self._get_or_create_fernet()
        self._provider_index_key = "provider_index"
    
    def _get_or_create_fernet(self):
        """Get or create Fernet encryption key stored in Windows Credential Manager or local fallback."""
        from cryptography.fernet import Fernet
        
        try:
            key_b64 = keyring.get_password(SERVICE_NAME, "master_key")
            if key_b64:
                try:
                    return Fernet(key_b64.encode())
                except Exception as e:
                    logger.warning(f"Failed to load existing master key: {e}")
            
            # Generate new key
            key = Fernet.generate_key()
            try:
                keyring.set_password(SERVICE_NAME, "master_key", key.decode())
            except Exception:
                self._keyring_available = False
            return Fernet(key)
        except Exception as e:
            # Fallback to local memory/file store in restricted/sandbox/headless environments
            self._keyring_available = False
            key = Fernet.generate_key()
            return Fernet(key)
    
    def store(self, provider: str, key: str) -> bool:
        """Encrypt and store API key."""
        try:
            encrypted = self._fernet.encrypt(key.encode()).decode()
            if self._keyring_available:
                try:
                    keyring.set_password(SERVICE_NAME, f"provider:{provider}", encrypted)
                    self._add_to_index(provider)
                    logger.info(f"Stored API key for {provider}")
                    return True
                except Exception:
                    self._keyring_available = False
            
            self._memory_store[provider] = encrypted
            self._add_to_index(provider)
            return True
        except Exception as e:
            logger.error(f"Failed to store API key for {provider}: {e}")
            return False
    
    def retrieve(self, provider: str) -> Optional[str]:
        """Retrieve and decrypt API key."""
        try:
            encrypted = None
            if self._keyring_available:
                try:
                    encrypted = keyring.get_password(SERVICE_NAME, f"provider:{provider}")
                except Exception:
                    self._keyring_available = False
            
            if not encrypted:
                encrypted = self._memory_store.get(provider)
                
            if not encrypted:
                return None
            return self._fernet.decrypt(encrypted.encode()).decode()
        except Exception as e:
            logger.error(f"Failed to retrieve API key for {provider}: {e}")
            return None
    
    def delete(self, provider: str) -> bool:
        """Delete stored API key."""
        try:
            if self._keyring_available:
                try:
                    keyring.delete_password(SERVICE_NAME, f"provider:{provider}")
                except Exception:
                    pass
            self._memory_store.pop(provider, None)
            self._remove_from_index(provider)
            logger.info(f"Deleted API key for {provider}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete API key for {provider}: {e}")
            return False
    
    def list_providers(self) -> List[str]:
        """List all providers with stored keys."""
        if self._keyring_available:
            try:
                index = keyring.get_password(SERVICE_NAME, self._provider_index_key)
                if index:
                    return [p for p in index.split(",") if p]
            except Exception:
                self._keyring_available = False
        return list(self._memory_store.keys())
    
    def _add_to_index(self, provider: str) -> None:
        """Add provider to index."""
        providers = self.list_providers()
        if provider not in providers:
            providers.append(provider)
            if self._keyring_available:
                try:
                    keyring.set_password(SERVICE_NAME, self._provider_index_key, ",".join(providers))
                except Exception:
                    pass
    
    def _remove_from_index(self, provider: str) -> None:
        """Remove provider from index."""
        providers = self.list_providers()
        if provider in providers:
            providers.remove(provider)
            if self._keyring_available:
                try:
                    keyring.set_password(SERVICE_NAME, self._provider_index_key, ",".join(providers))
                except Exception:
                    pass
    
    def has_key(self, provider: str) -> bool:
        """Check if provider has a stored key."""
        return provider in self.list_providers()


# Global vault instance
vault = APIKeyVault()