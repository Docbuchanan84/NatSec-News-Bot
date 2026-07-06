from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.config_loader import ConfigError, load_config
from app.routing.models import RoutingArticle
from app.routing_v2 import WeightedRoutingConfigError, WeightedRoutingEngine, load_weighted_routing_config
from app.routing_v2.matcher import literal_to_regex


def production_v2_engine() -> WeightedRoutingEngine:
    config = load_config(Path("config/config.json"))
    return WeightedRoutingEngine(load_weighted_routing_config(Path("config/routing_v2"), config))


def channel_score(decision, channel_key: str) -> int:
    for score in decision.channel_scores:
        if score.channel_key == channel_key:
            return score.score
    return 0


def write_source_url_config(tmp_path: Path, sources: list[dict]):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "version": 1,
          "settings": {"routing": {"enabled": true, "mode": "enforced", "engine": "weighted_v2"}},
          "channels": [
            {"key": "sea", "name": "Sea", "discordChannelId": "111111111111111111"},
            {"key": "air", "name": "Air", "discordChannelId": "222222222222222222"},
            {"key": "review", "name": "Review", "discordChannelId": "333333333333333333"}
          ]
        }
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    root = tmp_path / "routing_v2"
    root.mkdir()
    (root / "routes.json").write_text(
        """
        {
          "version": 1,
          "settings": {
            "primary_threshold": 25,
            "review_threshold": 20,
            "noise_threshold": 35,
            "field_multipliers": {"title": 1.0, "summary": 0.7, "url_slug": 0.4, "source_name": 0.8}
          },
          "routes": [
            {"key": "sea", "destination_class": "primary", "threshold": 25},
            {"key": "air", "destination_class": "primary", "threshold": 25},
            {"key": "review", "destination_class": "review", "threshold": 20},
            {"key": "noise", "destination_class": "pseudo", "pseudo": true, "threshold": 35}
          ]
        }
        """,
        encoding="utf-8",
    )
    (root / "evidence.json").write_text(
        """
        {
          "version": 1,
          "evidence": [
            {"id": "destroyer", "type": "literal", "phrase": "destroyer", "scores": {"sea": 25}}
          ]
        }
        """,
        encoding="utf-8",
    )
    (root / "sources.json").write_text(
        json.dumps({"version": 1, "sources": sources}, indent=2),
        encoding="utf-8",
    )
    (root / "mirrors.json").write_text('{"version": 1, "mirrors": []}', encoding="utf-8")
    return WeightedRoutingEngine(load_weighted_routing_config(root, config))


def test_literal_to_regex_handles_spacing_hyphens_plural_and_possessive() -> None:
    pattern = re.compile(literal_to_regex("carrier strike group"), re.IGNORECASE)

    assert pattern.search("Carrier Strike Group deploys")
    assert pattern.search("carrier-strike groups deploy")
    assert pattern.search("carrier strike group's deployment")
    assert not pattern.search("carriersomething strike group")


