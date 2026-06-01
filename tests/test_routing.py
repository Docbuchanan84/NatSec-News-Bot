from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_loader import load_config
from app.discord_bot import _format_score_line
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.models import RoutingArticle
from app.routing.taxonomy import expand_tags


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def app_config(tmp_path: Path):
    path = tmp_path / "config.json"
    channels = []
    for index, key in enumerate(
        [
            "indo-pacific",
            "sea",
            "africa",
            "middle-east",
            "air",
            "land",
            "strategic-weapons",
            "south-central-america",
            "the-hill",
        ]
    ):
        channels.append(
            {
                "key": key,
                "name": key,
                "discordChannelId": f"{index + 1}" * 18,
                "feeds": [{"name": "Feed", "url": f"https://example.com/{key}.rss"}],
            }
        )
    path.write_text(json.dumps({"version": 1, "channels": channels}), encoding="utf-8")
    return load_config(path)


def routing_dir(tmp_path: Path, *, unknown_tag: bool = False, duplicate_id: bool = False) -> Path:
    root = tmp_path / "routing"
    taxonomy = {
        "version": 1,
        "tags": {
            "world": {"parent_tags": []},
            "military": {"parent_tags": ["world"]},
            "naval": {"parent_tags": ["military"]},
            "air": {"parent_tags": ["military"]},
            "ground": {"parent_tags": ["military"]},
            "indo_pacific": {"parent_tags": ["world"]},
            "middle_east": {"parent_tags": ["world"]},
            "africa": {"parent_tags": ["world"]},
            "latin_america": {"parent_tags": ["world"]},
            "china": {"parent_tags": ["indo_pacific"]},
            "philippines": {"parent_tags": ["indo_pacific"]},
            "ghana": {"parent_tags": ["africa"]},
            "colombia": {"parent_tags": ["latin_america"]},
            "yemen": {"parent_tags": ["middle_east"]},
            "aircraft_carrier": {"parent_tags": ["naval", "military"]},
            "carrier_strike_group": {"parent_tags": ["aircraft_carrier", "naval", "military"]},
            "missile": {"parent_tags": ["military"]},
            "missile_defense": {"parent_tags": ["ground", "military"]},
            "air_defense": {"parent_tags": ["ground", "missile_defense", "military"]},
            "strategic_weapon": {"parent_tags": ["military"]},
            "nuclear_weapon": {"parent_tags": ["strategic_weapon"]},
            "icbm": {"parent_tags": ["nuclear_weapon", "missile"]},
            "arms_control": {"parent_tags": ["strategic_weapon"]},
            "government": {"parent_tags": ["world"]},
            "legislation": {"parent_tags": ["government"]},
            "election": {"parent_tags": ["government"]},
            "photo_gallery": {"parent_tags": ["world"]},
            "obituary": {"parent_tags": ["world"]},
            "skip_candidate": {"parent_tags": []},
            "review_required": {"parent_tags": []},
            "ambiguous": {"parent_tags": ["review_required"]},
        },
    }
    entries = [
        {
            "id": "chinese_aircraft_carrier",
            "aliases": ["Chinese aircraft carrier"],
            "tags": ["china", "aircraft_carrier", "naval", "indo_pacific"],
            "priority": 80,
            "score": 5,
        },
        {
            "id": "chinese_aircraft",
            "aliases": ["Chinese aircraft"],
            "tags": ["china", "air"],
            "priority": 10,
            "score": 1,
        },
        {
            "id": "chinese_carrier",
            "aliases": ["Chinese carrier"],
            "tags": ["china", "aircraft_carrier", "naval", "indo_pacific"],
            "priority": 70,
            "score": 5,
        },
        {
            "id": "liaoning",
            "aliases": ["Liaoning"],
            "tags": ["china", "aircraft_carrier", "naval", "indo_pacific"],
            "priority": 60,
            "score": 4,
        },
        {
            "id": "philippine_sea",
            "aliases": ["Philippine Sea"],
            "tags": ["philippines", "indo_pacific", "naval"],
            "priority": 60,
            "score": 4,
        },
        {
            "id": "ghana",
            "aliases": ["Ghana", "Ghanaian"],
            "tags": ["ghana", "africa"],
            "priority": 40,
            "score": 3,
        },
        {
            "id": "colombia",
            "aliases": ["Colombia", "Colombian"],
            "tags": ["colombia", "latin_america"],
            "priority": 40,
            "score": 3,
        },
        {
            "id": "legislation",
            "aliases": ["parliament passes", "lawmakers approve"],
            "tags": ["legislation", "government"],
            "priority": 40,
            "score": 3,
        },
        {
            "id": "election",
            "aliases": ["presidential runoff", "runoff"],
            "tags": ["election", "government"],
            "priority": 30,
            "score": 2,
        },
        {
            "id": "yemen",
            "aliases": ["Yemen", "Yemeni"],
            "tags": ["yemen", "middle_east"],
            "priority": 40,
            "score": 3,
        },
        {
            "id": "obituary",
            "aliases": ["dies at", "dead at"],
            "tags": ["obituary", "review_required"],
            "priority": 40,
            "score": 1,
        },
        {
            "id": "photo_gallery",
            "aliases": ["in pictures", "week around the world in pictures"],
            "tags": ["photo_gallery", "skip_candidate"],
            "priority": 90,
            "score": 0,
        },
        {
            "id": "air_defense",
            "aliases": ["Patriot battery", "Iron Dome", "air defense"],
            "tags": ["air_defense", "missile_defense", "ground", "military"],
            "priority": 60,
            "score": 4,
        },
        {
            "id": "missile",
            "aliases": ["missile"],
            "tags": ["missile", "military"],
            "priority": 20,
            "score": 2,
        },
        {
            "id": "icbm",
            "aliases": ["ICBM", "intercontinental ballistic missile"],
            "tags": ["icbm", "nuclear_weapon", "strategic_weapon", "missile", "military"],
            "priority": 70,
            "score": 5,
        },
        {
            "id": "arms_control",
            "aliases": ["New START", "nuclear arms"],
            "tags": ["arms_control", "nuclear_weapon", "strategic_weapon", "military"],
            "priority": 70,
            "score": 5,
        },
    ]
    if unknown_tag:
        entries[0]["tags"] = ["missing_tag"]
    if duplicate_id:
        entries.append({**entries[0]})
    channels = {
        "version": 1,
        "max_destinations": 2,
        "review_tags": ["review_required", "ambiguous"],
        "skip_tags": ["skip_candidate"],
        "rule_defaults": {
            "enabled": True,
            "minimum_score": 4,
            "tag_penalties": {"skip_candidate": 8},
            "content_mode_adjustments": {"title_only": -1},
        },
        "channels": [
            {
                "channel_key": "indo-pacific",
                "priority": 90,
                "tag_boosts": {"indo_pacific": 5, "china": 4, "philippines": 3},
                "required_any": ["indo_pacific"],
            },
            {
                "channel_key": "sea",
                "priority": 100,
                "tag_boosts": {"naval": 6, "aircraft_carrier": 5},
                "required_any": ["naval"],
            },
            {
                "channel_key": "africa",
                "priority": 80,
                "tag_boosts": {"africa": 5, "ghana": 5, "legislation": 2},
                "required_any": ["africa"],
            },
            {
                "channel_key": "south-central-america",
                "priority": 80,
                "tag_boosts": {"latin_america": 6, "colombia": 5, "election": 2},
                "required_any": ["latin_america", "colombia"],
            },
            {
                "channel_key": "the-hill",
                "priority": 65,
                "tag_boosts": {"government": 4, "election": 4},
                "tag_penalties": {"latin_america": 6},
                "required_any": ["government", "election"],
            },
            {
                "channel_key": "middle-east",
                "priority": 80,
                "tag_boosts": {"middle_east": 5, "yemen": 5, "obituary": 1},
                "required_any": ["middle_east"],
            },
            {
                "channel_key": "air",
                "priority": 70,
                "tag_boosts": {"air": 5},
                "tag_penalties": {"air_defense": 6, "missile_defense": 5},
                "term_penalties": {"chinese_aircraft_carrier": 5},
                "required_any": ["air"],
            },
            {
                "channel_key": "land",
                "priority": 85,
                "tag_boosts": {"ground": 6, "air_defense": 7, "missile_defense": 6},
                "required_any": ["ground", "air_defense", "missile_defense"],
            },
            {
                "channel_key": "strategic-weapons",
                "minimum_score": 6,
                "priority": 90,
                "tag_boosts": {"nuclear_weapon": 8, "icbm": 8, "strategic_weapon": 5, "arms_control": 5},
                "tag_penalties": {"air_defense": 8, "missile_defense": 8, "ground": 3},
                "required_any": ["nuclear_weapon", "icbm", "strategic_weapon", "arms_control"],
            },
        ],
    }
    write_json(root / "taxonomy.json", taxonomy)
    write_json(root / "knowledge_base.json", {"version": 1, "entries": entries})
    write_json(root / "channels.json", channels)
    return root


