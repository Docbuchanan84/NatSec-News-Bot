from __future__ import annotations

from collections import Counter
from typing import Iterable

from app.routing.models import RoutingDecision


def format_decision(decision: RoutingDecision, limit: int = 1900) -> str:
    lines = [
        f"Decision: {decision.decision_status}",
        f"Reason: {decision.reason or 'none'}",
        f"Content mode: {decision.content_mode}",
        "Matches: " + _format_matches(decision),
        "Emitted tags: " + (", ".join(decision.emitted_tags) or "none"),
        "Expanded tags: " + (", ".join(decision.expanded_tags) or "none"),
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


def _format_matches(decision: RoutingDecision) -> str:
    if not decision.matched_entries:
        return "none"
    text = ", ".join(
        f"{match.knowledge_entry_id} ({match.matched_alias})"
        for match in decision.matched_entries[:10]
    )
    if len(decision.matched_entries) > 10:
        text += f", +{len(decision.matched_entries) - 10} more"
    return text
