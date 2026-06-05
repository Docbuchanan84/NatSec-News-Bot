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


def production_engine() -> RoutingEngine:
    config = load_config(Path("config/config.json"))
    return RoutingEngine(load_routing_config(Path("config/routing"), config))


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


def test_state_department_africa_routes_region_not_white_house() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Secretary Rubio visits Ghana to discuss security cooperation",
            source_name="State Department Africa",
        )
    )
    assert "africa" in decision.selected_channel_keys
    assert "the-white-house" not in decision.selected_channel_keys


def test_us_domestic_non_politics_routes_north_america() -> None:
    decision = production_engine().route(
        RoutingArticle(title="California wildfire forces evacuations near Los Angeles", source_name="BBC US and Canada")
    )
    assert "north-america" in decision.selected_channel_keys
    assert "the-hill" not in decision.selected_channel_keys


def test_us_politics_routes_the_hill_not_north_america() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Congress passes border security legislation", source_name="The Hill")
    )
    assert "the-hill" in decision.selected_channel_keys
    assert "north-america" not in decision.selected_channel_keys


def test_brazil_economy_routes_region_not_science_technology() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Brazil economy grows as inflation cools", source_name="Bloomberg")
    )
    assert "south-central-america" in decision.selected_channel_keys
    assert "science-technology" not in decision.selected_channel_keys


def test_routine_india_items_do_not_route_indo_pacific() -> None:
    domestic_decision = production_engine().route(
        RoutingArticle(title="BJP wins Uttarakhand local election", source_name="The Indian Express")
    )
    economy_decision = production_engine().route(
        RoutingArticle(title="India economy grows as inflation cools", source_name="BBC Asia")
    )

    assert "indo-pacific" not in domestic_decision.selected_channel_keys
    assert "indo-pacific" not in economy_decision.selected_channel_keys


def test_high_importance_india_security_routes_indo_pacific() -> None:
    decision = production_engine().route(
        RoutingArticle(title="India-Pakistan missile strikes trigger Kashmir crisis", source_name="Reuters")
    )
    assert "indo-pacific" in decision.selected_channel_keys


def test_defense_contract_routes_industrial_base() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Lockheed wins Pentagon contract for missile interceptors", source_name="Defense News Industry")
    )
    assert "industrial-base" in decision.selected_channel_keys


def test_generic_procurement_does_not_route_industrial_base() -> None:
    decision = production_engine().route(
        RoutingArticle(title="City procurement office selects new payroll vendor", source_name="Bloomberg")
    )
    assert "industrial-base" not in decision.selected_channel_keys


def test_generic_company_earnings_do_not_route_industrial_base() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Apple shares rise after quarterly earnings beat forecasts", source_name="Bloomberg")
    )
    assert "industrial-base" not in decision.selected_channel_keys


def test_source_gates_keep_source_channels_exclusive() -> None:
    engine = production_engine()
    state_decision = engine.route(
        RoutingArticle(title="State Department announces talks with Ghana", source_name="State Department Press Releases")
    )
    defense_media_decision = engine.route(
        RoutingArticle(title="Defense One reports on Army modernization", source_name="Defense One")
    )
    reuters_mention_decision = engine.route(
        RoutingArticle(title="BBC cites Reuters report on China talks", source_name="BBC Asia")
    )
    ap_mention_decision = engine.route(
        RoutingArticle(title="NPR discusses Associated Press election analysis", source_name="NPR")
    )

    assert "the-white-house" not in state_decision.selected_channel_keys
    assert "dept-of-war" not in defense_media_decision.selected_channel_keys
    assert "reuters" not in reuters_mention_decision.selected_channel_keys
    assert "associated-press" not in ap_mention_decision.selected_channel_keys


def test_official_dod_source_can_route_dept_of_war() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Defense.gov announces new military exercise readiness initiative",
            source_name="Defense.gov Top News",
            source_id="defense-gov",
            source_class="official_us_defense",
        )
    )
    assert "dept-of-war" in decision.mirror_channel_keys
    assert "dept-of-war" in decision.final_channel_keys


def test_no_match_drops_source_mirror() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Reuters publishes morning briefing",
            source_name="Reuters",
            source_id="reuters",
            source_class="wire_service",
        )
    )
    assert decision.decision_status == "no_match"
    assert decision.final_channel_keys == ()


def test_skip_suppresses_source_mirror() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Reuters sports roundup covers World Cup and tennis",
            source_name="Reuters",
            source_id="reuters",
            source_class="wire_service",
        )
    )
    assert decision.decision_status == "skipped"
    assert "reuters" not in decision.final_channel_keys


def test_review_routes_only_to_review() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Former Yemen president dies at 80",
            source_name="Associated Press",
            source_id="associated-press",
            source_class="wire_service",
        )
    )
    assert decision.decision_status == "review"
    assert decision.final_channel_keys == ("review",)


