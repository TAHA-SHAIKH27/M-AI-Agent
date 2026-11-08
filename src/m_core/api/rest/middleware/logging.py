"""Request logging middleware for REST API."""
import time
import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for logging HTTP requests and responses."""
    
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        
        # Log request
        client_host = request.client.host if request.client else "unknown"
        logger.info(
            "http_request_started",
            method=request.method,
            url=str(request.url),
            client=client_host,
            headers=dict(request.headers),
        )
        
        try:
            response = await call_next(request)
            
            # Calculate duration
            duration_ms = (time.time() - start_time) * 1000
            
            # Log response
            logger.info(
                "http_request_completed",
                method=request.method,
                url=str(request.url),
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
            
            # Add timing header
            response.headers["X-Process-Time-MS"] = str(round(duration_ms, 2))
            
            return response
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                "http_request_failed",
                method=request.method,
                url=str(request.url),
                error=str(e),
                duration_ms=round(duration_ms, 2),
            )
            raise