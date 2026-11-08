"""Encryption utilities."""
import os
import base64
import structlog
from typing import Optional, Union
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = structlog.get_logger(__name__)


def generate_key() -> bytes:
    """Generate a new Fernet key."""
    return Fernet.generate_key()


def derive_key(password: str, salt: Optional[bytes] = None) -> tuple[bytes, bytes]:
    """Derive a key from password using PBKDF2."""
    if salt is None:
        salt = os.urandom(16)
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
    return key, salt


def encrypt_data(data: Union[str, bytes], key: bytes) -> str:
    """Encrypt data with Fernet."""
    f = Fernet(key)
    if isinstance(data, str):
        data = data.encode()
    encrypted = f.encrypt(data)
    return base64.urlsafe_b64encode(encrypted).decode()


def decrypt_data(encrypted_data: str, key: bytes) -> bytes:
    """Decrypt data with Fernet."""
    f = Fernet(key)
    data = base64.urlsafe_b64decode(encrypted_data.encode())
    return f.decrypt(data)


def encrypt_string(data: str, key: bytes) -> str:
    """Encrypt a string."""
    return encrypt_data(data, key)


def decrypt_string(encrypted_data: str, key: bytes) -> str:
    """Decrypt to string."""
    return decrypt_data(encrypted_data, key).decode()


class EncryptionContext:
    """Context manager for encryption operations."""
    
    def __init__(self, password: Optional[str] = None, key: Optional[bytes] = None):
        if key:
            self._key = key
            self._salt = None
        elif password:
            self._key, self._salt = derive_key(password)
        else:
            self._key = generate_key()
            self._salt = None
    
    @property
    def key(self) -> bytes:
        return self._key
    
    @property
    def salt(self) -> Optional[bytes]:
        return self._salt
    
    def encrypt(self, data: Union[str, bytes]) -> str:
        return encrypt_data(data, self._key)
    
    def decrypt(self, encrypted_data: str) -> bytes:
        return decrypt_data(encrypted_data, self._key)
    
    def encrypt_str(self, data: str) -> str:
        return encrypt_string(data, self._key)
    
    def decrypt_str(self, encrypted_data: str) -> str:
        return decrypt_string(encrypted_data, self._key)