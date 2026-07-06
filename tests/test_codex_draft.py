from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from app.codex_draft import build_codex_draft_payload, media_options_for_job, related_article_candidates
from app.models import PostJob


def _job(**overrides) -> PostJob:
    values = {
        "article_id": 42,
        "channel_id": "111",
        "title": "Navy destroyer joins Red Sea air defense patrol",
        "url": "https://www.dvidshub.net/image/123/navy-destroyer",
        "summary": "U.S. Navy officials said the ship joined a regional air defense patrol.",
        "image_url": "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2607/123/800w_q95.jpg",
        "image_source": "media_thumbnail",
        "source_name": "U.S. Navy DVIDS",
        "source_id": "dvids-navy",
        "source_class": "official_us_defense",
        "normalized_published_at": datetime(2026, 7, 5, tzinfo=UTC),
    }
    values.update(overrides)
    return PostJob(**values)


def test_media_options_label_dvids_media_safe_with_attribution() -> None:
    options = media_options_for_job(_job())

    assert options[0]["recommended"] is True
    assert options[0]["rights_label"] == "safe"
    assert options[0]["attribution"] == "Image: U.S. Navy DVIDS / DVIDS"
    assert "Navy destroyer" in options[0]["alt_text_seed"]


def test_build_payload_includes_request_routing_related_and_media() -> None:
    payload = build_codex_draft_payload(
        job=_job(),
        routing={"decision_status": "routed", "final_channel_keys": ["sea"]},
        related_articles=[{"id": 10, "title": "Related story"}],
        request={"source_message_url": "https://discord.com/channels/1/2/3"},
    )

    assert payload["article"]["id"] == 42
    assert payload["routing"]["final_channel_keys"] == ["sea"]
    assert payload["related_articles"][0]["id"] == 10
    assert payload["request"]["source_message_url"].endswith("/1/2/3")
    assert payload["media_options"][0]["rights_label"] == "safe"


def test_related_article_candidates_uses_title_overlap() -> None:
    target = _job(title="NATO air defense exercise begins in Poland")
    rows = [
        {"id": 99, "title": "NATO begins air defense drill in Poland", "summary": "", "normalized_published_at": "2026"},
        {"id": 100, "title": "Baseball team signs pitcher", "summary": "", "normalized_published_at": "2026"},
    ]

    assert [row["id"] for row in related_article_candidates(target, rows)] == [99]


def test_worker_prompt_invokes_skill_and_embeds_payload() -> None:
    worker_path = Path(__file__).resolve().parents[1] / "ops" / "codex-draft-worker.py"
    spec = importlib.util.spec_from_file_location("codex_draft_worker", worker_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prompt = module.build_prompt({"article": {"id": 42, "title": "Test story"}})

    assert "$natsec-x-draft" in prompt
    assert '"id": 42' in prompt
    assert "media options with attribution" in prompt