def test_source_mirror_is_additive_and_does_not_consume_primary_slot() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Reuters: Iran sanctions expand after missile attack",
            source_name="Reuters",
            source_id="reuters",
            source_class="wire_service",
        )
    )
    assert "middle-east" in decision.primary_channel_keys
    assert "reuters" in decision.mirror_channel_keys
    assert decision.final_channel_keys[: len(decision.primary_channel_keys)] == decision.primary_channel_keys


def test_iran_war_missile_attack_routes_middle_east_not_domain_bucket() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Iran launches missiles at Israel as Gulf states brace for wider conflict",
            source_name="Reuters",
        )
    )
    assert "middle-east" in decision.selected_channel_keys
    assert "air" not in decision.selected_channel_keys
    assert "land" not in decision.selected_channel_keys
    assert "sea" not in decision.selected_channel_keys


def test_gulf_state_iran_conflict_routes_middle_east() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Bahrain and Kuwait raise alerts after Iranian drone attack near the Gulf",
            source_name="Associated Press",
        )
    )
    assert "middle-east" in decision.selected_channel_keys
    assert "air" not in decision.selected_channel_keys


def test_strait_of_hormuz_conflict_routes_middle_east_not_sea() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="US Navy destroyer responds to Iranian missile attack in the Strait of Hormuz",
            source_name="USNI News",
        )
    )
    assert "middle-east" in decision.selected_channel_keys
    assert "sea" not in decision.selected_channel_keys


def test_russia_ukraine_missile_attack_routes_europe_not_domain_bucket() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Russia launches missile attack on Kyiv as Ukraine war intensifies",
            source_name="Associated Press",
        )
    )
    assert "europe" in decision.selected_channel_keys
    assert "air" not in decision.selected_channel_keys
    assert "land" not in decision.selected_channel_keys
    assert "strategic-weapons" not in decision.selected_channel_keys


def test_russian_drone_attack_routes_europe_not_air() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Russian drone attack hits Ukraine energy sites overnight",
            source_name="BBC Europe",
        )
    )
    assert "europe" in decision.selected_channel_keys
    assert "air" not in decision.selected_channel_keys


def test_debug_score_line_is_human_readable() -> None:
    line = _format_score_line(
        {
            "channel_key": "middle-east",
            "score": 11,
            "minimum_score": 4,
            "reasons": ["tag +5: middle_east", "tag +6: iran", "title_only -1"],
        }
    )
    assert line == "- middle-east [primary]: 11/4 (tag +5: middle_east; tag +6: iran; title_only -1)"


def test_patriot_contract_routes_industrial_base_and_europe() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Patriot contract driven by Ukraine demand expands production",
            source_name="Defense News",
            source_id="defense-news",
            source_class="defense_media",
        )
    )
    assert "industrial-base" in decision.primary_channel_keys
    assert "europe" in decision.primary_channel_keys
    assert "land" not in decision.primary_channel_keys


def test_carrier_global_false_positive_does_not_route_sea() -> None:
    decision = production_engine().route(
        RoutingArticle(
            title="Carrier Global shares rise after earnings",
            source_name="Reuters",
            source_id="reuters",
            source_class="wire_service",
        )
    )
    assert decision.decision_status == "skipped"
    assert "sea" not in decision.final_channel_keys


def test_naval_special_warfare_routes_special_operations() -> None:
    decision = production_engine().route(
        RoutingArticle(title="SEAL Team conducts maritime special operations exercise", source_name="DVIDS")
    )
    assert "special-operations" in decision.selected_channel_keys
    assert "sea" not in decision.selected_channel_keys


def test_marine_expeditionary_unit_routes_sea() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Marine Expeditionary Unit sails with amphibious ready group", source_name="DVIDS")
    )
    assert "sea" in decision.selected_channel_keys


def test_marine_littoral_regiment_routes_land() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Marine Littoral Regiment trains in the Philippines", source_name="DVIDS")
    )
    assert "land" in decision.selected_channel_keys


def test_army_force_tracking_routes_land() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Army sustainment brigade opens logistics hub", source_name="DVIDS")
    )
    assert "land" in decision.selected_channel_keys


def test_air_force_wing_routes_air() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Air Force fighter wing deploys F-35s to Kadena", source_name="DVIDS")
    )
    assert "air" in decision.selected_channel_keys


def test_military_sealift_routes_sea() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Military Sealift Command oiler supports carrier strike group", source_name="DVIDS")
    )
    assert "sea" in decision.selected_channel_keys
    assert "special-operations" not in decision.selected_channel_keys


def test_naval_air_wing_routes_sea() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Carrier Air Wing patrol squadron deploys P-8 aircraft", source_name="DVIDS")
    )
    assert "sea" in decision.selected_channel_keys
    assert "special-operations" not in decision.selected_channel_keys


def test_navy_destroyer_does_not_route_special_operations() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Navy destroyer conducts freedom of navigation patrol", source_name="DVIDS")
    )
    assert "sea" in decision.selected_channel_keys
    assert "special-operations" not in decision.selected_channel_keys


def test_generic_attack_does_not_route_special_operations() -> None:
    decision = production_engine().route(
        RoutingArticle(title="Drone attack damages port facility", source_name="Associated Press")
    )
    assert "special-operations" not in decision.selected_channel_keys