def engine(tmp_path: Path) -> RoutingEngine:
    return RoutingEngine(load_routing_config(routing_dir(tmp_path), app_config(tmp_path)))


def test_taxonomy_parent_expansion(tmp_path: Path) -> None:
    config = load_routing_config(routing_dir(tmp_path), app_config(tmp_path))
    assert expand_tags({"carrier_strike_group"}, config.taxonomy) >= {"aircraft_carrier", "naval", "military", "world"}


def test_unknown_tags_fail_validation(tmp_path: Path) -> None:
    with pytest.raises(RoutingConfigError) as exc:
        load_routing_config(routing_dir(tmp_path, unknown_tag=True), app_config(tmp_path))
    assert "unknown tag" in str(exc.value)


def test_duplicate_knowledge_ids_fail_validation(tmp_path: Path) -> None:
    with pytest.raises(RoutingConfigError) as exc:
        load_routing_config(routing_dir(tmp_path, duplicate_id=True), app_config(tmp_path))
    assert "duplicates another knowledge entry" in str(exc.value)


def test_alias_matching_is_case_insensitive(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="chinese carrier liaoning enters philippine sea"))
    assert "chinese_carrier" in {match.knowledge_entry_id for match in decision.matched_entries}


def test_longest_match_beats_shorter_overlap(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Chinese aircraft carrier deploys"))
    ids = {match.knowledge_entry_id for match in decision.matched_entries}
    assert "chinese_aircraft_carrier" in ids
    assert "chinese_aircraft" not in ids


def test_chinese_aircraft_carrier_does_not_route_as_generic_air(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Chinese aircraft carrier deploys"))
    assert "air" not in decision.selected_channel_keys
    assert "sea" in decision.selected_channel_keys


def test_penalties_reduce_score(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Chinese aircraft carrier in pictures"))
    sea_score = next(score for score in decision.channel_scores if score.channel_key == "sea")
    assert decision.decision_status == "skipped"
    assert sea_score.score < 4


def test_title_only_mode_is_more_conservative(tmp_path: Path) -> None:
    title_only = engine(tmp_path).route(RoutingArticle(title="Ghana parliament passes bill"))
    with_summary = engine(tmp_path).route(RoutingArticle(title="Ghana parliament passes bill", summary="A short stub."))
    key = "africa"
    title_score = next(score.score for score in title_only.channel_scores if score.channel_key == key)
    summary_score = next(score.score for score in with_summary.channel_scores if score.channel_key == key)
    assert title_score < summary_score


def test_chinese_carrier_routes_to_naval_and_indo_pacific(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Chinese carrier Liaoning enters Philippine Sea"))
    assert {"sea", "indo-pacific"} <= set(decision.selected_channel_keys)


def test_photo_gallery_gets_skip_behavior(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="The week around the world in pictures"))
    assert "photo_gallery" in decision.emitted_tags
    assert "skip_candidate" in decision.emitted_tags
    assert decision.decision_status == "skipped"
    assert decision.selected_channel_keys == ()


def test_ghana_legislation_tags(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Ghana parliament passes anti-LGBTQ+ bill"))
    assert {"ghana", "africa", "legislation"} <= set(decision.emitted_tags) | set(decision.expanded_tags)


def test_colombia_routes_to_south_central_america(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(
        RoutingArticle(title="Colombia presidential runoff pits leftist senator against pro-Trump rival")
    )
    assert "south-central-america" in decision.selected_channel_keys
    assert "the-hill" not in decision.selected_channel_keys
    assert {"colombia", "latin_america"} <= set(decision.emitted_tags) | set(decision.expanded_tags)


def test_yemen_obituary_tags(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(
        RoutingArticle(title="Abdu Rabbuh Mansour Hadi, Exiled Ex-President of Yemen, Dies at 80")
    )
    assert {"yemen", "middle_east", "obituary"} <= set(decision.emitted_tags) | set(decision.expanded_tags)


def test_air_defense_routes_to_land_not_air_or_strategic(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="Germany deploys Patriot battery"))
    assert "land" in decision.selected_channel_keys
    assert "air" not in decision.selected_channel_keys
    assert "strategic-weapons" not in decision.selected_channel_keys


def test_generic_missile_does_not_route_to_strategic_weapons(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="US approves Hellfire missile sale"))
    assert "strategic-weapons" not in decision.selected_channel_keys


def test_nuclear_arms_route_to_strategic_weapons(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="New START nuclear arms talks resume"))
    assert "strategic-weapons" in decision.selected_channel_keys


def test_icbm_routes_to_strategic_weapons(tmp_path: Path) -> None:
    decision = engine(tmp_path).route(RoutingArticle(title="North Korea tests ICBM"))
    assert "strategic-weapons" in decision.selected_channel_keys


def test_debug_score_line_is_human_readable() -> None:
    line = _format_score_line(
        {
            "channel_key": "middle-east",
            "score": 11,
            "minimum_score": 4,
            "reasons": ["tag +5: middle_east", "tag +6: iran", "title_only -1"],
        }
    )
    assert line == "- middle-east: 11/4 (tag +5: middle_east; tag +6: iran; title_only -1)"
