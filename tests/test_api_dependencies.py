"""Regression tests for API client network allow-list validation."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from m_core.api.rest import dependencies


@pytest.mark.asyncio
async def test_private_172_range_is_allowed(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "debug", False)
    request = SimpleNamespace(client=SimpleNamespace(host="172.16.0.10"))
    await dependencies.validate_localhost(request)


@pytest.mark.asyncio
async def test_public_172_address_is_rejected(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "debug", False)
    request = SimpleNamespace(client=SimpleNamespace(host="172.32.0.10"))
    with pytest.raises(HTTPException) as exc:
        await dependencies.validate_localhost(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_other_private_ranges_are_allowed(monkeypatch):
    monkeypatch.setattr(dependencies.settings, "debug", False)
    for host in ("10.1.2.3", "192.168.1.20", "127.0.0.1", "::1"):
        request = SimpleNamespace(client=SimpleNamespace(host=host))
        await dependencies.validate_localhost(request)
