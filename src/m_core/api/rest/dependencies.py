"""FastAPI dependencies for REST API."""
from typing import Optional
import ipaddress
from fastapi import Depends, HTTPException, Header, Request
from pydantic import BaseModel

from m_core.config import settings
from m_core.security import consent_manager
from m_core.agents import agent_registry
from m_core.orchestration import orchestrator
from m_core.memory import memory_manager
from m_core.tools import tool_registry
from m_core.model_providers import provider_factory


class APIClientInfo(BaseModel):
    """Client information from request."""
    host: str
    user_agent: Optional[str] = None
    api_version: str = "v1"


async def get_client_info(request: Request) -> APIClientInfo:
    """Extract client information from request."""
    return APIClientInfo(
        host=request.client.host if request.client else "unknown",
        user_agent=request.headers.get("user-agent"),
    )


async def validate_localhost(request: Request) -> None:
    """Validate request comes from localhost (for non-debug mode)."""
    if not settings.debug:
        client_host = request.client.host if request.client else "unknown"
        # Accept loopback and RFC1918 IPv4 addresses only. Prefix checks such
        # as startswith("172.") accidentally admit public 172.x.x.x addresses.
        try:
            address = ipaddress.ip_address(client_host.split("%", 1)[0])
        except ValueError:
            address = None
        private_networks = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        if address is not None and (
            address.is_loopback
            or any(address.version == network.version and address in network for network in private_networks)
        ):
            return
        
        if client_host not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(
                status_code=403,
                detail="Access denied: API only accessible from localhost",
            )


async def get_current_agent() -> str:
    """Get the default agent ID."""
    return "general_assistant"


async def check_consent(capability: str) -> bool:
    """Check if consent is granted for a capability."""
    return consent_manager.is_granted(capability)


def require_consent(capability: str):
    """Create a dependency that requires consent for a capability."""
    async def _check_consent() -> None:
        if not consent_manager.is_granted(capability):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": f"Consent required for capability: {capability}",
                    "error_code": "CONSENT_REQUIRED",
                    "capability": capability,
                },
            )
    return _check_consent


def get_orchestrator():
    """Get the global orchestrator instance."""
    return orchestrator


def get_memory_manager():
    """Get the global memory manager instance."""
    return memory_manager


def get_tool_registry():
    """Get the global tool registry instance."""
    return tool_registry


def get_agent_registry():
    """Get the global agent registry instance."""
    return agent_registry


def get_provider_factory():
    """Get the global provider factory instance."""
    return provider_factory


async def get_conversation_id(
    conversation_id: Optional[str] = Header(None, alias="X-Conversation-ID"),
) -> Optional[str]:
    """Extract conversation ID from header."""
    return conversation_id


async def get_task_id(
    task_id: Optional[str] = Header(None, alias="X-Task-ID"),
) -> Optional[str]:
    """Extract task ID from header."""
    return task_id