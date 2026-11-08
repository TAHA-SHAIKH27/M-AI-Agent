"""API package for M AI Assistant."""
from .rest.server import app
from .websocket import websocket_router, connection_manager

__all__ = [
    "app",
    "websocket_router",
    "connection_manager",
]