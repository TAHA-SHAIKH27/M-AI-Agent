"""Security package for M AI Assistant."""
from .vault import APIKeyVault, vault
from .consent import ConsentManager, Capability, ConsentScope, ConsentRecord, consent_manager
from .encryption import encrypt_data, decrypt_data, generate_key
from .audit import AuditLogger, audit_logger, AuditEventType

__all__ = [
    "APIKeyVault", "vault",
    "ConsentManager", "Capability", "ConsentScope", "ConsentRecord", "consent_manager",
    "encrypt_data", "decrypt_data", "generate_key",
    "AuditLogger", "audit_logger", "AuditEventType",
]