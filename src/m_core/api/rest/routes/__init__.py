"""REST API routes package."""
from .health import router as health_router
from .chat import router as chat_router
from .memory import router as memory_router
from .tools import router as tools_router
from .settings import router as settings_router
from .tasks import router as tasks_router

__all__ = [
    "health_router",
    "chat_router",
    "memory_router",
    "tools_router",
    "settings_router",
    "tasks_router",
]