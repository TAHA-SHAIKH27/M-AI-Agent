"""Stage 1A mocked unit tests: Model Providers, Vision Scaling, and Factory lifecycle."""
import base64
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from m_core.common_schemas import ChatMessage, CompletionRequest, MessageRole, ToolFunction, ToolSchema
from m_core.config import settings
from m_core.core.exceptions import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from m_core.model_providers.factory import ProviderFactory
from m_core.model_providers.nvidia_nemotron import NVIDIANemotronProvider
from m_core.model_providers.vision_provider import (
    BaseVisionProvider,
    GeminiVisionProvider,
    NVIDIANIMVisionProvider,
    ScreenAnalysis,
    VisionAction,
    VisionConfig,
    VisionProviderFactory,
    VisionProviderType,
    close_vision_factory,
    get_vision_factory,
)


def _make_test_png_b64(width: int = 1920, height: int = 1080) -> str:
    img = Image.new("RGB", (width, height), color=(20, 40, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


class TestVisionConfigAndScaling:
    """Verify VisionConfig defaults/overrides and bbox/action coordinate scaling."""

    def test_vision_config_preserves_explicit_overrides(self):
        settings.nvidia_api_key = "nvapi-from-settings"
        settings.gemini_api_key = "gem-from-settings"
        settings.nvidia_vision_model = "nvidia/nvlm-d-72b"

        cfg = VisionConfig(
            nvidia_api_key="explicit-nv",
            nvidia_vision_model="nvidia/custom-vision",
            gemini_api_key="explicit-gem",
            gemini_vision_model="gemini-2.0-pro-exp",
        )
        assert cfg.nvidia_api_key == "explicit-nv"
        assert cfg.nvidia_vision_model == "nvidia/custom-vision"
        assert cfg.gemini_api_key == "explicit-gem"
        assert cfg.gemini_vision_model == "gemini-2.0-pro-exp"

    def test_vision_config_falls_back_to_settings_only_when_unset(self):
        settings.nvidia_api_key = "nv-fallback"
        settings.gemini_api_key = "gem-fallback"
        cfg = VisionConfig()
        assert cfg.nvidia_api_key == "nv-fallback"
        assert cfg.gemini_api_key == "gem-fallback"

    def test_normalize_bbox_scales_normalized_1000_and_clamps_bounds(self):
        provider = NVIDIANIMVisionProvider(VisionConfig(nvidia_api_key="dummy"))
        # 1000-normalized box in center-right + overflowing edge
        scaled = provider._normalize_bbox(
            {"left": 500, "top": 250, "width": 250, "height": 100},
            img_width=1920,
            img_height=1080,
            coordinate_space="normalized_1000",
        )
        assert scaled["left"] == 960
        assert scaled["top"] == 270
        assert scaled["width"] == 480
        assert scaled["height"] == 108
        assert scaled["right"] == 1440
        assert scaled["bottom"] == 378

        clamped = provider._normalize_bbox(
            {"left": -50, "top": 1200, "width": 5000, "height": 500},
            img_width=1920,
            img_height=1080,
            coordinate_space="pixel",
        )
        assert clamped["left"] == 0
        assert clamped["top"] == 1079
        assert clamped["width"] == 1920
        assert clamped["height"] == 1
        assert clamped["right"] <= 1920
        assert clamped["bottom"] <= 1080

    def test_normalize_bbox_unit_fraction_and_right_bottom_form(self):
        provider = NVIDIANIMVisionProvider(VisionConfig(nvidia_api_key="dummy"))
        unit = provider._normalize_bbox(
            {"x": 0.25, "y": 0.5, "right": 0.75, "bottom": 1.0},
            img_width=800,
            img_height=600,
        )
        assert unit == {
            "left": 200,
            "top": 300,
            "width": 400,
            "height": 300,
            "right": 600,
            "bottom": 600,
        }
        assert provider._normalize_bbox({}, 800, 600) == {}
        assert provider._normalize_bbox({"left": "bad"}, 800, 600) == {}

    def test_normalize_action_scales_normalized_1000_and_clamps(self):
        provider = NVIDIANIMVisionProvider(VisionConfig(nvidia_api_key="dummy"))
        action = provider._normalize_action(
            {
                "action": "click",
                "target": "Save",
                "coordinate_space": "normalized_1000",
                "params": {"x": 500, "y": 900},
                "reasoning": "Center-bottom button",
                "confidence": 0.9,
            },
            img_width=1000,
            img_height=2000,
        )
        assert isinstance(action, VisionAction)
        assert action.params["x"] == 500
        assert action.params["y"] == 1800


class TestVisionProvidersAndFactory:
    """Mocked transport & lifecycle tests for NVIDIA NIM Vision, Gemini Vision, and VisionProviderFactory."""

    async def test_vision_init_without_key_makes_zero_http_calls(self):
        prov = NVIDIANIMVisionProvider(VisionConfig(nvidia_api_key=""))
        await prov.initialize()
        assert prov._initialized is False
        assert await prov.list_models() == []
        health = await prov.health_check()
        assert health.healthy is False

    async def test_nvidia_nim_analyze_screen_reasoning_content_and_find_elements(self):
        b64 = _make_test_png_b64(1000, 500)

        def handler(request: httpx.Request) -> httpx.Response:
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "reasoning_content": json.dumps(
                                {
                                    "description": "Editor window visible",
                                    "coordinate_space": "normalized_1000",
                                    "elements": [
                                        {
                                            "label": "Cancel",
                                            "type": "button",
                                            "bbox": {"left": 100, "top": 100, "width": 100, "height": 50},
                                            "confidence": 0.95,
                                        },
                                        {
                                            "label": "Save Button",
                                            "description": "Primary save action",
                                            "type": "button",
                                            "bbox": {"left": 500, "top": 200, "width": 100, "height": 100},
                                            "confidence": 0.85,
                                        },
                                    ],
                                    "text_content": "Save Button Cancel",
                                    "suggested_actions": ["Click Save Button"],
                                    "app_type": "editor",
                                }
                            ),
                        }
                    }
                ]
            }
            return httpx.Response(200, json=payload)

        prov = NVIDIANIMVisionProvider(VisionConfig(nvidia_api_key="nv-dummy"))
        prov._client = httpx.AsyncClient(
            base_url="https://mock.nvidia.local/v1",
            transport=httpx.MockTransport(handler),
        )
        prov._initialized = True

        analysis = await prov.analyze_screen(b64, "Find save button")
        assert analysis.description == "Editor window visible"
        assert analysis.screenshot_dimensions == {"width": 1000, "height": 500}
        # Scaled from normalized_1000 against 1000x500 image
        assert analysis.elements[1]["bbox"] == {
            "left": 500,
            "top": 100,
            "width": 100,
            "height": 50,
            "right": 600,
            "bottom": 150,
        }

        ranked = await prov.find_elements(b64, "Save Button")
        assert ranked[0]["label"] == "Save Button"
        await prov.close()

    async def test_vision_factory_error_detection_fallback_and_close(self):
        b64 = _make_test_png_b64(400, 300)
        cfg = VisionConfig(
            primary_provider=VisionProviderType.NVIDIA_NIM,
            fallback_providers=[VisionProviderType.GEMINI],
            nvidia_api_key="nv-dummy",
            gemini_api_key="gem-dummy",
        )
        factory = VisionProviderFactory(cfg)

        nv = NVIDIANIMVisionProvider(cfg)
        nv._initialized = True
        nv.analyze_screen = AsyncMock(
            return_value=ScreenAnalysis(
                description="Analysis failed: HTTP 503",
                elements=[],
                text_content="",
                suggested_actions=[],
                app_type="unknown",
                model_type="nvidia_nim",
                screenshot_dimensions={},
            )
        )
        nv.close = AsyncMock()

        gem = GeminiVisionProvider(cfg)
        gem._initialized = True
        gem.analyze_screen = AsyncMock(
            return_value=ScreenAnalysis(
                description="Gemini fallback succeeded",
                elements=[{"label": "OK", "bbox": {"left": 10, "top": 10, "width": 50, "height": 20}}],
                text_content="OK",
                suggested_actions=[],
                app_type="dialog",
                model_type="gemini",
                screenshot_dimensions={"width": 400, "height": 300},
            )
        )
        gem.close = AsyncMock()

        factory._providers = {
            VisionProviderType.NVIDIA_NIM: nv,
            VisionProviderType.GEMINI: gem,
        }
        factory._current_provider = nv
        factory._initialized = True

        res = await factory.analyze_screen(b64, "Find OK")
        assert res.description == "Gemini fallback succeeded"
        assert factory._current_provider is gem

        await factory.close()
        nv.close.assert_awaited_once()
        gem.close.assert_awaited_once()

    async def test_global_vision_factory_lifecycle_and_close_reset(self, monkeypatch):
        await close_vision_factory()
        monkeypatch.setattr(VisionProviderFactory, "initialize", AsyncMock(return_value=None))
        f1 = await get_vision_factory(VisionConfig())
        f2 = await get_vision_factory()
        assert f1 is f2
        f1.close = AsyncMock()
        await close_vision_factory()
        f1.close.assert_awaited_once()


