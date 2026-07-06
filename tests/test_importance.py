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

    assert score <= 20
    assert "low_signal_cap 20" in reasons


def test_active_conflict_scores_high_importance() -> None:
    score, reasons = score_importance(
        make_decision(
            concepts=("ukraine_war",),
            emitted_tags=("ukraine", "active_conflict", "attack", "missile"),
            expanded_tags=("europe", "world", "military"),
        ),
        RoutingArticle(title="Breaking missile strike hits Kyiv", source_class="wire_service"),
    )

    assert score == 100
    assert "concept +18: ukraine_war" in reasons
    assert "tag +18: active_conflict" in reasons


def test_medium_regional_security_story_scores_below_hot_conflict() -> None:
    score, reasons = score_importance(
        make_decision(emitted_tags=("diplomacy", "sanctions"), expanded_tags=("world",)),
        RoutingArticle(title="Allies expand sanctions after talks", source_class="think_tank"),
    )

    assert 10 <= score < 50
    assert "source_class +3: think_tank" in reasons


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
        build_importance_config([ImportanceTerm("coup alert", 25, "watch")]),
    )

    assert score >= 35
    assert "watch +25: coup alert" in reasons


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
    assert "watch +28: sunk" in enabled_reasons
    assert "watch +28: sunk" not in disabled_reasons


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

    assert fresh_score >= stale_score + 12
    assert "recency +18" in fresh_reasons
    assert "recency +18" not in stale_reasons


def test_routine_analysis_gets_dampened() -> None:
    score, reasons = score_importance(
        make_decision(emitted_tags=("government",)),
        RoutingArticle(title="Analysis: weekly briefing reviews procurement plans", source_class="think_tank"),
    )

    assert score <= 5
    assert "dampener -12: roundup" in reasons or "dampener -12: opinion" in reasons


def test_unrouted_critical_story_is_not_forced_to_low_signal_floor() -> None:
    score, reasons = score_importance(
        make_decision(status="no_match"),
        RoutingArticle(title="Breaking news: tanker sunk after missile strike"),
    )

    assert score >= 50
    assert "unrouted_critical_cap 60" in reasons


def test_negative_watch_term_lowers_importance() -> None:
    base_score, _base_reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Defense contract awarded for patrol aircraft"),
    )
    score, reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Defense contract awarded for patrol aircraft"),
        build_importance_config([ImportanceTerm("contract awarded", -20, "noise")]),
    )

    assert score <= max(0, base_score - 15)
    assert "watch -20: contract awarded" in reasons


def test_expired_watch_term_is_ignored() -> None:
    now = datetime(2026, 6, 30, 12, tzinfo=UTC)
    score, reasons = score_importance(
        make_decision(),
        RoutingArticle(title="Crimea bridge alert"),
        build_importance_config(
            [ImportanceTerm("crimea bridge", 25, "trend", expires_at=now - timedelta(hours=1))],
            now=now,
        ),
    )

    assert score < 30
    assert all("crimea bridge" not in reason for reason in reasons)


def test_similar_recent_story_gets_minor_penalty() -> None:
    now = datetime(2026, 6, 30, 12, tzinfo=UTC)
    article = RoutingArticle(
        article_id=2,
        title="Navy destroyer enters Red Sea after Houthi missile attack",
        normalized_title="navy destroyer enters red sea after houthi missile attack",
        title_signature="navy destroyer enters red sea houthi missile attack",
        story_cluster_key="cluster-1",
    )
    base_score, _base_reasons = score_importance(make_decision(), article, build_importance_config(now=now))
    score, reasons = score_importance(
        make_decision(),
        article,
        build_importance_config(
            now=now,
            recent_articles=[
                {
                    "id": 1,
                    "title": "Navy destroyer enters Red Sea after Houthi missile attack",
                    "normalized_title": "navy destroyer enters red sea after houthi missile attack",
                    "title_signature": "navy destroyer enters red sea houthi missile attack",
                    "story_cluster_key": "cluster-1",
                    "normalized_published_at": now.isoformat(),
                }
            ],
        ),
    )

    assert score < base_score
    assert any(reason.startswith("similarity -") for reason in reasons)
