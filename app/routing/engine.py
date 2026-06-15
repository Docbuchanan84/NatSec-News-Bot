from __future__ import annotations

from urllib.parse import urlparse

from app.routing.matcher import match_knowledge_entries, match_suppression_entries
from app.routing.models import ChannelScore, RoutingArticle, RoutingConfig, RoutingDecision
from app.routing.scorer import score_channels
from app.routing.taxonomy import expand_tags

SUMMARY_MATCH_LIMIT = 1000
SECONDARY_PRIMARY_MIN_SCORE = 10
SECONDARY_PRIMARY_MAX_GAP = 3


class RoutingEngine:
    def __init__(self, config: RoutingConfig) -> None:
        self.config = config

    def route(self, article: RoutingArticle) -> RoutingDecision:
        content_mode = _content_mode(article)
        match_text = _build_match_text(article)
        matches = match_knowledge_entries(match_text, self.config.knowledge_entries)
        emitted_tags = {tag for match in matches for tag in match.emitted_tags}
        emitted_tags.update(article.routing_tags or ())
        expanded_tags = expand_tags(emitted_tags, self.config.taxonomy)
        all_tags = emitted_tags | expanded_tags
        suppression_matches = tuple(
            match
            for match in match_suppression_entries(match_text, self.config.suppression_entries)
            if not (set(match.unless_tags_any) & all_tags)
        )
        primary_rules = tuple(rule for rule in self.config.channel_rules if rule.destination_class == "primary")
        mirror_rules = tuple(rule for rule in self.config.channel_rules if rule.destination_class == "mirror")
        review_rules = tuple(rule for rule in self.config.channel_rules if rule.destination_class == "review")
        primary_scores = score_channels(
            primary_rules,
            matches,
            emitted_tags,
            expanded_tags,
            content_mode,
            article.source_name,
            article.source_id,
            article.source_class,
            self.config.max_primary_destinations or self.config.max_destinations,
        )
        mirror_scores = score_channels(
            mirror_rules,
            matches,
            emitted_tags,
            expanded_tags,
            content_mode,
            article.source_name,
            article.source_id,
            article.source_class,
            25,
            mirror_mode=True,
        )
        review_scores = _review_scores(review_rules, selected=False)
        scores = primary_scores + mirror_scores + review_scores
        top_score = max((score.score for score in scores), default=0)
        mirror_candidates = tuple(score for score in mirror_scores if score.selected)
        primary_keys = _select_primary_keys(primary_scores, has_mirror=bool(mirror_candidates))
        mirror_keys = _select_mirror_keys(mirror_candidates, primary_keys, self.config.max_destinations)
        scores = _set_score_selections(primary_scores + mirror_scores, set(primary_keys) | set(mirror_keys)) + review_scores
        review_keys: tuple[str, ...] = ()
        final_keys: tuple[str, ...] = ()
        status = "no_match"
        reason = "no_match"

        if any(match.action == "skip" for match in suppression_matches):
            status = "skipped"
            reason = "suppression_match"
            scores = _clear_score_selections(scores)
            primary_keys = ()
            mirror_keys = ()
        elif all_tags & set(self.config.skip_tags):
            status = "skipped"
            reason = "skipped_candidate"
            scores = _clear_score_selections(scores)
            primary_keys = ()
            mirror_keys = ()
        elif all_tags & set(self.config.review_tags):
            status = "review"
            reason = "review_required"
            primary_keys = ()
            mirror_keys = ()
            review_keys = tuple(rule.channel_key for rule in review_rules if rule.enabled)
            final_keys = review_keys
            scores = _clear_score_selections(primary_scores + mirror_scores) + _review_scores(review_rules, selected=True)
        elif not emitted_tags:
            status = "no_match"
            reason = "no_match"
            scores = _clear_score_selections(scores)
            primary_keys = ()
            mirror_keys = ()
        else:
            final_keys = _dedupe_keys(primary_keys + mirror_keys)
            status = "routed" if final_keys else "no_match"
            reason = None if final_keys else "no_destination"
            if not final_keys:
                scores = _clear_score_selections(scores)
        explanation = _build_explanation(
            content_mode,
            article.source_name,
            article.source_id,
            article.source_class,
            matches,
            suppression_matches,
            emitted_tags,
            expanded_tags,
            scores,
            primary_keys,
            mirror_keys,
            review_keys,
            final_keys,
            status,
            reason,
        )
        return RoutingDecision(
            content_mode=content_mode,
            matched_entries=matches,
            emitted_tags=tuple(sorted(emitted_tags)),
            expanded_tags=tuple(sorted(expanded_tags)),
            channel_scores=scores,
            selected_channel_keys=final_keys,
            decision_status=status,
            top_score=top_score,
            explanation=tuple(explanation),
            suppression_matches=suppression_matches,
            primary_channel_keys=primary_keys,
            mirror_channel_keys=mirror_keys,
            review_channel_keys=review_keys,
            final_channel_keys=final_keys,
            reason=reason,
        )