def test_sub_sandwich_blocks_weak_sub_and_routes_review_noise() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title="Best sub sandwich shops near naval base", source_name="Local Guide")
    )
    matched_ids = {match.knowledge_entry_id for match in decision.matched_entries}

    assert decision.decision_status == "review"
    assert decision.reason == "noise_candidate"
    assert "review" in decision.final_channel_keys
    assert "sub_sandwich_noise" in matched_ids
    assert "weak_sub" not in matched_ids
    assert "sea" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "How 'Obsession' went from a sub-$1M horror film to a box office phenomenon",
        "A whole fleet of moon landers and rovers will arrive on the lunar surface",
        "Big relief for Sreesanth as cricket board revokes three-year ban",
        "Singer co-wrote hits including YMCA and In the Navy",
        "Businessman backs arming security group after CSG meeting",
        "Navy Federal Credit Union announces new mortgage rate",
        "Carrier Global shares rise after earnings",
        "America's Best Towns to Visit this year",
    ],
)
def test_common_domain_false_positives_do_not_route_sea(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert "sea" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "England comes back with 2 goals from Harry Kane in World Cup",
        "Belgium completes stunning World Cup comeback against Senegal to set up showdown with USA",
        "The USMNT is set to face Bosnia and Herzegovina in the World Cup round of 32",
        "Celtics agree to send Jaylen Brown to 76ers in blockbuster NBA trade",
        "Former NBA star Malik Beasley pleads not guilty to gambling charges",
        "Monmouth football assistant coach dies unexpectedly at 34",
    ],
)
def test_sports_articles_route_to_sports(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert decision.primary_channel_keys[0] == "sports"
    assert "sports" in decision.final_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "Budget fight becomes political football in Congress",
        "Belgium announces new defense spending plan after NATO summit",
        "Philadelphia announces new infrastructure bond sale",
    ],
)
def test_sports_route_avoids_city_country_and_political_football_false_positives(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert "sports" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "Tropical storm Douglas forms in Pacific Ocean, no threat to land, hurricane center says",
        "Millions of Americans face dangerous temperatures as heat wave bears down",
        "Extreme heat forecast: What to expect as heat wave hits Midwest and Northeast",
        "Tornado warning issued as severe thunderstorms sweep across Oklahoma",
        "Atmospheric river brings flash flooding threat to California",
    ],
)
def test_weather_articles_route_to_weather(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert decision.primary_channel_keys[0] == "weather"
    assert "weather" in decision.final_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "Budget fight creates political storm in Congress",
        "Minister weathered criticism after procurement scandal",
        "Stormzy and Oritse Williams pay tribute to musician stabbed in London",
        "Air Force contract covers IT and weather forecasting at training bases",
    ],
)
def test_weather_route_avoids_metaphors_and_support_functions(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert "weather" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "Gold holds gain after Warsh remarks ease Fed rate-hike prospects",
        "South Korea inflation accelerates to 30-month high in June",
        "CPI report shows core inflation cooling more than expected",
        "Jobs report shows nonfarm payrolls slowing as unemployment rate rises",
        "Treasury yields fall as recession fears grow after weak GDP data",
    ],
)
def test_economy_articles_route_to_economy(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert decision.primary_channel_keys[0] == "economy"
    assert "economy" in decision.final_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "Blue Owl fund facing redemptions raises $500 million in bond sale",
        "Company shares rise after quarterly earnings beat expectations",
        "Alan Greenspan, former Federal Reserve chairman, dies at 100",
        "One example of Dems using the president's I love the inflation comment",
        "Navy Federal Credit Union announces new mortgage rate",
    ],
)
def test_economy_route_avoids_broad_business_and_context_false_positives(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert "economy" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title,expected_match",
    [
        ("Colorado governor signs emergency bill", "Colorado"),
        ("Golden State lawmakers debate new port rules", "Golden State"),
        ("Ontario announces new energy security plan", "Ontario"),
        ("Nuevo Leon governor meets automakers", "Nuevo Leon"),
        ("CDMX water restrictions expanded after drought", "CDMX"),
        ("CA: wildfire smoke closes schools", "CA:"),
    ],
)
def test_north_america_subdivision_terms_add_low_weight_signal(title: str, expected_match: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert channel_score(decision, "north-america") > 0
    assert any(match.matched_alias == expected_match for match in decision.matched_entries)
    assert "north-america" not in decision.primary_channel_keys


@pytest.mark.parametrize(
    "title",
    [
        "IN lawmakers debate budget plan",
        "Officials weigh options in budget fight",
        "OR officials weigh options after vote",
        "ON balance, markets expect Fed rate decision",
    ],
)
def test_north_america_prefixes_do_not_match_ordinary_abbreviations(title: str) -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title=title, source_name="Broad News", source_class="major_media")
    )

    assert channel_score(decision, "north-america") == 0


def test_strong_submarine_routes_sea() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title="Royal Navy submarine enters the Barents Sea", source_name="Royal Navy via Google News")
    )

    assert decision.primary_channel_keys[0] == "sea"


