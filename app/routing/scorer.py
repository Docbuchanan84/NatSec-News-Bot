from __future__ import annotations

from app.routing.models import ChannelRule, ChannelScore, KnowledgeMatch


def score_channels(
    rules: tuple[ChannelRule, ...],
    matches: tuple[KnowledgeMatch, ...],
    emitted_tags: set[str],
    expanded_tags: set[str],
    content_mode: str,
    source_name: str | None,
    source_id: str | None,
    source_class: str | None,
    max_destinations: int,
    *,
    mirror_mode: bool = False,
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
                    destination_class=rule.destination_class,
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
                    destination_class=rule.destination_class,
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
                    destination_class=rule.destination_class,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=(f"excluded_any met: {', '.join(excluded_matches)}",),
                )
            )
            continue

        source_gate_reason = _source_gate_reason(rule, source_id, source_class)
        if source_gate_reason:
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    destination_class=rule.destination_class,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=(source_gate_reason,),
                )
            )
            continue
        required_source_matches = _matched_source_hints(rule.required_source_any, source_name)
        if rule.required_source_any and not required_source_matches:
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    destination_class=rule.destination_class,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=("required_source_any not met",),
                )
            )
            continue
        excluded_source_matches = _matched_source_hints(rule.excluded_source_any, source_name)
        if excluded_source_matches:
            scores.append(
                ChannelScore(
                    channel_key=rule.channel_key,
                    destination_class=rule.destination_class,
                    score=0,
                    minimum_score=rule.minimum_score,
                    priority=rule.priority,
                    selected=False,
                    reasons=(f"excluded_source_any met: {', '.join(excluded_source_matches)}",),
                )
            )
            continue

        score = 0
        if rule.required_source_ids:
            reasons.append(f"source_id required: {source_id}")
        if rule.required_source_classes:
            reasons.append(f"source_class required: {source_class}")
        for source_hint in required_source_matches:
            reasons.append(f"source required: {source_hint}")
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
                destination_class=rule.destination_class,
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
        if _selectable(item, mirror_mode=mirror_mode)
    ][: max(0, max_destinations)]
    selected_keys = {item.channel_key for item in selected_ranked}

    return tuple(
        ChannelScore(
            channel_key=item.channel_key,
            destination_class=item.destination_class,
            score=item.score,
            minimum_score=item.minimum_score,
            priority=item.priority,
            selected=item.channel_key in selected_keys,
            reasons=item.reasons,
        )
        for item in ranked
    )


def _selectable(item: ChannelScore, *, mirror_mode: bool) -> bool:
    if item.score >= item.minimum_score and item.score > 0:
        return True
    if not mirror_mode:
        return False
    return any(
        reason.startswith("source_id required:") or reason.startswith("source_class required:")
        for reason in item.reasons
    )


def _source_gate_reason(rule: ChannelRule, source_id: str | None, source_class: str | None) -> str | None:
    source_id_value = (source_id or "unknown").casefold()
    source_class_value = (source_class or "unknown").casefold()
    required_ids = {value.casefold() for value in rule.required_source_ids}
    excluded_ids = {value.casefold() for value in rule.excluded_source_ids}
    required_classes = {value.casefold() for value in rule.required_source_classes}
    excluded_classes = {value.casefold() for value in rule.excluded_source_classes}
    if required_ids and source_id_value not in required_ids:
        return "required_source_ids not met"
    if source_id_value in excluded_ids:
        return f"excluded_source_ids met: {source_id}"
    if required_classes and source_class_value not in required_classes:
        return "required_source_classes not met"
    if source_class_value in excluded_classes:
        return f"excluded_source_classes met: {source_class}"
    return None


def _matched_source_hints(values: tuple[str, ...], source_name: str | None) -> tuple[str, ...]:
    if not values or not source_name:
        return ()
    source_folded = source_name.casefold()
    return tuple(value for value in values if value.casefold() in source_folded)


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