def _clear_score_selections(scores: tuple[ChannelScore, ...]) -> tuple[ChannelScore, ...]:
    return _set_score_selections(scores, set())


def _set_score_selections(scores: tuple[ChannelScore, ...], selected_keys: set[str]) -> tuple[ChannelScore, ...]:
    return tuple(
        ChannelScore(
            channel_key=score.channel_key,
            destination_class=score.destination_class,
            score=score.score,
            minimum_score=score.minimum_score,
            priority=score.priority,
            selected=score.channel_key in selected_keys,
            reasons=score.reasons,
        )
        for score in scores
    )


def _review_scores(rules, *, selected: bool) -> tuple[ChannelScore, ...]:
    return tuple(
        ChannelScore(
            channel_key=rule.channel_key,
            destination_class=rule.destination_class,
            score=0,
            minimum_score=rule.minimum_score,
            priority=rule.priority,
            selected=selected and rule.enabled,
            reasons=("review destination",) if rule.enabled else ("disabled",),
        )
        for rule in rules
    )


def _dedupe_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return tuple(deduped)


def _select_primary_keys(primary_scores: tuple[ChannelScore, ...], *, has_mirror: bool) -> tuple[str, ...]:
    candidates = tuple(score for score in primary_scores if score.selected)
    if not candidates:
        return ()
    selected = [candidates[0].channel_key]
    if has_mirror or len(candidates) == 1:
        return tuple(selected)
    top = candidates[0]
    secondary = candidates[1]
    if secondary.score >= SECONDARY_PRIMARY_MIN_SCORE and top.score - secondary.score <= SECONDARY_PRIMARY_MAX_GAP:
        selected.append(secondary.channel_key)
    return tuple(selected)


def _select_mirror_keys(
    mirror_candidates: tuple[ChannelScore, ...],
    primary_keys: tuple[str, ...],
    max_destinations: int,
) -> tuple[str, ...]:
    if not primary_keys:
        return ()
    remaining = max(0, max_destinations - len(primary_keys))
    return tuple(score.channel_key for score in mirror_candidates[:remaining])


def _content_mode(article: RoutingArticle) -> str:
    if article.summary and article.summary.strip():
        return "title_and_stub"
    return "title_only"


def _build_match_text(article: RoutingArticle) -> str:
    parts = [article.title]
    if article.summary:
        parts.append(article.summary[:SUMMARY_MATCH_LIMIT])
    slug_text = _url_slug_text(article.url)
    if slug_text:
        parts.append(slug_text)
    return "\n".join(part for part in parts if part)


def _url_slug_text(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path.replace("-", " ").replace("_", " ")
    return path


def _build_explanation(
    content_mode: str,
    source_name: str | None,
    source_id: str | None,
    source_class: str | None,
    matches,
    suppression_matches,
    emitted_tags: set[str],
    expanded_tags: set[str],
    scores,
    primary_channel_keys: tuple[str, ...],
    mirror_channel_keys: tuple[str, ...],
    review_channel_keys: tuple[str, ...],
    final_channel_keys: tuple[str, ...],
    status: str,
    reason: str | None,
) -> list[str]:
    lines: list[str] = [f"content_mode={content_mode}"]
    lines.append(f"source_name={source_name or 'unknown'}")
    lines.append(f"source_id={source_id or 'unknown'}")
    lines.append(f"source_class={source_class or 'unknown'}")
    if matches:
        match_text = ", ".join(f"{match.knowledge_entry_id} ({match.matched_alias})" for match in matches[:12])
        if len(matches) > 12:
            match_text += f", +{len(matches) - 12} more"
        lines.append(f"matched={match_text}")
    else:
        lines.append("matched=none")
    if suppression_matches:
        suppression_text = ", ".join(
            f"{match.suppression_id} ({match.matched_alias})" for match in suppression_matches[:12]
        )
        if len(suppression_matches) > 12:
            suppression_text += f", +{len(suppression_matches) - 12} more"
        lines.append(f"suppressions={suppression_text}")
    else:
        lines.append("suppressions=none")
    lines.append(f"emitted_tags={', '.join(sorted(emitted_tags)) or 'none'}")
    parent_only = sorted(expanded_tags - emitted_tags)
    lines.append(f"expanded_parent_tags={', '.join(parent_only) or 'none'}")
    lines.append(f"primary_channels={', '.join(primary_channel_keys) or 'none'}")
    lines.append(f"mirror_channels={', '.join(mirror_channel_keys) or 'none'}")
    lines.append(f"review_channels={', '.join(review_channel_keys) or 'none'}")
    lines.append(f"final_channels={', '.join(final_channel_keys) or 'none'}")
    top_scores = [score for score in scores if score.score > 0][:8]
    if top_scores:
        lines.append(
            "top_scores="
            + "; ".join(
                f"{score.channel_key}:{score.score}/{score.minimum_score}" for score in top_scores
            )
        )
    else:
        lines.append("top_scores=none")
    lines.append(f"decision={status}")
    if reason:
        lines.append(f"reason={reason}")
    return lines
