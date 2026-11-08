"""Audit logging for security events."""
import json
import structlog
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path
from enum import Enum

from ..config import settings
from ..utils.logging import get_logger

logger = get_logger(__name__)


class AuditEventType(str, Enum):
    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_DENIED = "consent_denied"
    API_KEY_STORED = "api_key_stored"
    API_KEY_RETRIEVED = "api_key_retrieved"
    API_KEY_DELETED = "api_key_deleted"
    TOOL_EXECUTED = "tool_executed"
    TOOL_FAILED = "tool_failed"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    MODEL_ERROR = "model_error"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    APP_LAUNCHED = "app_launched"
    APP_CONTROLLED = "app_controlled"
    SYSTEM_COMMAND = "system_command"
    PHONE_CALL = "phone_call"
    PHONE_SMS = "phone_sms"
    VOICE_STT = "voice_stt"
    VOICE_TTS = "voice_tts"
    SCREEN_CAPTURE = "screen_capture"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    MEMORY_DELETE = "memory_delete"
    MEMORY_SEARCH = "memory_search"
    MEMORY_CONSOLIDATE = "memory_consolidate"
    ERROR = "error"


class AuditLogger:
    """Structured audit logger for security events."""
    
    def __init__(self):
        self._enabled = settings.audit_log_enabled
        self._log_file = settings.data_dir / "logs" / "audit.log"
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def log(
        self,
        event_type: AuditEventType,
        user: str = "system",
        capability: Optional[str] = None,
        resource: Optional[str] = None,
        action: Optional[str] = None,
        result: str = "success",
        details: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> None:
        """Log an audit event."""
        if not self._enabled:
            return
        
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type.value,
            "user": user,
            "capability": capability,
            "resource": resource,
            "action": action,
            "result": result,
            "details": details or {},
            "correlation_id": correlation_id,
        }
        
        # Remove None values
        event = {k: v for k, v in event.items() if v is not None}
        
        # Write to audit log file
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")
        
        # Also log via structlog
        log_method = logger.info if result == "success" else logger.warning
        log_method(
            "audit_event",
            event_type=event_type.value,
            user=user,
            capability=capability,
            resource=resource,
            action=action,
            result=result,
            correlation_id=correlation_id,
        )
    
    def log_consent(self, capability: str, granted: bool, scope: str = "global", **kwargs) -> None:
        """Log consent decision."""
        self.log(
            event_type=AuditEventType.CONSENT_GRANTED if granted else AuditEventType.CONSENT_DENIED,
            capability=capability,
            action="grant" if granted else "deny",
            details={"scope": scope, **kwargs},
        )
    
    def log_tool(self, tool: str, success: bool, duration_ms: float, **kwargs) -> None:
        """Log tool execution."""
        self.log(
            event_type=AuditEventType.TOOL_EXECUTED if success else AuditEventType.TOOL_FAILED,
            resource=tool,
            action="execute",
            result="success" if success else "failure",
            details={"duration_ms": duration_ms, **kwargs},
        )
    
    def log_agent(self, agent: str, event: str, **kwargs) -> None:
        """Log agent lifecycle event."""
        event_map = {
            "started": AuditEventType.AGENT_STARTED,
            "completed": AuditEventType.AGENT_COMPLETED,
            "failed": AuditEventType.AGENT_FAILED,
        }
        self.log(
            event_type=event_map.get(event, AuditEventType.ERROR),
            resource=agent,
            action=event,
            details=kwargs,
        )
    
    def log_model(self, provider: str, model: str, success: bool, latency_ms: float, **kwargs) -> None:
        """Log model request."""
        self.log(
            event_type=AuditEventType.MODEL_RESPONSE if success else AuditEventType.MODEL_ERROR,
            resource=f"{provider}/{model}",
            action="complete",
            result="success" if success else "failure",
            details={"latency_ms": latency_ms, **kwargs},
        )
    
    def get_recent(self, limit: int = 100) -> list:
        """Get recent audit events."""
        if not self._log_file.exists():
            return []
        
        events = []
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception as e:
            logger.error(f"Failed to read audit log: {e}")
        
        return events[-limit:]


# Global audit logger
audit_logger = AuditLogger()