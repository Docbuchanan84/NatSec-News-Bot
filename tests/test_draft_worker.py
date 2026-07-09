from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _load_worker():
    worker_path = Path(__file__).resolve().parents[1] / "ops" / "draft-worker.py"
    spec = importlib.util.spec_from_file_location("draft_worker_under_test", worker_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def worker():
    return _load_worker()


def _payload(*, profile: str = "fast", tweet_only: bool = False) -> dict:
    return {
        "request": {"profile": profile, "tweet_only": tweet_only},
        "article": {
            "id": 42,
            "title": "Navy destroyer joins Red Sea patrol",
            "url": "https://example.mil/news/story",
            "image_url": "https://example.mil/media/ship.jpg",
        },
        "related_articles": [],
        "media_options": [
            {
                "media_url": "https://example.mil/media/ship.jpg",
                "source_url": "https://example.mil/news/story",
            }
        ],
    }


def _draft() -> dict:
    return {
        "tweet": "The U.S. Navy says a destroyer joined a Red Sea air-defense patrol. - U.S. Navy",
        "evidence": [
            {
                "source": "U.S. Navy",
                "url": "https://example.mil/news/story",
                "verifies": "The ship joined the patrol.",
            }
        ],
        "media": {
            "recommended": {
                "type": "image",
                "attribution": "Image: U.S. Navy",
                "rights_label": "safe",
                "media_url": "https://example.mil/media/ship.jpg",
                "source_url": "https://example.mil/news/story",
            },
            "alt_text": "A U.S. Navy destroyer underway.",
            "other_options": [],
        },
        "notes": [],
    }


def test_profiles_are_pinned_and_fast_is_cheaper(worker) -> None:
    profiles = worker.build_profiles()

    assert profiles["fast"].model == "gpt-5.4-mini-2026-03-17"
    assert profiles["quality"].model == "gpt-5.5-2026-04-23"
    assert profiles["fast"].max_tool_calls < profiles["quality"].max_tool_calls
    assert profiles["fast"].input_usd_per_million < profiles["quality"].input_usd_per_million


def test_validate_and_render_full_draft(worker) -> None:
    draft = _draft()
    payload = _payload()

    worker.validate_draft(
        draft,
        payload,
        sources={"https://example.mil/news/story"},
        media_urls={"https://example.mil/media/ship.jpg"},
    )
    rendered = worker.render_draft(draft, tweet_only=False, metrics_line="Run: fast")

    assert rendered.startswith("Tweet:\n")
    assert "Evidence:\n- U.S. Navy:" in rendered
    assert "Media:\n- Recommended:" in rendered
    assert "Notes:\n- Run: fast" in rendered


def test_tweet_only_keeps_output_clean(worker) -> None:
    assert worker.render_draft(_draft(), tweet_only=True, metrics_line="hidden") == _draft()["tweet"]


def test_validate_rejects_unsearched_evidence_url(worker) -> None:
    draft = _draft()
    draft["evidence"][0]["url"] = "https://invented.example/story"

    with pytest.raises(ValueError, match="evidence URL"):
        worker.validate_draft(
            draft,
            _payload(),
            sources=set(),
            media_urls={"https://example.mil/media/ship.jpg"},
        )


def test_validate_rejects_unsearched_media_url(worker) -> None:
    draft = _draft()
    draft["media"]["recommended"]["media_url"] = "https://invented.example/image.jpg"

    with pytest.raises(ValueError, match="media URL"):
        worker.validate_draft(
            draft,
            _payload(),
            sources={"https://example.mil/news/story"},
            media_urls=set(),
        )


def test_cost_estimate_accounts_for_cache_output_and_search(worker) -> None:
    profile = worker.build_profiles()["fast"]
    usage = {
        "input_tokens": 10_000,
        "cached_input_tokens": 4_000,
        "output_tokens": 1_000,
        "reasoning_tokens": 500,
    }

    cost = worker.estimate_cost(profile, usage, web_search_calls=2)

    assert cost == pytest.approx(0.0293)


def test_normalize_url_drops_tracking_and_fragment(worker) -> None:
    assert (
        worker.normalize_url("HTTPS://Example.COM/story/?utm_source=x&id=2#section")
        == "https://example.com/story?id=2"
    )


def test_openai_run_uses_required_bounded_search_and_writes_safe_metrics(
    worker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    response_dump = {
        "id": "resp_test",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [{"url": "https://example.mil/news/story"}],
                    "results": [
                        {
                            "url": "https://example.mil/news/story",
                            "image_url": "https://example.mil/media/ship.jpg",
                        }
                    ],
                },
            }
        ],
        "usage": {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 300,
            "output_tokens_details": {"reasoning_tokens": 100},
        },
    }
    captured: dict = {}

    class FakeResponse:
        output_text = json.dumps(_draft())

        def model_dump(self, *, mode: str):
            assert mode == "json"
            return response_dump

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-secret")
    monkeypatch.setenv("DRAFT_BACKEND", "openai")
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("Draft safely.", encoding="utf-8")

    server = worker.DraftServer(
        ("127.0.0.1", 0),
        worker.DraftWorker,
        codex="codex",
        repo=tmp_path,
        codex_timeout=10,
        openai_timeout=15,
        quiet=True,
    )
    try:
        server.prompt_path = prompt_path
        server.metrics_path = tmp_path / "metrics.jsonl"
        result = server.run_openai(_payload())
    finally:
        server.server_close()

    assert captured["tool_choice"] == "required"
    assert captured["max_tool_calls"] == 3
    assert captured["tools"][0]["search_content_types"] == ["text", "image"]
    assert result.meta["backend"] == "openai"
    assert result.meta["web_search_calls"] == 1
    metric_text = (tmp_path / "metrics.jsonl").read_text(encoding="utf-8")
    assert "test-key-not-secret" not in metric_text
    assert "Navy destroyer" not in metric_text
    assert '"status":"ok"' in metric_text


def test_unknown_profile_fails_without_calling_api(
    worker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DRAFT_BACKEND", "openai")
    server = worker.DraftServer(
        ("127.0.0.1", 0),
        worker.DraftWorker,
        codex="codex",
        repo=tmp_path,
        codex_timeout=10,
        openai_timeout=15,
        quiet=True,
    )
    try:
        with pytest.raises(ValueError, match="unknown draft profile"):
            server.run_openai(_payload(profile="expensive"))
    finally:
        server.server_close()
