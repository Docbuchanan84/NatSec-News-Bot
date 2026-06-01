from __future__ import annotations

from app.routing.models import ChannelRule, ChannelScore, KnowledgeMatch


def score_channels(
    rules: tuple[ChannelRule, ...],
    matches: tuple[KnowledgeMatch, ...],
    emitted_tags: set[str],
    expanded_tags: set[str],
    content_mode: str,
    source_name: str | None,
    max_destinations: int,
) -> tuple[ChannelScore, ...]:
    match_ids = {match.knowledge_entry_id for match in matches}
    alias_keys = {match.matched_alias.casefold() for match in matches}
    all_tags = emitted_tags | expanded_tags
    scores: list[ChannelScore] = []

    for rule in rules:
        if not rule.enabled:
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=("disabled",),
                )
            )
            continue

        reasons: list[str] = []
        if rule.required_any and not _required_any_met(rule.required_any, all_tags, match_ids, alias_keys):
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=("required_any not met",),
                )
            )
            continue
        excluded_matches = _matched_any(rule.excluded_any, all_tags, match_ids, alias_keys)
        if excluded_matches:
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=(f"excluded_any met: {', '.join(excluded_matches)}",),
                )
            )
            continue

        score = 0
        for key, value in rule.term_boosts.items():
            if key in match_ids or key.casefold() in alias_keys:
                score += value
                reasons.append(f"term +{value}: {key}")
        for tag, value in rule.tag_boosts.items():
            if tag in all_tags:
                score += value
                reasons.append(f"tag +{value}: {tag}")
        for key, value in rule.term_penalties.items():
            if key in match_ids or key.casefold() in alias_keys:
                score -= value
                reasons.append(f"term -{value}: {key}")
        for tag, value in rule.tag_penalties.items():
            if tag in all_tags:
                score -= value
                reasons.append(f"tag -{value}: {tag}")

        if source_name:
            source_folded = source_name.casefold()
            for source_hint, value in rule.source_biases.items():
                if source_hint.casefold() in source_folded:
                    score += value
                    reasons.append(f"source {value:+}: {source_hint}")

        adjustment = rule.content_mode_adjustments.get(content_mode, 0)
        if adjustment:
            score += adjustment
            reasons.append(f"{content_mode} {adjustment:+}")

        scores.append(
            ChannelScore(
                channel_key=rule.channel_key,
                score=score,
                minimum_score=rule.minimum_score,
                priority=rule.priority,
                selected=False,
                reasons=tuple(reasons) or ("no score contributions",),
            )
        )

    ranked = sorted(scores, key=lambda item: (-item.score, -item.priority, item.channel_key))
    selected_ranked = [
        item
        for item in ranked
        if item.score >= item.minimum_score and item.score > 0
    ][: max(0, max_destinations)]
    selected_keys = {item.channel_key for item in selected_ranked}

    return tuple(
        ChannelScore(
            channel_key=item.channel_key,
            score=item.score,
            minimum_score=item.minimum_score,
            priority=item.priority,
            selected=item.channel_key in selected_keys,
            reasons=item.reasons,
        )
        for item in ranked
    )


def _required_any_met(
    required_any: tuple[str, ...],
    tags: set[str],
    match_ids: set[str],
    alias_keys: set[str],
) -> bool:
    return bool(_matched_any(required_any, tags, match_ids, alias_keys))


def _matched_any(
    values: tuple[str, ...],
    tags: set[str],
    match_ids: set[str],
    alias_keys: set[str],
) -> tuple[str, ...]:
    matched: list[str] = []
    for value in values:
        if value in tags or value in match_ids or value.casefold() in alias_keys:
            matched.append(value)
    return tuple(matched)
