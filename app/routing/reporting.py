from __future__ import annotations

from collections import Counter
from typing import Iterable

from app.routing.models import RoutingDecision


def format_decision(decision: RoutingDecision, limit: int = 1900) -> str:
    lines = [
        f"Decision: {decision.decision_status}",
        f"Reason: {decision.reason or 'none'}",
        f"Importance: {decision.importance_score}/10 ({'; '.join(decision.importance_reasons[:4]) or 'not scored'})",
        f"Content mode: {decision.content_mode}",
        "Matched concepts: " + _format_concepts(decision),
        "Matched aliases: " + _format_aliases(decision),
        "Suppressions: " + _format_suppressions(decision),
        "Emitted tags: " + (", ".join(decision.emitted_tags) or "none"),
        "Expanded parent tags: " + _format_parent_tags(decision),
        "Primary destinations: " + (", ".join(decision.primary_channel_keys) or "none"),
        "Mirror destinations: " + (", ".join(decision.mirror_channel_keys) or "none"),
        "Review destinations: " + (", ".join(decision.review_channel_keys) or "none"),
        "Final destinations: " + (", ".join(decision.final_channel_keys) or "none"),
        "Scores:",
    ]
    for score in decision.channel_scores[:12]:
        marker = "*" if score.selected else "-"
        reasons = "; ".join(score.reasons[:3])
        lines.append(
            f"{marker} {score.channel_key} [{score.destination_class}]: "
            f"{score.score}/{score.minimum_score} ({reasons})"
        )
    return truncate("\n".join(lines), limit)


def format_backtest_summary(results: Iterable[tuple[int, str, RoutingDecision]], limit: int = 1900) -> str:
    materialized = list(results)
    status_counts = Counter(decision.decision_status for _, _, decision in materialized)
    importance_counts = Counter(decision.importance_score for _, _, decision in materialized)
    channel_counts = Counter(
        channel_key
        for _, _, decision in materialized
        for channel_key in decision.final_channel_keys
    )
    lines = [
        f"Backtest articles: {len(materialized)}",
        "Statuses: "
        + ", ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
        if status_counts
        else "Statuses: none",
        "Top suggested channels: "
        + (", ".join(f"{key}={count}" for key, count in channel_counts.most_common(8)) or "none"),
        "Importance: "
        + (", ".join(f"{score}={count}" for score, count in sorted(importance_counts.items())) or "none"),
        "",
        "Samples:",
    ]
    for article_id, title, decision in materialized[:8]:
        selected = ", ".join(decision.final_channel_keys) or decision.decision_status
        lines.append(f"{article_id}: {title[:90]} -> {selected}")
    lines.append("")
    lines.append("Full detail is in the audit log when audit logging is enabled.")
    return truncate("\n".join(lines), limit)


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "\n... truncated ..."
    return value[: max(0, limit - len(suffix))].rstrip() + suffix


def _format_concepts(decision: RoutingDecision) -> str:
    if not decision.matched_entries:
        return "none"
    ids = []
    seen = set()
    for match in decision.matched_entries:
        if match.knowledge_entry_id in seen:
            continue
        ids.append(match.knowledge_entry_id)
        seen.add(match.knowledge_entry_id)
    text = ", ".join(ids[:10])
    if len(ids) > 10:
        text += f", +{len(ids) - 10} more"
    return text


def _format_aliases(decision: RoutingDecision) -> str:
    if not decision.matched_entries:
        return "none"
    text = ", ".join(
        f"{match.matched_alias} -> {match.knowledge_entry_id}"
        for match in decision.matched_entries[:10]
    )
    if len(decision.matched_entries) > 10:
        text += f", +{len(decision.matched_entries) - 10} more"
    return text


def _format_suppressions(decision: RoutingDecision) -> str:
    if not decision.suppression_matches:
        return "none"
    text = ", ".join(
        f"{match.matched_alias} -> {match.suppression_id}"
        for match in decision.suppression_matches[:10]
    )
    if len(decision.suppression_matches) > 10:
        text += f", +{len(decision.suppression_matches) - 10} more"
    return text


def _format_parent_tags(decision: RoutingDecision) -> str:
    parents = [tag for tag in decision.expanded_tags if tag not in decision.emitted_tags]
    return ", ".join(parents) or "none"
