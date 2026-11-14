"""Shared isolation fixtures for Stage 1 unit tests (zero live API, zero desktop side effects)."""
from pathlib import Path
import pytest
import httpx

from m_core.config import settings
from m_core.security import consent_manager, audit_logger


@pytest.fixture(autouse=True)
def stage1_isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate filesystem state, credentials, and network transport for Stage 1 tests."""
    isolated_data = tmp_path / "m_unit_data"
    isolated_data.mkdir(parents=True, exist_ok=True)
    (isolated_data / "logs").mkdir(exist_ok=True)
    (isolated_data / "orchestration").mkdir(exist_ok=True)

    # Save global settings & security singleton state for clean restore
    orig_data_dir = settings.data_dir
    orig_testing = settings.testing
    orig_nv_key = settings.nvidia_api_key
    orig_gem_key = settings.gemini_api_key
    orig_oai_key = settings.openai_api_key
    orig_ant_key = settings.anthropic_api_key
    orig_nv_vis_model = settings.nvidia_vision_model

    orig_consent_file = consent_manager._consent_file
    orig_consents = dict(consent_manager._consents)
    orig_audit_file = audit_logger._log_file

    # Patch environment & settings into temp dir
    monkeypatch.setenv("M_DATA_DIR", str(isolated_data))
    monkeypatch.setenv("M_MEMORY_EPISODIC_DB", f"sqlite:///{isolated_data.as_posix()}/memory/episodic.db")
    monkeypatch.setenv("M_MEMORY_SEMANTIC_PATH", str(isolated_data / "memory" / "semantic"))
    monkeypatch.setenv("M_MEMORY_PROCEDURAL_PATH", str(isolated_data / "memory" / "procedural"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "False")

    settings.data_dir = isolated_data
    settings.testing = True
    settings.nvidia_api_key = None
    settings.gemini_api_key = None
    settings.openai_api_key = None
    settings.anthropic_api_key = None

    consent_manager._consent_file = isolated_data / "consents.json"
    consent_manager._consents = {}
    audit_logger._log_file = isolated_data / "logs" / "audit.log"

    # Hard block any unmocked live network transport (httpx.MockTransport bypasses HTTPTransport)
    async def _block_async_http(self, request):
        raise RuntimeError(f"Blocked unmocked external async HTTP request to {request.url}")

    def _block_sync_http(self, request):
        raise RuntimeError(f"Blocked unmocked external sync HTTP request to {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _block_async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _block_sync_http)

    try:
        yield isolated_data
    finally:
        settings.data_dir = orig_data_dir
        settings.testing = orig_testing
        settings.nvidia_api_key = orig_nv_key
        settings.gemini_api_key = orig_gem_key
        settings.openai_api_key = orig_oai_key
        settings.anthropic_api_key = orig_ant_key
        settings.nvidia_vision_model = orig_nv_vis_model

        consent_manager._consent_file = orig_consent_file
        consent_manager._consents = orig_consents
        audit_logger._log_file = orig_audit_file