def test_red_sea_current_event_routes_region_before_domain() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="U.S. Navy shoots down Houthi drone in the Red Sea",
            source_name="Reuters",
            source_id="reuters",
            source_class="wire_service",
        )
    )

    assert decision.primary_channel_keys[0] == "middle-east"
    assert "sea" not in decision.primary_channel_keys[:1]


def test_domain_change_routes_sea_before_region_or_industrial() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="Navy changes destroyer maintenance model after fleet readiness review",
            source_name="USNI News",
            source_id="usni",
            source_class="defense_media",
        )
    )

    assert decision.primary_channel_keys[0] == "sea"


def test_second_primary_allowed_within_ten_percent() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title="F-35 aircraft carrier integration test expands", source_name="Defense News")
    )

    assert set(decision.primary_channel_keys[:2]) == {"sea", "air"}


def test_no_clear_route_goes_to_review() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(title="Local school board announces lunch menu", source_name="Local News")
    )

    assert decision.decision_status == "review"
    assert decision.reason == "no_route_threshold"
    assert decision.final_channel_keys == ("review",)


def test_source_mirrors_remain_post_primary() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="Navy destroyer conducts freedom of navigation patrol",
            source_name="USNI News",
            source_id="usni",
            source_class="defense_media",
        )
    )

    assert decision.primary_channel_keys == ("sea",)
    assert "defense-media" in decision.mirror_channel_keys
    assert decision.final_channel_keys == ("sea", "defense-media")


def test_noelreports_source_score_keeps_operational_updates_in_europe_only() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="Operational update",
            summary="Ukrainian infantry and air defense units struck Russian positions overnight.",
            source_name="Bluesky: NOELREPORTS",
            source_id="bluesky-noelreports",
            source_class="defense_media",
            source_url="https://bsky.app/profile/noelreports.com/rss",
            routing_tags=("ukraine", "europe", "active_conflict"),
        )
    )

    assert decision.primary_channel_keys == ("europe",)
    assert decision.mirror_channel_keys == ()
    assert decision.final_channel_keys == ("europe",)
    assert channel_score(decision, "europe") >= 80
    assert "land" not in decision.final_channel_keys
    assert "defense-media" not in decision.final_channel_keys


def test_natsec_news_route_is_hard_gated_to_nsn_x_account() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="National security officials warn of intelligence breach",
            source_name="Broad News",
            source_id="reuters",
            source_class="wire_service",
        )
    )

    assert "natsec-news" not in decision.primary_channel_keys
    assert "natsec-news" not in decision.final_channel_keys
    natsec_score = next(score for score in decision.channel_scores if score.channel_key == "natsec-news")
    assert natsec_score.score == 0
    assert natsec_score.reasons == ("required_source_ids not met",)


def test_natsec_news_route_allows_nsn_x_account() -> None:
    decision = production_v2_engine().route(
        RoutingArticle(
            title="National security update from NatSec News",
            source_name="X: @NatSec_News",
            source_id="x-natsec-news",
            source_class="owned_social",
        )
    )

    assert decision.primary_channel_keys[0] == "natsec-news"


def test_weighted_config_rejects_dangerous_regex(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "version": 1,
          "channels": [
            {"key": "review", "name": "Review", "discordChannelId": "111111111111111111"}
          ]
        }
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    root = tmp_path / "routing_v2"
    root.mkdir()
    (root / "routes.json").write_text(
        """
        {
          "version": 1,
          "routes": [
            {"key": "review", "destination_class": "review"},
            {"key": "noise", "destination_class": "pseudo", "pseudo": true}
          ]
        }
        """,
        encoding="utf-8",
    )
    (root / "evidence.json").write_text(
        """
        {
          "version": 1,
          "evidence": [
            {"id": "bad_regex", "type": "pattern", "pattern": "(a+)+", "scores": {"noise": 10}}
          ]
        }
        """,
        encoding="utf-8",
    )
    (root / "sources.json").write_text('{"version": 1, "sources": []}', encoding="utf-8")
    (root / "mirrors.json").write_text('{"version": 1, "mirrors": []}', encoding="utf-8")

    with pytest.raises(WeightedRoutingConfigError) as exc:
        load_weighted_routing_config(root, config)

    assert "nested quantifiers" in str(exc.value)