class TestNVIDIANemotronAndProviderFactory:
    """Mocked HTTP transport tests for NVIDIANemotronProvider and ProviderFactory."""

    async def test_nemotron_complete_stream_health_and_errors(self):
        settings.nvidia_api_key = "nv-dummy-key"

        def ok_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-ultra-550b-a55b"}]})
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                sse = (
                    'data: {"id":"c1","choices":[{"delta":{"reasoning_content":"think"},"finish_reason":null}]}\n\n'
                    'data: {"id":"c1","choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n'
                    "data: [DONE]\n\n"
                )
                return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
            return httpx.Response(
                200,
                json={
                    "id": "resp-1",
                    "model": "nvidia/nemotron-3-ultra-550b-a55b",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Done",
                                "reasoning_content": "Reasoned step",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                },
            )

        prov = NVIDIANemotronProvider()
        prov._client = httpx.AsyncClient(
            base_url="https://mock.nvidia.local/v1",
            transport=httpx.MockTransport(ok_handler),
        )

        health = await prov.health_check()
        assert health.healthy is True
        assert health.model_count == 1

        req = CompletionRequest(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            tools=[ToolSchema(function=ToolFunction(name="t", description="d", parameters={"type": "object"}))],
            tool_choice="auto",
        )
        comp = await prov.complete(req)
        assert comp.content == "Done"
        assert comp.reasoning_content == "Reasoned step"

        chunks = [c async for c in prov.stream(req)]
        assert len(chunks) == 2
        assert chunks[0].reasoning_content == "think"
        assert chunks[1].content == "hello"
        assert chunks[1].is_final is True

        with pytest.raises(NotImplementedError):
            await prov.embed(None)

        # Error mappings
        for code, exc_cls in [
            (401, ProviderAuthenticationError),
            (403, ProviderAuthenticationError),
            (400, ProviderError),
            (429, ProviderRateLimitError),
            (503, ProviderUnavailableError),
        ]:
            prov._client = httpx.AsyncClient(
                base_url="https://mock.nvidia.local/v1",
                transport=httpx.MockTransport(lambda req, c=code: httpx.Response(c, text="err")),
            )
            with pytest.raises(exc_cls):
                await prov.complete(req)

        def timeout_handler(req):
            raise httpx.ReadTimeout("timed out", request=req)

        prov._client = httpx.AsyncClient(
            base_url="https://mock.nvidia.local/v1",
            transport=httpx.MockTransport(timeout_handler),
        )
        with pytest.raises(ProviderTimeoutError):
            await prov.complete(req)
        await prov.close()

    async def test_provider_factory_async_sync_vision_and_close(self):
        pf = ProviderFactory()
        mock_vfactory = VisionProviderFactory(VisionConfig())
        mock_vfactory.initialize = AsyncMock(return_value=None)
        mock_vprov = NVIDIANIMVisionProvider(VisionConfig())
        mock_vfactory._current_provider = mock_vprov
        mock_vfactory._providers = {VisionProviderType.NVIDIA_NIM: mock_vprov}
        mock_vfactory.close = AsyncMock(return_value=None)
        pf._vision_factory = mock_vfactory

        got = await pf.get_vision_provider()
        assert got is mock_vprov
        assert pf.get_vision_provider_sync() is mock_vprov
        assert pf.get_provider(mock_vprov.provider_id) is mock_vprov

        await pf.close()
        mock_vfactory.close.assert_awaited_once()
        assert pf._vision_factory is None

