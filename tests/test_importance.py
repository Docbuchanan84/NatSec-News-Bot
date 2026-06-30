from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.routing.importance import ImportanceTerm, apply_importance, build_importance_config, score_importance
from app.routing.models import KnowledgeMatch, RoutingArticle, RoutingDecision


def make_decision(
    *,
    status: str = "routed",
    concepts: tuple[str, ...] = (),
    emitted_tags: tuple[str, ...] = (),
    expanded_tags: tuple[str, ...] = (),
) -> RoutingDecision:
    return RoutingDecision(
        content_mode="title_and_stub",
        matched_entries=tuple(
            KnowledgeMatch(
                knowledge_entry_id=concept,
                matched_alias=concept.replace("_", " "),
                match_start=0,
                match_end=len(concept),
                emitted_tags=(),
                priority=10,
                score=1,
            )
            for concept in concepts
        ),
        emitted_tags=emitted_tags,
        expanded_tags=expanded_tags,
        channel_scores=(),
        selected_channel_keys=("europe",) if status == "routed" else (),
        decision_status=status,
        top_score=8,
        explanation=(),
    )


def test_low_signal_no_match_caps_at_two() -> None:
    score, reasons = score_importance(
        make_decision(status="no_match", emitted_tags=("government",)),
        RoutingArticle(title="Weekly briefing released", source_class="wire_service"),
    )

    assert score <= 2
    assert "low_signal_cap 2" in reasons


def test_active_conflict_scores_high_importance() -> None:
    score, reasons = score_importance(
        make_decision(
            concepts=("ukraine_war",),
            emitted_tags=("ukraine", "active_conflict", "attack", "missile"),
            expanded_tags=("europe", "world", "military"),
        ),
        RoutingArticle(title="Breaking missile strike hits Kyiv", source_class="wire_service"),
    )

    assert score == 10
    assert "concept +3: ukraine_war" in reasons
    assert "tag +3: active_conflict" in reasons


def test_medium_regional_security_story_scores_below_hot_conflict() -> None:
    score, reasons = score_importance(
        make_decision(emitted_tags=("diplomacy", "sanctions"), expanded_tags=("world",)),
        RoutingArticle(title="Allies expand sanctions after talks", source_class="think_tank"),
    )

    assert score == 4
    assert "source_class +1: think_tank" in reasons


def test_apply_importance_returns_decision_with_score() -> None:
    decision = apply_importance(
        make_decision(emitted_tags=("cyber",)),
        RoutingArticle(title="Critical infrastructure cyber attack reported", source_class="official_us_defense"),
    )

    assert decision.importance_score >= 5
    assert decision.importance_reasons


def test_custom_watch_term_can_raise_importance() -> None:
    score, reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Coup alert prompts emergency meeting", source_class="wire_service"),
        build_importance_config([ImportanceTerm("coup alert", 5, "watch")]),
    )

    assert score >= 8
    assert "watch +5: coup alert" in reasons


def test_disabled_watch_term_overrides_default() -> None:
    enabled_score, enabled_reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Navy says destroyer sunk near contested strait"),
    )
    disabled_score, disabled_reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Navy says destroyer sunk near contested strait"),
        build_importance_config([ImportanceTerm("sunk", 4, "major_event", enabled=False)]),
    )

    assert enabled_score > disabled_score
    assert "watch +4: sunk" in enabled_reasons
    assert "watch +4: sunk" not in disabled_reasons


def test_fresh_valid_timestamp_adds_recency_signal() -> None:
    ingested_at = datetime(2026, 6, 30, 12, tzinfo=UTC)
    fresh_score, fresh_reasons = score_importance(
        make_decision(),
        RoutingArticle(
            title="Sanctions talks continue",
            source_class="wire_service",
            published_at=ingested_at - timedelta(minutes=20),
            ingested_at=ingested_at,
            timestamp_status="valid",
        ),
    )
    stale_score, stale_reasons = score_importance(
        make_decision(),
        RoutingArticle(
            title="Sanctions talks continue",
            source_class="wire_service",
            published_at=ingested_at - timedelta(hours=12),
            ingested_at=ingested_at,
            timestamp_status="valid",
        ),
    )

    assert fresh_score == stale_score + 2
    assert "recency +2" in fresh_reasons
    assert "recency +2" not in stale_reasons


def test_routine_analysis_gets_dampened() -> None:
    score, reasons = score_importance(
        make_decision(emitted_tags=("government",)),
        RoutingArticle(title="Analysis: weekly briefing reviews procurement plans", source_class="think_tank"),
    )

    assert score <= 2
    assert "dampener -2: roundup" in reasons or "dampener -2: opinion" in reasons


def test_unrouted_critical_story_is_not_forced_to_low_signal_floor() -> None:
    score, reasons = score_importance(
        make_decision(status="no_match"),
        RoutingArticle(title="Breaking news: tanker sunk after missile strike"),
    )

    assert score >= 5
    assert "unrouted_critical_cap 6" in reasons
