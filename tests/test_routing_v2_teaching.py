from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_loader import load_config
from app.routing.models import RoutingArticle
from app.routing_v2.teaching import (
    RoutingTeachError,
    apply_duplicate_teaching_rule,
    apply_duplicate_source_url_teaching_rule,
    apply_source_url_teaching_rule,
    apply_teaching_rule,
    latest_backup_path,
    latest_source_backup_path,
    make_source_url_teaching_rule,
    make_teaching_rule,
    preview_duplicate_source_url_teaching_rule,
    parse_score_string,
    preview_source_url_teaching_rule,
    preview_duplicate_teaching_rule,
    preview_teaching_rule,
    route_score_suggestions,
    restore_latest_backup,
)


def write_weighted_config(tmp_path: Path):
    routing_dir = tmp_path / "routing_v2"
    routing_dir.mkdir()
    (routing_dir / "routes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "primary_threshold": 25,
                    "review_threshold": 20,
                    "noise_threshold": 35,
                    "route_aliases": {
                        "US Politics": "the-hill",
                        "Us-politics": "the-hill",
                        "The Hill": "the-hill"
                    },
                    "field_multipliers": {"title": 1.0, "summary": 0.7, "url_slug": 0.4},
                },
                "routes": [
                    {"key": "sea", "destination_class": "primary", "threshold": 25},
                    {"key": "air", "destination_class": "primary", "threshold": 25},
                    {"key": "strategic-weapons", "destination_class": "primary", "threshold": 25},
                    {"key": "the-hill", "destination_class": "primary", "threshold": 25},
                    {"key": "review", "destination_class": "review", "threshold": 20},
                    {"key": "noise", "destination_class": "pseudo", "pseudo": True, "threshold": 35},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (routing_dir / "evidence.json").write_text(
        json.dumps(
            {
                "version": 1,
                "evidence": [
                    {
                        "id": "submarine",
                        "type": "literal",
                        "phrase": "submarine",
                        "scores": {"sea": 35},
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (routing_dir / "sources.json").write_text('{"version": 1, "sources": []}', encoding="utf-8")
    (routing_dir / "mirrors.json").write_text('{"version": 1, "mirrors": []}', encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "routing": {
                        "enabled": True,
                        "mode": "enforced",
                        "engine": "weighted_v2",
                        "weightedConfigDir": str(routing_dir),
                    }
                },
                "channels": [
                    {"key": "sea", "name": "Sea", "discordChannelId": "111111111111111111"},
                    {"key": "air", "name": "Air", "discordChannelId": "222222222222222222"},
                    {
                        "key": "strategic-weapons",
                        "name": "Strategic Weapons",
                        "discordChannelId": "333333333333333333",
                    },
                    {"key": "the-hill", "name": "The Hill", "discordChannelId": "555555555555555555"},
                    {"key": "review", "name": "Review", "discordChannelId": "444444444444444444"},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return load_config(config_path), routing_dir


def test_parse_score_string_validates_routes_and_duplicates(tmp_path: Path) -> None:
    config, _routing_dir = write_weighted_config(tmp_path)
    allowed = {"sea", "air", "noise"}

    assert parse_score_string("sea:+35, air:-5", allowed) == {"sea": 35, "air": -5}
    assert parse_score_string("Sea:+35, AIR:-5", allowed) == {"sea": 35, "air": -5}
    assert parse_score_string("Strategic Weapons:+55", {"strategic-weapons"}) == {"strategic-weapons": 55}
    assert parse_score_string("Us-politics:+50", {"the-hill"}, {"us-politics": "the-hill"}) == {"the-hill": 50}

    with pytest.raises(RoutingTeachError):
        parse_score_string("sea:+35, sea:+5", allowed)
    with pytest.raises(RoutingTeachError):
        parse_score_string("unknown:+35", allowed)
    with pytest.raises(RoutingTeachError):
        make_teaching_rule(term="nuclear deterrence", scores="unknown:+55", app_config=config)


def test_make_teaching_rule_uses_route_aliases_from_routes_json(tmp_path: Path) -> None:
    config, _routing_dir = write_weighted_config(tmp_path)

    rule = make_teaching_rule(term="Navalny", scores="Us-politics:+50", app_config=config)

    assert rule.scores == {"the-hill": 50}


def test_route_score_suggestions_include_aliases(tmp_path: Path) -> None:
    config, _routing_dir = write_weighted_config(tmp_path)

    suggestions = route_score_suggestions(config, "US")

    assert ("US Politics -> the-hill", "the-hill:+50") in suggestions


def test_preview_and_apply_teaching_rule_updates_evidence_and_backup(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    article = RoutingArticle(title="Pentagon reviews nuclear deterrence posture")
    rule = make_teaching_rule(
        term="nuclear deterrence",
        scores="strategic-weapons:+55, air:+6",
        app_config=config,
        notes="test rule",
    )

    preview = preview_teaching_rule(article, config, rule)
    assert preview.before.final_channel_keys == ("review",)
    assert preview.after.primary_channel_keys == ("strategic-weapons",)

    result = apply_teaching_rule(article, config, rule)
    assert result.after.primary_channel_keys == ("strategic-weapons",)
    assert latest_backup_path(routing_dir).exists()
    evidence = json.loads((routing_dir / "evidence.json").read_text(encoding="utf-8"))
    added = evidence["evidence"][-1]
    assert added["id"] == rule.id
    assert added["phrase"] == "nuclear deterrence"
    assert added["scores"] == {"air": 6, "strategic-weapons": 55}

    restore_latest_backup(routing_dir)
    restored = json.loads((routing_dir / "evidence.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in restored["evidence"]] == ["submarine"]
    assert not latest_backup_path(routing_dir).exists()


def test_regex_teaching_rule_is_validated_before_write(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    original = (routing_dir / "evidence.json").read_text(encoding="utf-8")

    with pytest.raises(RoutingTeachError):
        make_teaching_rule(
            term="(a+)+",
            scores="noise:+45",
            app_config=config,
            rule_type="regex",
        )

    assert (routing_dir / "evidence.json").read_text(encoding="utf-8") == original


def test_duplicate_literal_rule_previews_merge_and_replace(tmp_path: Path) -> None:
    config, _routing_dir = write_weighted_config(tmp_path)
    article = RoutingArticle(title="Navy submarine joins air defense exercise")
    rule = make_teaching_rule(
        term="Submarine",
        scores="sea:+45, air:+5",
        app_config=config,
        fields="title,summary",
        notes="stronger duplicate",
    )

    duplicate = preview_duplicate_teaching_rule(article, config, rule)

    assert duplicate is not None
    assert duplicate.duplicate.rule_id == "submarine"
    assert duplicate.duplicate.match_type == "literal_phrase"
    assert duplicate.current_json["scores"] == {"sea": 35}
    assert duplicate.merge_json["scores"] == {"air": 5, "sea": 45}
    assert duplicate.merge_json["fields"] == ["title", "summary"]
    assert duplicate.replace_json["phrase"] == "Submarine"
    assert duplicate.replace_json["scores"] == {"air": 5, "sea": 45}


def test_duplicate_merge_updates_existing_rule_in_place(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    article = RoutingArticle(title="Submarine air defense exercise")
    rule = make_teaching_rule(term="submarine", scores="sea:+45, air:+5", app_config=config)

    result = apply_duplicate_teaching_rule(article, config, rule, "merge")

    assert result.rule.id == "submarine"
    assert result.rule.scores == {"air": 5, "sea": 45}
    evidence = json.loads((routing_dir / "evidence.json").read_text(encoding="utf-8"))
    assert len(evidence["evidence"]) == 1
    assert evidence["evidence"][0]["id"] == "submarine"
    assert evidence["evidence"][0]["scores"] == {"air": 5, "sea": 45}


def test_duplicate_replace_updates_existing_rule_without_appending(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    article = RoutingArticle(title="Submarine air defense exercise")
    rule = make_teaching_rule(
        term="submarine",
        scores="air:+30",
        app_config=config,
        fields="title",
        notes="replace test",
    )

    result = apply_duplicate_teaching_rule(article, config, rule, "replace")

    assert result.rule.id == "submarine"
    assert result.rule.scores == {"air": 30}
    evidence = json.loads((routing_dir / "evidence.json").read_text(encoding="utf-8"))
    assert len(evidence["evidence"]) == 1
    assert evidence["evidence"][0]["scores"] == {"air": 30}
    assert evidence["evidence"][0]["fields"] == ["title"]
    assert evidence["evidence"][0]["notes"] == "replace test"


def test_source_url_teaching_rule_updates_sources_and_backup(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    article = RoutingArticle(
        title="Submarine modernization update",
        source_url="https://news.navy.mil/rss.xml",
    )
    rule = make_source_url_teaching_rule(
        scores="sea:+10, air:-5",
        app_config=config,
        source_url=article.source_url,
        notes="configured Navy feed",
    )

    preview = preview_source_url_teaching_rule(article, config, rule)
    result = apply_source_url_teaching_rule(article, config, rule)

    assert preview.after.channel_scores[0].score >= preview.before.channel_scores[0].score
    assert latest_source_backup_path(routing_dir).exists()
    sources = json.loads((routing_dir / "sources.json").read_text(encoding="utf-8"))
    added = sources["sources"][-1]
    assert added["source_url_hosts"] == ["news.navy.mil"]
    assert added["scores"] == {"air": -5, "sea": 10}
    assert result.rule.rule_type == "source_url"

    restore_latest_backup(routing_dir)
    restored = json.loads((routing_dir / "sources.json").read_text(encoding="utf-8"))
    assert restored["sources"] == []


def test_source_url_teaching_validates_regex_before_write(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    original = (routing_dir / "sources.json").read_text(encoding="utf-8")

    with pytest.raises(RoutingTeachError):
        make_source_url_teaching_rule(
            scores="sea:+10",
            app_config=config,
            host="example.com",
            path_regex="(a+)+",
        )

    assert (routing_dir / "sources.json").read_text(encoding="utf-8") == original


def test_duplicate_source_url_rule_previews_and_merges(tmp_path: Path) -> None:
    config, routing_dir = write_weighted_config(tmp_path)
    (routing_dir / "sources.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {
                        "id": "source-url-news-navy",
                        "source_url_hosts": ["news.navy.mil"],
                        "scores": {"sea": 5},
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    article = RoutingArticle(title="Submarine update", source_url="https://news.navy.mil/rss.xml")
    rule = make_source_url_teaching_rule(scores="sea:+10, air:+5", app_config=config, host="news.navy.mil")

    duplicate = preview_duplicate_source_url_teaching_rule(article, config, rule)
    result = apply_duplicate_source_url_teaching_rule(article, config, rule, "merge")

    assert duplicate is not None
    assert duplicate.duplicate.rule_id == "source-url-news-navy"
    assert duplicate.merge_json["scores"] == {"air": 5, "sea": 10}
    assert result.rule.id == "source-url-news-navy"
    sources = json.loads((routing_dir / "sources.json").read_text(encoding="utf-8"))
    assert len(sources["sources"]) == 1
    assert sources["sources"][0]["scores"] == {"air": 5, "sea": 10}
