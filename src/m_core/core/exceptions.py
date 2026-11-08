"""Custom exceptions for M AI Assistant."""


class MError(Exception):
    """Base exception for M AI Assistant."""
    
    def __init__(self, message: str, code: str = None, details: dict = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details or {}
    
    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class ConfigurationError(MError):
    """Configuration related errors."""
    pass


class ProviderError(MError):
    """Model provider related errors."""
    
    def __init__(self, message: str, provider: str = None, **kwargs):
        super().__init__(message, **kwargs)
        self.provider = provider
        if provider:
            self.details["provider"] = provider


class ProviderUnavailableError(ProviderError):
    """Provider is unavailable or unhealthy."""
    pass


class ProviderAuthenticationError(ProviderError):
    """Provider authentication failed."""
    pass


class ProviderRateLimitError(ProviderError):
    """Provider rate limit exceeded."""
    pass


class ProviderTimeoutError(ProviderError):
    """Provider request timed out."""
    pass


class PermissionError(MError):
    """Permission/consent related errors."""
    
    def __init__(self, message: str, capability: str = None, **kwargs):
        super().__init__(message, **kwargs)
        self.capability = capability
        if capability:
            self.details["capability"] = capability


class ConsentRequiredError(PermissionError):
    """User consent required for capability."""
    pass


class ToolExecutionError(MError):
    """Tool execution errors."""
    
    def __init__(self, message: str, tool: str = None, **kwargs):
        super().__init__(message, **kwargs)
        self.tool = tool
        if tool:
            self.details["tool"] = tool


class ToolNotFoundError(ToolExecutionError):
    """Tool not found in registry."""
    pass


class ToolValidationError(ToolExecutionError):
    """Tool input validation failed."""
    pass


class AgentError(MError):
    """Agent related errors."""
    
    def __init__(self, message: str, agent: str = None, **kwargs):
        super().__init__(message, **kwargs)
        self.agent = agent
        if agent:
            self.details["agent"] = agent


class AgentNotFoundError(AgentError):
    """Agent not found in registry."""
    pass


class AgentExecutionError(AgentError):
    """Agent execution failed."""
    pass


class MemoryError(MError):
    """Memory system errors."""
    pass


class MemoryNotFoundError(MemoryError):
    """Memory entry not found."""
    pass


class VoiceError(MError):
    """Voice pipeline errors."""
    pass


class PhoneError(MError):
    """Phone/communication errors."""
    pass


class ValidationError(MError):
    """Input validation errors."""
    
    def __init__(self, message: str, field: str = None, value: any = None, **kwargs):
        super().__init__(message, **kwargs)
        self.field = field
        self.value = value
        if field:
            self.details["field"] = field
        if value is not None:
            self.details["value"] = str(value)