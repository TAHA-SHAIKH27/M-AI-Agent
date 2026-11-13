"""Permission and consent management."""
import json
import sys
import structlog
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class Capability(str, Enum):
    # Model
    MODEL_CHAT = "model:chat"
    MODEL_EMBED = "model:embed"
    MODEL_TOOLS = "model:tools"
    
    # File
    FILE_READ = "file:read"
    FILE_WRITE = "file:write"
    FILE_DELETE = "file:delete"
    FILE_SEARCH = "file:search"
    FILE_LIST = "file:list"
    
    # App
    APP_LAUNCH = "app:launch"
    APP_CONTROL = "app:control"
    APP_LIST = "app:list"
    APP_CLOSE = "app:close"
    
    # System
    SYSTEM_ADMIN = "system:admin"
    SYSTEM_VOLUME = "system:volume"
    SYSTEM_POWER = "system:power"
    SYSTEM_BRIGHTNESS = "system:brightness"
    SYSTEM_CLIPBOARD = "system:clipboard"
    SYSTEM_INFO = "system:info"
    
    # Web
    WEB_SEARCH = "web:search"
    WEB_BROWSE = "web:browse"
    WEB_AUTOMATE = "web:automate"
    WEB_DOWNLOAD = "web:download"
    
    # Voice
    VOICE_STT = "voice:stt"
    VOICE_TTS = "voice:tts"
    VOICE_WAKE = "voice:wake-word"
    VOICE_MIC = "voice:mic"
    
    # Phone
    PHONE_OUTBOUND = "phone:outbound"
    PHONE_INBOUND = "phone:inbound"
    PHONE_CONTACTS = "phone:contacts"
    PHONE_SMS = "phone:sms"
    PHONE_RECORD = "phone:record"
    
    # Screen
    SCREEN_CAPTURE = "screen:capture"
    SCREEN_OCR = "screen:ocr"
    SCREEN_ANALYZE = "screen:analyze"
    SCREEN_RECORD = "screen:record"
    
    # Automation
    AUTOMATION_INPUT = "automation:input"
    AUTOMATION_WINDOW = "automation:window"
    AUTOMATION_MACRO = "automation:macro"
    
    # Memory
    MEMORY_READ = "memory:read"
    MEMORY_WRITE = "memory:write"
    MEMORY_DELETE = "memory:delete"
    
    # Plugin
    PLUGIN_INSTALL = "plugin:install"
    PLUGIN_UPDATE = "plugin:update"
    PLUGIN_REMOVE = "plugin:remove"


class ConsentScope(str, Enum):
    GLOBAL = "global"
    SESSION = "session"
    PER_REQUEST = "per-request"


@dataclass
class ConsentRecord:
    capability: str
    granted: bool
    granted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expires_at: Optional[str] = None
    scope: str = ConsentScope.GLOBAL.value
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_valid(self) -> bool:
        """Check if consent is still valid."""
        if not self.granted:
            return False
        if self.expires_at:
            try:
                expires = datetime.fromisoformat(self.expires_at)
                if expires < datetime.now():
                    return False
            except ValueError:
                return False
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConsentRecord":
        return cls(**data)


class ConsentManager:
    """Manages user consent for capabilities."""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self._consents: Dict[str, ConsentRecord] = {}
        if data_dir is None:
            from ..config import settings
            data_dir = settings.data_dir
        self._consent_file = data_dir / "consents.json"
        self._load()
    
    def _load(self) -> None:
        """Load consents from file."""
        if self._consent_file.exists():
            try:
                data = json.loads(self._consent_file.read_text(encoding="utf-8"))
                for cap_str, record_data in data.items():
                    self._consents[cap_str] = ConsentRecord.from_dict(record_data)
                logger.info(f"Loaded {len(self._consents)} consent records")
            except Exception as e:
                logger.error(f"Failed to load consents: {e}")
    
    def _save(self) -> None:
        """Save consents to file."""
        try:
            self._consent_file.parent.mkdir(parents=True, exist_ok=True)
            data = {cap: record.to_dict() for cap, record in self._consents.items()}
            self._consent_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save consents: {e}")
    
    def is_granted(self, capability: Capability | str) -> bool:
        """Check if capability is granted."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        record = self._consents.get(cap_str)
        return record is not None and record.is_valid()
    
    def get_record(self, capability: Capability | str) -> Optional[ConsentRecord]:
        """Get consent record."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        return self._consents.get(cap_str)
    
    def grant(
        self,
        capability: Capability | str,
        scope: ConsentScope = ConsentScope.GLOBAL,
        ttl_hours: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ConsentRecord:
        """Grant consent for a capability."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        
        expires_at = None
        if ttl_hours:
            expires_at = (datetime.now() + timedelta(hours=ttl_hours)).isoformat()
        
        record = ConsentRecord(
            capability=cap_str,
            granted=True,
            scope=scope.value,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        
        self._consents[cap_str] = record
        self._save()
        logger.info(f"Granted consent for {cap_str} ({scope.value})")
        return record
    
    def revoke(self, capability: Capability | str) -> bool:
        """Revoke consent for a capability."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        if cap_str in self._consents:
            del self._consents[cap_str]
            self._save()
            logger.info(f"Revoked consent for {cap_str}")
            return True
        return False
    
    def request_consent(
        self,
        capability: Capability | str,
        context: str = "",
        scope: ConsentScope = ConsentScope.SESSION,
    ) -> bool:
        """Request consent from user (CLI mode)."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        
        if self.is_granted(cap_str):
            return True
        
        # Only prompt interactively if running in an interactive terminal and not testing
        if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty() and not getattr(settings, "testing", False):
            try:
                print(f"\n⚠️  Consent Required")
                print(f"   Capability: {cap_str}")
                if context:
                    print(f"   Reason: {context}")
                print(f"   Scope: {scope.value}")
                response = input("   Grant consent? [y/N]: ").strip().lower()
                if response in ("y", "yes"):
                    self.grant(cap_str, scope=scope)
                    return True
            except (EOFError, OSError):
                return False
        
        return False
    
    def require_consent(self, capability: Capability | str, context: str = "") -> None:
        """Require consent, raise if not granted."""
        cap_str = capability.value if isinstance(capability, Capability) else capability
        if not self.is_granted(cap_str):
            if not self.request_consent(cap_str, context):
                from ..core.exceptions import ConsentRequiredError
                raise ConsentRequiredError(
                    f"Consent required for capability: {cap_str}",
                    capability=cap_str,
                )
    
    def list_granted(self) -> List[ConsentRecord]:
        """List all granted consents."""
        return [r for r in self._consents.values() if r.is_valid()]
    
    def list_all(self) -> List[ConsentRecord]:
        """List all consent records."""
        return list(self._consents.values())
    
    def clear_expired(self) -> int:
        """Remove expired consents."""
        expired = [cap for cap, record in self._consents.items() if not record.is_valid()]
        for cap in expired:
            del self._consents[cap]
        if expired:
            self._save()
            logger.info(f"Cleared {len(expired)} expired consents")
        return len(expired)


# Global consent manager
consent_manager = ConsentManager()