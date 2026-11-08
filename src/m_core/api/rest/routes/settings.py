"""Settings routes for REST API."""
import structlog
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any

from m_core.config import settings
from m_core.model_providers import provider_factory

from ..schemas import SafeSettingsResponse, ErrorResponse
from ..dependencies import get_client_info, validate_localhost, APIClientInfo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get(
    "",
    response_model=SafeSettingsResponse,
    summary="Get safe configuration settings",
    description="Get non-sensitive configuration information for the frontend",
)
async def get_settings(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> SafeSettingsResponse:
    """Get safe configuration settings (no secrets)."""
    logger.info("settings_request", client=client.host)
    
    try:
        # Get available providers
        providers = provider_factory.get_all_providers()
        available_providers = [p.id for p in providers]
        
        return SafeSettingsResponse(
            app_name=settings.app_name,
            app_version=settings.app_version,
            debug=settings.debug,
            api_host=settings.api_host,
            api_port=settings.api_port,
            nvidia_model=settings.nvidia_model,
            ollama_model=settings.ollama_model,
            openai_model=settings.openai_model,
            anthropic_model=settings.anthropic_model,
            available_providers=available_providers,
        )
        
    except Exception as e:
        logger.error("settings_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get settings",
                "error_code": "SETTINGS_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/providers",
    summary="Get provider information",
    description="Get information about configured model providers",
)
async def get_providers(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> Dict[str, Any]:
    """Get provider information."""
    try:
        providers = provider_factory.get_all_providers()
        
        provider_info = []
        for p in providers:
            info = {
                "id": p.id,
                "name": p.name,
                "type": p.type,
                "is_primary": p == provider_factory.get_primary(),
                "capabilities": {
                    "chat": p.capabilities.chat,
                    "streaming": p.capabilities.streaming,
                    "function_calling": p.capabilities.function_calling,
                    "vision": p.capabilities.vision,
                    "reasoning": p.capabilities.reasoning,
                    "max_context_tokens": p.capabilities.max_context_tokens,
                },
                "default_model": p.get_default_model() if hasattr(p, 'get_default_model') else "unknown",
            }
            provider_info.append(info)
        
        return {
            "providers": provider_info,
            "primary": provider_factory.get_primary().id if provider_factory.get_primary() else None,
        }
        
    except Exception as e:
        logger.error("providers_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get providers",
                "error_code": "PROVIDERS_FAILED",
                "details": {"message": str(e)},
            },
        )


@router.get(
    "/health",
    summary="Get provider health",
    description="Check health of all model providers",
)
async def get_provider_health(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> Dict[str, Any]:
    """Get provider health status."""
    try:
        health_results = await provider_factory.health_check_all()
        
        return {
            "providers": health_results,
            "primary_healthy": False,
        }
        
    except Exception as e:
        logger.error("provider_health_failed", error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Failed to get provider health",
                "error_code": "PROVIDER_HEALTH_FAILED",
                "details": {"message": str(e)},
            },
        )