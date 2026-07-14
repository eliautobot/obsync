from __future__ import annotations

import json

import httpx
import pytest

from obsync.llm import LLMAnalyzer, LLMConfig, fallback_analysis, validate_base_url


def test_fallback_analysis_uses_path_and_text() -> None:
    result = fallback_analysis("Clients/Acme/quarterly_report.txt", "Revenue increased.", ".txt")
    assert result.title == "Quarterly Report"
    assert result.category == "Acme"
    assert "txt" in result.tags
    assert result.provider == "rules"


@pytest.mark.parametrize("url", ["", "localhost:11434", "file:///tmp/model", "ftp://model"])
def test_invalid_model_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_base_url(url)


@pytest.mark.asyncio
async def test_ollama_structured_response_is_normalized(monkeypatch) -> None:
    response_data = {
        "message": {
            "content": json.dumps(
                {
                    "title": "Quarterly Plan",
                    "summary": "Planning document.",
                    "category": "Planning",
                    "document_type": "report",
                    "tags": ["Planning", "Q3"],
                    "confidence": 1.5,
                    "related_notes": ["Operations", "Invented Note"],
                }
            )
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json=response_data)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    analyzer = LLMAnalyzer(
        LLMConfig(enabled=True, provider="ollama", base_url="http://model", model="test-model")
    )
    result = await analyzer.analyze(
        source_path="plan.txt",
        text="Plan content",
        mime_type="text/plain",
        candidates=["Operations"],
    )
    assert result.provider == "ollama"
    assert result.confidence == 1.0
    assert result.tags == ["planning", "q3"]
    assert result.related_notes == ["Operations"]
    monkeypatch.setattr(httpx, "AsyncClient", original)


@pytest.mark.asyncio
async def test_model_failure_falls_back_to_rules(monkeypatch) -> None:
    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FailingClient())
    analyzer = LLMAnalyzer(
        LLMConfig(enabled=True, provider="ollama", base_url="http://offline", model="model")
    )
    result = await analyzer.analyze(
        source_path="notes.txt", text="hello", mime_type="text/plain", candidates=[]
    )
    assert result.provider == "rules"


@pytest.mark.asyncio
async def test_openai_compatible_retries_without_response_format(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if "response_format" in payload:
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Local analysis",
                                    "summary": "Summary",
                                    "category": "Notes",
                                    "document_type": "note",
                                    "tags": ["local"],
                                    "confidence": 0.8,
                                    "related_notes": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    analyzer = LLMAnalyzer(
        LLMConfig(
            enabled=True,
            provider="lmstudio",
            base_url="http://lmstudio:1234",
            model="local",
            api_key="key",
        )
    )
    result = await analyzer.analyze(
        source_path="note.txt", text="Body", mime_type="text/plain", candidates=[]
    )
    assert result.provider == "lmstudio"
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


@pytest.mark.asyncio
async def test_disabled_llm_connection_test() -> None:
    result = await LLMAnalyzer(LLMConfig()).test_connection()
    assert result["ok"] is False
    assert "disabled" in result["message"]
