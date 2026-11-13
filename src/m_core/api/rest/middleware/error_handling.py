"""Error handling middleware for REST API."""
import traceback
import structlog
from typing import Callable, Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR, HTTP_400_BAD_REQUEST
from fastapi import HTTPException

from ..schemas import ErrorResponse
from m_core.core.exceptions import (
    MError, ConfigurationError, ProviderError, PermissionError,
    ToolExecutionError, ValidationError, ToolNotFoundError,
    AgentNotFoundError, ConsentRequiredError,
)

logger = structlog.get_logger(__name__)


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Middleware for handling exceptions and returning consistent error responses."""
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
            
        except HTTPException:
            # Re-raise HTTPException to let FastAPI's exception handler handle it
            raise
            
        except MError as e:
            # Known M errors
            logger.warning(
                "m_error",
                error_type=type(e).__name__,
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code=type(e).__name__,
                details=getattr(e, "details", None),
            )
            
        except ConsentRequiredError as e:
            # Consent required - special handling
            logger.warning(
                "consent_required",
                capability=e.capability,
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=f"Consent required: {e.capability}",
                error_code="CONSENT_REQUIRED",
                details={"capability": e.capability},
            )
            
        except PermissionError as e:
            logger.warning(
                "permission_error",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code="PERMISSION_DENIED",
            )
            
        except ProviderError as e:
            logger.error(
                "provider_error",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                error="Model provider error",
                error_code="PROVIDER_ERROR",
                details={"message": str(e)},
            )
            
        except ToolExecutionError as e:
            logger.error(
                "tool_execution_error",
                tool=e.tool_name if hasattr(e, "tool_name") else "unknown",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                error="Tool execution failed",
                error_code="TOOL_EXECUTION_ERROR",
                details={"message": str(e)},
            )
            
        except ToolNotFoundError as e:
            logger.warning(
                "tool_not_found",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code="TOOL_NOT_FOUND",
            )
            
        except AgentNotFoundError as e:
            logger.warning(
                "agent_not_found",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code="AGENT_NOT_FOUND",
            )
            
        except ValidationError as e:
            logger.warning(
                "validation_error",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code="VALIDATION_ERROR",
            )
            
        except ValueError as e:
            logger.warning(
                "value_error",
                message=str(e),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_400_BAD_REQUEST,
                error=str(e),
                error_code="INVALID_INPUT",
            )
            
        except Exception as e:
            # Unexpected errors - log full traceback
            logger.error(
                "unhandled_exception",
                error_type=type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
                path=str(request.url),
            )
            return self._error_response(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                error="Internal server error",
                error_code="INTERNAL_ERROR",
                details={"type": type(e).__name__} if not logger._config.get("debug") else None,
            )
    
    def _error_response(
        self,
        status_code: int,
        error: str,
        error_code: str,
        details: Optional[dict] = None,
    ) -> JSONResponse:
        """Create standardized error response."""
        from datetime import datetime
        return JSONResponse(
            status_code=status_code,
            content=ErrorResponse(
                error=error,
                error_code=error_code,
                details=details,
                timestamp=int(datetime.now().timestamp() * 1000),
            ).to_dict() if hasattr(ErrorResponse, 'to_dict') else {
                "error": error,
                "error_code": error_code,
                "details": details,
                "timestamp": int(datetime.now().timestamp() * 1000),
            },
        )