"""Pytest configuration and fixtures for API tests."""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from m_core.api.rest.server import app
from m_core.security import consent_manager, Capability
from m_core.model_providers import provider_factory
from tests.api.mock_provider import MockProvider


# Event loop fixture removed to use pytest-asyncio's function-scoped loops


@pytest.fixture(scope="function")
async def async_client():
    """Create async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="function", autouse=True)
async def grant_consents():
    """Grant required consents for tests."""
    required = [
        Capability.MEMORY_READ,
        Capability.MEMORY_WRITE,
        Capability.FILE_READ,
        Capability.FILE_LIST,
        Capability.FILE_SEARCH,
        Capability.WEB_SEARCH,
        Capability.SYSTEM_INFO,
    ]
    for cap in required:
        consent_manager.grant(cap)
    yield
    # Note: We don't revoke after test to avoid affecting other tests


@pytest.fixture(scope="function")
async def mock_provider():
    """Replace provider factory with mock provider for testing."""
    mock = MockProvider(response_content="Hello! 2 + 2 = 4")
    provider_factory.replace_providers([mock])
    yield mock
    # Restore original providers after test
    from m_core.model_providers.factory import ProviderFactory
    original = ProviderFactory()
    provider_factory.replace_providers(original._providers)


@pytest.fixture(scope="function")
def anyio_backend():
    """Use asyncio backend for anyio."""
    return "asyncio"