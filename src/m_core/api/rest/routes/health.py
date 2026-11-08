"""Health check routes."""
import time
import structlog
from fastapi import APIRouter, Depends
from typing import Dict, Any

from m_core.config import settings
from m_core.model_providers import provider_factory
from m_core.memory import memory_manager
from m_core.orchestration import orchestrator
from m_core.tools import tool_registry
from m_core.agents import agent_registry
from ..schemas import HealthResponse, HealthStatus
from ..dependencies import get_client_info, validate_localhost, APIClientInfo

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

# Track startup time
_start_time = time.time()


@router.get(
    "",
    response_model=HealthResponse,
    summary="Health check",
    description="Check backend health and readiness",
)
async def health_check(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> HealthResponse:
    """Comprehensive health check endpoint."""
    components = {}
    overall_healthy = True
    
    # Check model providers
    try:
        provider_health = await provider_factory.health_check_all()
        providers_info = {}
        for pid, health in provider_health.items():
            is_healthy = health.get("healthy", False) if isinstance(health, dict) else getattr(health, "healthy", False)
            providers_info[pid] = {
                "healthy": is_healthy,
                "latency_ms": health.get("latency_ms", 0.0) if isinstance(health, dict) else getattr(health, "latency_ms", 0.0),
                "error": health.get("error") if isinstance(health, dict) else getattr(health, "error", None),
                "model_count": health.get("model_count", 0) if isinstance(health, dict) else getattr(health, "model_count", 0),
            }
            if not is_healthy:
                overall_healthy = False
        components["providers"] = providers_info
    except Exception as e:
        logger.error("health_check_providers_failed", error=str(e))
        components["providers"] = {"error": str(e)}
        overall_healthy = False
    
    # Check memory systems
    try:
        memory_stats = await memory_manager.get_stats()
        components["memory"] = memory_stats
        # Check if any memory tier is unhealthy
        for tier, stats in memory_stats.items():
            if tier != "config" and isinstance(stats, dict):
                if stats.get("status") == "error":
                    overall_healthy = False
    except Exception as e:
        logger.error("health_check_memory_failed", error=str(e))
        components["memory"] = {"error": str(e)}
        overall_healthy = False
    
    # Check orchestrator
    try:
        active_tasks = len(orchestrator._active_tasks)
        components["orchestrator"] = {
            "active_tasks": active_tasks,
            "initialized": orchestrator._initialized,
        }
    except Exception as e:
        logger.error("health_check_orchestrator_failed", error=str(e))
        components["orchestrator"] = {"error": str(e)}
        overall_healthy = False
    
    # Check tool registry
    try:
        tools = tool_registry.list_tools()
        components["tools"] = {
            "total": len(tools),
            "enabled": len([t for t in tools if t.get("enabled", True)]),
        }
    except Exception as e:
        logger.error("health_check_tools_failed", error=str(e))
        components["tools"] = {"error": str(e)}
    
    # Check agent registry
    try:
        agents = agent_registry.list_agents()
        components["agents"] = {
            "total": len(agents),
            "enabled": len([a for a in agents if a.get("enabled", True)]),
        }
    except Exception as e:
        logger.error("health_check_agents_failed", error=str(e))
        components["agents"] = {"error": str(e)}
    
    # Determine overall status
    if overall_healthy:
        status = HealthStatus.HEALTHY
    else:
        status = HealthStatus.DEGRADED
    
    uptime = time.time() - _start_time
    
    return HealthResponse(
        status=status,
        timestamp=int(time.time() * 1000),
        version=settings.app_version,
        components=components,
        uptime_seconds=uptime,
    )


@router.get(
    "/ready",
    summary="Readiness check",
    description="Check if backend is ready to serve requests",
)
async def readiness_check(
    client: APIClientInfo = Depends(get_client_info),
    _: None = Depends(validate_localhost),
) -> Dict[str, Any]:
    """Kubernetes-style readiness probe."""
    # Check if primary provider is available
    primary = provider_factory.get_primary()
    primary_healthy = False
    if primary:
        try:
            health = await primary.health_check_cached()
            primary_healthy = health.healthy
        except Exception:
            pass
    
    # Check if memory is initialized
    memory_ready = False
    try:
        stats = await memory_manager.get_stats()
        memory_ready = "episodic" in stats
    except Exception:
        pass
    
    ready = primary_healthy and memory_ready and orchestrator._initialized
    
    return {
        "ready": ready,
        "primary_provider_healthy": primary_healthy,
        "memory_ready": memory_ready,
        "orchestrator_initialized": orchestrator._initialized,
    }


@router.get(
    "/live",
    summary="Liveness check",
    description="Check if backend process is alive",
)
async def liveness_check(
    client: APIClientInfo = Depends(get_client_info),
) -> Dict[str, Any]:
    """Kubernetes-style liveness probe."""
    return {
        "alive": True,
        "timestamp": int(time.time() * 1000),
    }