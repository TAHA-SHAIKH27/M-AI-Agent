"""REST API server for M AI Assistant."""
import asyncio
import structlog
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from m_core.config import settings
from m_core.core.lifecycle import lifecycle, LifecyclePhase
from m_core.utils.logging import get_logger

from .routes import (
    health_router, chat_router, memory_router, tools_router, settings_router, tasks_router,
)
from .middleware import (
    RequestLoggingMiddleware, ErrorHandlingMiddleware, SecurityMiddleware,
)
from .dependencies import validate_localhost
from ..websocket import websocket_router, register_event_handlers

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    # Startup
    logger.info("api_server_starting", version=settings.app_version)
    
    # Run lifecycle startup hooks
    await lifecycle.startup()
    
    # Initialize orchestrator (which initializes scheduler, state_manager, etc.)
    from m_core.orchestration import orchestrator
    await orchestrator.initialize()
    await register_event_handlers()
    
    logger.info("api_server_started", host=settings.api_host, port=settings.api_port)
    
    yield
    
    # Shutdown
    logger.info("api_server_shutting_down")
    
    # Shutdown orchestrator, autonomous executor, desktop & provider resources
    from m_core.orchestration import scheduler, autonomous_executor
    from m_core.desktop import desktop_automation
    from m_core.model_providers import provider_factory, close_vision_factory

    await scheduler.shutdown()
    for cleanup in (
        autonomous_executor.close,
        desktop_automation.close,
        provider_factory.close,
        close_vision_factory,
    ):
        try:
            await cleanup()
        except Exception as e:
            logger.warning("api_server_cleanup_error", error=str(e))
    
    # Run lifecycle shutdown hooks
    await lifecycle.shutdown()
    
    logger.info("api_server_shutdown_complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    
    app = FastAPI(
        title="M AI Assistant API",
        description="REST API and WebSocket server for M AI Assistant backend",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    
    # Custom exception handler for HTTPException to ensure proper status codes
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": str(exc.detail) if exc.detail else "HTTP error",
                "error_code": f"HTTP_{exc.status_code}",
                "details": exc.detail if isinstance(exc.detail, dict) else None,
            },
        )
    
    # CORS middleware (restrictive by default)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"] if settings.debug else [],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time-MS"],
    )
    
    # Custom middleware (order matters - first added = outermost)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(ErrorHandlingMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    
    # Include routers
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(memory_router, prefix="/api/v1")
    app.include_router(tools_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(websocket_router, prefix="/api/v1")
    
    # Root endpoint
    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "status": "running",
            "docs": "/docs" if settings.debug else "disabled",
        }
    
    # Favicon
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return JSONResponse(content={}, status_code=204)
    
    return app


# Create the app instance
app = create_app()


# For direct execution
if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "m_core.api.rest.server:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )