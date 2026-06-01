from __future__ import annotations

from urllib.parse import urlparse

from app.routing.matcher import match_knowledge_entries
from app.routing.models import ChannelScore, RoutingArticle, RoutingConfig, RoutingDecision
from app.routing.scorer import score_channels
from app.routing.taxonomy import expand_tags

SUMMARY_MATCH_LIMIT = 1000


class RoutingEngine:
    def __init__(self, config: RoutingConfig) -> None:
        self.config = config

    def route(self, article: RoutingArticle) -> RoutingDecision:
        content_mode = _content_mode(article)
        match_text = _build_match_text(article)
        matches = match_knowledge_entries(match_text, self.config.knowledge_entries)
        emitted_tags = {tag for match in matches for tag in match.emitted_tags}
        expanded_tags = expand_tags(emitted_tags, self.config.taxonomy)
        scores = score_channels(
            self.config.channel_rules,
            matches,
            emitted_tags,
            expanded_tags,
            content_mode,
            article.source_name,
            self.config.max_destinations,
        )
        selected_channel_keys = tuple(score.channel_key for score in scores if score.selected)
        top_score = max((score.score for score in scores), default=0)
        status = _decision_status(
            matched=bool(matches),
            selected=bool(selected_channel_keys),
            emitted_tags=emitted_tags,
            expanded_tags=expanded_tags,
            review_tags=set(self.config.review_tags),
            skip_tags=set(self.config.skip_tags),
        )
        if status != "routed":
            selected_channel_keys = ()
            scores = _clear_score_selections(scores)
        explanation = _build_explanation(
            content_mode,
            matches,
            emitted_tags,
            expanded_tags,
            scores,
            selected_channel_keys,
            status,
        )
        return RoutingDecision(
            content_mode=content_mode,
            matched_entries=matches,
            emitted_tags=tuple(sorted(emitted_tags)),
            expanded_tags=tuple(sorted(expanded_tags)),
            channel_scores=scores,
            selected_channel_keys=selected_channel_keys,
            decision_status=status,
            top_score=top_score,
            explanation=tuple(explanation),
        )


def _clear_score_selections(scores: tuple[ChannelScore, ...]) -> tuple[ChannelScore, ...]:
    return tuple(
        ChannelScore(
            channel_key=score.channel_key,
            score=score.score,
            minimum_score=score.minimum_score,
            priority=score.priority,
            selected=False,
            reasons=score.reasons,
        )
        for score in scores
    )


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


def _decision_status(
    matched: bool,
    selected: bool,
    emitted_tags: set[str],
    expanded_tags: set[str],
    review_tags: set[str],
    skip_tags: set[str],
) -> str:
    all_tags = emitted_tags | expanded_tags
    if all_tags & skip_tags:
        return "skipped"
    if all_tags & review_tags:
        return "review"
    if selected:
        return "routed"
    if not matched:
        return "no_match"
    return "review"


def _build_explanation(
    content_mode: str,
    matches,
    emitted_tags: set[str],
    expanded_tags: set[str],
    scores,
    selected_channel_keys: tuple[str, ...],
    status: str,
) -> list[str]:
    lines: list[str] = [f"content_mode={content_mode}"]
    if matches:
        match_text = ", ".join(f"{match.knowledge_entry_id} ({match.matched_alias})" for match in matches[:12])
        if len(matches) > 12:
            match_text += f", +{len(matches) - 12} more"
        lines.append(f"matched={match_text}")
    else:
        lines.append("matched=none")
    lines.append(f"emitted_tags={', '.join(sorted(emitted_tags)) or 'none'}")
    parent_only = sorted(expanded_tags - emitted_tags)
    lines.append(f"expanded_parent_tags={', '.join(parent_only) or 'none'}")
    if selected_channel_keys:
        lines.append(f"selected_channels={', '.join(selected_channel_keys)}")
    else:
        lines.append("selected_channels=none")
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
    return lines
