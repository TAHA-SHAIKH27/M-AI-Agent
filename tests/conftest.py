"""Pytest configuration and fixtures."""
import pytest
import asyncio
from typing import AsyncGenerator
from m_core.config import settings

# Configure test mode
settings.testing = True


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_settings():
    """Mock settings for testing."""
    from m_core.config import Settings
    return Settings(
        debug=True,
        testing=True,
        nvidia_api_key="test-key",
        nvidia_base_url="https://integrate.api.nvidia.com/v1",
        nvidia_model="nvidia/nemotron-3-ultra-550b-a55b",
    )