def test_source_url_host_score_is_bias_only_and_cannot_route_by_itself(tmp_path: Path) -> None:
    engine = write_source_url_config(
        tmp_path,
        [
            {
                "id": "source-url-navy",
                "source_url_hosts": ["news.navy.mil"],
                "scores": {"sea": 35},
            }
        ],
    )

    decision = engine.route(
        RoutingArticle(
            title="Ceremony held at headquarters",
            source_url="https://news.navy.mil/rss.xml",
        )
    )

    assert channel_score(decision, "sea") == 35
    assert decision.decision_status == "review"
    assert decision.reason == "no_route_threshold"
    assert "sea" not in decision.primary_channel_keys
    assert any("source:source-url-navy" == match.knowledge_entry_id for match in decision.matched_entries)


def test_source_url_host_score_can_boost_route_with_content_evidence(tmp_path: Path) -> None:
    engine = write_source_url_config(
        tmp_path,
        [
            {
                "id": "source-url-navy",
                "source_url_hosts": ["news.navy.mil"],
                "scores": {"sea": 10},
            }
        ],
    )

    decision = engine.route(
        RoutingArticle(
            title="Destroyer maintenance plan expands",
            source_url="https://news.navy.mil/rss.xml",
        )
    )

    assert channel_score(decision, "sea") == 35
    assert decision.primary_channel_keys == ("sea",)
    assert any("source_url +10: source-url-navy" in reason for score in decision.channel_scores for reason in score.reasons)


def test_source_url_negative_score_suppresses_route(tmp_path: Path) -> None:
    engine = write_source_url_config(
        tmp_path,
        [
            {
                "id": "source-url-no-sea",
                "source_url_hosts": ["movies.example"],
                "scores": {"sea": -20, "review": 5},
            }
        ],
    )

    decision = engine.route(
        RoutingArticle(
            title="Destroyer movie trailer released",
            source_url="https://movies.example/rss",
        )
    )

    assert channel_score(decision, "sea") == 5
    assert "sea" not in decision.primary_channel_keys


def test_source_url_path_terms_match_complete_tokens_only(tmp_path: Path) -> None:
    engine = write_source_url_config(
        tmp_path,
        [
            {
                "id": "source-url-ar",
                "source_url_path_terms": ["ar"],
                "scores": {"sea": 35},
            }
        ],
    )

    no_match = engine.route(RoutingArticle(title="Destroyer update", source_url="https://example.com/are/rss.xml"))
    match = engine.route(RoutingArticle(title="Destroyer update", source_url="https://example.com/ar/rss.xml"))

    assert channel_score(no_match, "sea") == 25
    assert channel_score(match, "sea") == 60


def test_source_url_path_regex_is_validated(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "version": 1,
          "channels": [
            {"key": "sea", "name": "Sea", "discordChannelId": "111111111111111111"},
            {"key": "review", "name": "Review", "discordChannelId": "222222222222222222"}
          ]
        }
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    root = tmp_path / "routing_v2"
    root.mkdir()
    (root / "routes.json").write_text(
        """
        {
          "version": 1,
          "routes": [
            {"key": "sea", "destination_class": "primary"},
            {"key": "review", "destination_class": "review"},
            {"key": "noise", "destination_class": "pseudo", "pseudo": true}
          ]
        }
        """,
        encoding="utf-8",
    )
    (root / "evidence.json").write_text('{"version": 1, "evidence": []}', encoding="utf-8")
    (root / "sources.json").write_text(
        '{"version": 1, "sources": [{"id": "bad-source-regex", "source_url_path_patterns": ["(a+)+"], "scores": {"sea": 10}}]}',
        encoding="utf-8",
    )
    (root / "mirrors.json").write_text('{"version": 1, "mirrors": []}', encoding="utf-8")

    with pytest.raises(WeightedRoutingConfigError) as exc:
        load_weighted_routing_config(root, config)

    assert "nested quantifiers" in str(exc.value)
