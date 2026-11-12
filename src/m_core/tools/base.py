"""Base tool class for M AI Assistant."""
import asyncio
import time
import uuid
import structlog
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List, Type
from dataclasses import dataclass

from .schemas import (
    ToolManifest,
    ToolInvocation,
    ToolResult,
    ToolExecutionContext,
    ToolRiskLevel,
    ToolCategory,
)
from ..core.exceptions import ToolExecutionError, ToolNotFoundError, ToolValidationError, ConsentRequiredError
from ..security import consent_manager, audit_logger, AuditEventType
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ToolExecutionMetrics:
    """Metrics for tool execution."""
    start_time: float = 0.0
    end_time: float = 0.0
    execution_time_ms: float = 0.0
    retry_count: int = 0
    success: bool = False
    error: Optional[str] = None


class BaseTool(ABC):
    """Abstract base class for all tools."""
    
    # Class attributes to be overridden by subclasses
    manifest: Any  # ToolManifest or dict
    
    def __init__(self):
        # Get the class manifest (defined as class attribute)
        class_manifest = getattr(self.__class__, 'manifest', None)
        if not class_manifest:
            raise NotImplementedError("Subclasses must define a manifest")
        
        # Convert dict manifest to object with attribute access
        self._manifest = self._normalize_manifest(class_manifest)
        
        # Validate manifest
        self._validate_manifest()
        self._metrics = ToolExecutionMetrics()
    
    def _normalize_manifest(self, manifest: Any) -> Any:
        """Convert dict manifest to object with attribute access."""
        if isinstance(manifest, dict):
            # Create a simple object with attribute access
            class ManifestObj:
                def __init__(self, data):
                    for k, v in data.items():
                        setattr(self, k, v)
            return ManifestObj(manifest)
        return manifest
    
    def _validate_manifest(self) -> None:
        """Validate the tool manifest."""
        if not self._manifest.name:
            raise ValueError("Tool manifest must have a name")
        if not self._manifest.input_schema:
            raise ValueError("Tool manifest must have an input_schema")
        if not self._manifest.output_schema:
            raise ValueError("Tool manifest must have an output_schema")
        if self._manifest.timeout_seconds <= 0:
            raise ValueError("Tool manifest timeout_seconds must be positive")
        if self._manifest.max_retries < 0:
            raise ValueError("Tool manifest max_retries must be non-negative")
    
    @property
    def manifest(self) -> Any:
        return self._manifest
    
    @property
    def name(self) -> str:
        """Tool name."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('name', '')
        return getattr(m, 'name', '')
    
    @property
    def display_name(self) -> str:
        """Display name."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('display_name', '')
        return getattr(m, 'display_name', '')
    
    @property
    def risk_level(self) -> ToolRiskLevel:
        """Risk level."""
        m = self.manifest
        if isinstance(m, dict):
            return ToolRiskLevel(m.get('risk_level', 'low'))
        return getattr(m, 'risk_level', ToolRiskLevel.LOW)
    
    @property
    def requires_approval(self) -> bool:
        """Whether this tool requires explicit user approval."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('requires_approval', False)
        return getattr(m, 'requires_approval', False)
    
    def get_required_permissions(self) -> List[str]:
        """Get required permissions."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('required_permissions', [])
        return getattr(m, 'required_permissions', [])
    
    def get_optional_permissions(self) -> List[str]:
        """Get optional permissions."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('optional_permissions', [])
        return getattr(m, 'optional_permissions', [])
    
    def validate_input(self, arguments: Dict[str, Any]) -> None:
        """Validate input arguments against schema.
        
        Uses JSON Schema validation via jsonschema library.
        """
        try:
            import jsonschema
            # Get input schema from manifest (handles both dict and object)
            m = self.manifest
            if isinstance(m, dict):
                input_schema = m.get('input_schema', {})
            else:
                input_schema = getattr(m, 'input_schema', {})
            jsonschema.validate(instance=arguments, schema=input_schema)
        except jsonschema.ValidationError as e:
            raise ToolValidationError(
                f"Invalid input for tool {self.name}: {e.message}",
                tool=self.name,
                details={"path": list(e.path), "schema_path": list(e.schema_path)}
            )
        except ImportError:
            # jsonschema not available, skip validation
            logger.warning("jsonschema not available, skipping input validation")
    
    def validate_output(self, result: Any) -> None:
        """Validate output against schema."""
        try:
            import jsonschema
            m = self.manifest
            if isinstance(m, dict):
                output_schema = m.get('output_schema', {})
            else:
                output_schema = getattr(m, 'output_schema', {})
            jsonschema.validate(instance=result, schema=output_schema)
        except jsonschema.ValidationError as e:
            raise ToolExecutionError(
                f"Invalid output from tool {self.name}: {e.message}",
                tool=self.name,
                details={"path": list(e.path), "schema_path": list(e.schema_path)}
            )
        except ImportError:
            logger.warning("jsonschema not available, skipping output validation")
    
    def get_max_retries(self) -> int:
        """Get max retries from manifest."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('max_retries', 1)
        return getattr(m, 'max_retries', 1)
    
    def get_timeout_seconds(self) -> int:
        """Get timeout seconds from manifest."""
        m = self.manifest
        if isinstance(m, dict):
            return m.get('timeout_seconds', 30)
        return getattr(m, 'timeout_seconds', 30)
    
    def check_consent(self, context: ToolExecutionContext) -> None:
        """Check if required consents are granted."""
        for perm in self.get_required_permissions():
            if not context.consent_manager.is_granted(perm):
                raise ConsentRequiredError(
                    f"Consent required for capability: {perm}",
                    capability=perm
                )

    async def execute(self, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool with full lifecycle management.
        
        This is the main entry point that handles:
        - Consent checking
        - Input validation
        - Approval handling
        - Execution with timeout
        - Retry logic
        - Output validation
        - Audit logging
        - Metrics collection
        """
        self._metrics.start_time = time.time()
        correlation_id = context.correlation_id or str(uuid.uuid4())
        
        try:
            # Check consent
            self.check_consent(context)
            
            # Validate input
            self.validate_input(context.arguments)
            
            # Check if approval is required
            if self.requires_approval and not context.arguments.get("approval_token"):
                # Check if approval was already granted in this session
                if not context.arguments.get("approved"):
                    raise ToolExecutionError(
                        f"Tool {self.name} requires explicit user approval",
                        tool=self.name,
                        code="APPROVAL_REQUIRED",
                        details={"requires_approval": True, "risk_level": self.risk_level.value}
                    )
            
            # Execute with timeout and retries
            result = await self._execute_with_retry(context)
            
            # Validate output
            if result.success and result.result is not None:
                self.validate_output(result.result)
            
            # Add correlation info
            final_result = ToolResult(
                success=result.success,
                result=result.result,
                error=result.error,
                error_code=result.error_code,
                metadata=result.metadata,
                execution_time_ms=result.execution_time_ms,
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            
        except ConsentRequiredError as e:
            final_result = ToolResult(
                success=False,
                error=str(e),
                error_code="CONSENT_REQUIRED",
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            logger.warning(f"Consent required for {self.name}: {e}")
            
        except ToolValidationError as e:
            final_result = ToolResult(
                success=False,
                error=str(e),
                error_code="VALIDATION_ERROR",
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            logger.warning(f"Validation error for {self.name}: {e}")
            
        except ToolExecutionError as e:
            final_result = ToolResult(
                success=False,
                error=str(e),
                error_code=e.code or "EXECUTION_ERROR",
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            logger.error(f"Execution error for {self.name}: {e}")
            
        except asyncio.TimeoutError:
            final_result = ToolResult(
                success=False,
                error=f"Tool execution timed out after {context.timeout_seconds}s",
                error_code="TIMEOUT",
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            logger.error(f"Timeout executing {self.name}")
            
        except Exception as e:
            final_result = ToolResult(
                success=False,
                error=f"Unexpected error: {str(e)}",
                error_code="UNEXPECTED_ERROR",
                tool_name=self.name,
                correlation_id=context.correlation_id,
            )
            logger.exception(f"Unexpected error executing {self.name}")
        
        finally:
            self._metrics.end_time = time.time()
            self._metrics.execution_time_ms = (self._metrics.end_time - self._metrics.start_time) * 1000
            self._metrics.success = final_result.success
            
            # Add metrics to result metadata
            final_result = ToolResult(
                success=final_result.success,
                result=final_result.result,
                error=final_result.error,
                error_code=final_result.error_code,
                metadata={
                    **final_result.metadata,
                    "metrics": {
                        "execution_time_ms": self._metrics.execution_time_ms,
                        "retry_count": self._metrics.retry_count,
                    }
                },
                execution_time_ms=self._metrics.execution_time_ms,
                tool_name=self.name,
                correlation_id=final_result.correlation_id,
            )
            
            # Audit log
            if context.audit_logger:
                audit_logger.log(
                    event_type=AuditEventType.TOOL_EXECUTED if final_result.success else AuditEventType.TOOL_FAILED,
                    capability=f"tool:{self.name}",
                    resource=self.name,
                    action="execute",
                    result="success" if final_result.success else "failure",
                    details={
                        "tool": self.name,
                        "correlation_id": correlation_id,
                        "execution_time_ms": self._metrics.execution_time_ms,
                        "retry_count": self._metrics.retry_count,
                        "error": final_result.error,
                    },
                    correlation_id=correlation_id,
                )
        
        return final_result
    
    async def _execute_with_retry(self, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool with retry logic."""
        last_error = None
        
        max_retries = self.get_max_retries()
        for attempt in range(max_retries + 1):
            self._metrics.retry_count = attempt
            
            try:
                # Apply timeout
                manifest_timeout = max(1, int(self.get_timeout_seconds()))
                requested_timeout = max(1, int(context.timeout_seconds))
                timeout = min(manifest_timeout, requested_timeout)
                raw_result = await asyncio.wait_for(
                    self._execute_impl(ToolExecutionContext(
                        tool_name=context.tool_name,
                        arguments=context.arguments,
                        correlation_id=context.correlation_id,
                        user_id=context.user_id,
                        permissions_granted=context.permissions_granted,
                        audit_logger=context.audit_logger,
                        consent_manager=context.consent_manager,
                        timeout_seconds=timeout,
                        memory_manager=context.memory_manager,
                        model_provider_factory=context.model_provider_factory,
                    )),
                    timeout=timeout
                )
                
                # Convert dict result to ToolResult if needed
                if isinstance(raw_result, dict):
                    return ToolResult(
                        success=raw_result.get("success", False),
                        result=raw_result.get("result"),
                        error=raw_result.get("error"),
                        error_code=raw_result.get("error_code"),
                        tool_name=self.name,
                    )
                return raw_result
                
            except asyncio.TimeoutError:
                last_error = f"Execution timed out after {context.timeout_seconds}s"
                if attempt < self.get_max_retries():
                    logger.warning(f"Attempt {attempt + 1} timed out, retrying...")
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    raise asyncio.TimeoutError(
                        f"Tool timed out after {self.get_max_retries() + 1} attempts"
                    )
                    
            except Exception as e:
                last_error = str(e)
                if attempt < self.get_max_retries():
                    logger.warning(f"Attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise
        
        # All retries exhausted
        raise ToolExecutionError(
            f"Tool failed after {self.get_max_retries() + 1} attempts: {last_error}",
            tool=self.name,
            error_code="MAX_RETRIES_EXCEEDED"
        )
    
    @abstractmethod
    async def _execute_impl(self, context: ToolExecutionContext) -> ToolResult:
        """Actual tool implementation. Must be implemented by subclasses."""
        pass
    
    def get_manifest(self) -> ToolManifest:
        """Get tool manifest."""
        return self.manifest
    
    def get_metrics(self) -> ToolExecutionMetrics:
        """Get execution metrics."""
        return self._metrics