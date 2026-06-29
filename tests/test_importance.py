from __future__ import annotations

from app.routing.importance import apply_importance, score_importance
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

    assert score == 2
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

    assert score == 3
    assert "source_class +1: think_tank" in reasons


def test_apply_importance_returns_decision_with_score() -> None:
    decision = apply_importance(
        make_decision(emitted_tags=("cyber",)),
        RoutingArticle(title="Critical infrastructure cyber attack reported", source_class="official_us_defense"),
    )

    assert decision.importance_score >= 5
    assert decision.importance_reasons
