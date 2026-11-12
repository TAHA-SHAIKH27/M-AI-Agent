"""Security middleware for REST API."""
import ipaddress
import structlog
from typing import Callable, Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_413_REQUEST_ENTITY_TOO_LARGE, HTTP_429_TOO_MANY_REQUESTS

from m_core.config import settings

logger = structlog.get_logger(__name__)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Security middleware for request validation and protection."""
    
    def __init__(
        self,
        app,
        max_body_size: int = 10 * 1024 * 1024,  # 10 MB
        rate_limit_requests: int = 100,
        rate_limit_window: int = 60,  # seconds
    ):
        super().__init__(app)
        self.max_body_size = max_body_size
        self.rate_limit_requests = rate_limit_requests
        self.rate_limit_window = rate_limit_window
        self._request_counts: dict = {}
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Check content length
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.max_body_size:
            logger.warning(
                "request_too_large",
                content_length=content_length,
                max_allowed=self.max_body_size,
                path=str(request.url),
            )
            return JSONResponse(
                status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={
                    "error": "Request body too large",
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "max_size_bytes": self.max_body_size,
                },
            )
        
        # Check host binding (localhost only by default)
        if not settings.debug:
            # In production, enforce localhost binding
            client_host = request.client.host if request.client else "unknown"
            # Allow localhost and private IPs
            if not self._is_allowed_host(client_host):
                logger.warning(
                    "unauthorized_host",
                    client=client_host,
                    path=str(request.url),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Access denied",
                        "error_code": "HOST_NOT_ALLOWED",
                    },
                )
        
        # Rate limiting (enforced when rate_limit_enabled, or in production)
        is_testing = getattr(settings, "testing", False)
        rate_limit_active = getattr(settings, "rate_limit_enabled", True) and (not settings.debug or is_testing)
        
        if rate_limit_active:
            client_host = request.client.host if request.client else "unknown"
            if not self._check_rate_limit(client_host):
                logger.warning(
                    "rate_limit_exceeded",
                    client=client_host,
                    path=str(request.url),
                )
                return JSONResponse(
                    status_code=HTTP_429_TOO_MANY_REQUESTS,
                    content={
                        "error": "Rate limit exceeded",
                        "error_code": "RATE_LIMITED",
                        "retry_after_seconds": self.rate_limit_window,
                    },
                )
        
        # Add security headers
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        return response
    
    def _is_allowed_host(self, host: str) -> bool:
        """Allow loopback and RFC1918/ULA client addresses only."""
        allowed_names = {"localhost"}
        if host in allowed_names:
            return True
        try:
            address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            return False
        if address.is_loopback:
            return True
        if isinstance(address, ipaddress.IPv4Address):
            return address.is_private and not address.is_link_local
        return address.is_private and not address.is_link_local

    def _check_rate_limit(self, client_host: str) -> bool:
        """Fast in-memory rate limiting."""
        import time
        now = time.time()
        
        # Clean expired entries
        expired_keys = [k for k, v in self._request_counts.items() if now - v["window_start"] >= self.rate_limit_window]
        for k in expired_keys:
            del self._request_counts[k]
        
        if client_host not in self._request_counts:
            self._request_counts[client_host] = {
                "count": 0,
                "window_start": now,
            }
        
        entry = self._request_counts[client_host]
        if now - entry["window_start"] >= self.rate_limit_window:
            entry["count"] = 0
            entry["window_start"] = now
        
        entry["count"] += 1
        limit = getattr(settings, "rate_limit_requests", self.rate_limit_requests)
        return entry["count"] <= limit