from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from urllib.parse import unquote, urlparse

from app.routing.models import ChannelScore, KnowledgeMatch, RoutingArticle, RoutingDecision
from app.routing_v2.matcher import accept_longest_matches, find_evidence_candidates
from app.routing_v2.models import (
    AcceptedEvidence,
    BlockedEvidence,
    MirrorRule,
    SourceScoreRule,
    WeightedRoute,
    WeightedRoutingConfig,
)

SUMMARY_MATCH_LIMIT = 1000
PATH_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class MatchedSourceScore:
    rule: SourceScoreRule
    labels: tuple[str, ...]
    has_non_url_match: bool = False
    has_url_match: bool = False


class WeightedRoutingEngine:
    def __init__(self, config: WeightedRoutingConfig) -> None:
        self.config = config
        self.routes = {route.key: route for route in config.routes}

    def route(self, article: RoutingArticle) -> RoutingDecision:
        fields = _article_fields(article)
        candidates = find_evidence_candidates(fields, self.config.evidence_rules)
        accepted_candidates, blocked = accept_longest_matches(candidates)
        accepted = tuple(self._accepted_evidence(candidate) for candidate in accepted_candidates)
        source_evidence = tuple(_matched_source_rules(article, self.config.source_rules))
        totals: dict[str, int] = defaultdict(int)
        reasons_by_route: dict[str, list[str]] = defaultdict(list)
        primary_evidence_routes: set[str] = set()

        for item in accepted:
            rule = item.candidate.rule
            for route_key, delta in item.weighted_scores.items():
                totals[route_key] += delta
                reasons_by_route[route_key].append(
                    f"{item.candidate.field} {delta:+}: {rule.id} ({item.candidate.text})"
                )
                if delta > 0:
                    primary_evidence_routes.add(route_key)

        for source_match in source_evidence:
            source_rule = source_match.rule
            source_label = ", ".join(source_match.labels) or "matched"
            url_only_bias = source_match.has_url_match and not source_match.has_non_url_match and source_rule.url_bias_only
            for route_key, delta in source_rule.scores.items():
                totals[route_key] += delta
                origin = "source_url" if source_match.has_url_match and not source_match.has_non_url_match else "source"
                reasons_by_route[route_key].append(f"{origin} {delta:+}: {source_rule.id} ({source_label})")
                if delta > 0 and not url_only_bias:
                    primary_evidence_routes.add(route_key)

        primary_scores = self._primary_scores(article, totals, reasons_by_route)
        review_score = self._route_score("review", totals, reasons_by_route)
        noise_score = self._route_score("noise", totals, reasons_by_route)
        primary_keys, status, reason = self._select_primary_routes(
            primary_scores,
            review_score,
            noise_score,
            primary_evidence_routes,
        )
        mirror_keys = self._select_mirrors(article, has_primary=bool(primary_keys) and status == "routed")
        final_keys = primary_keys + mirror_keys
        review_keys: tuple[str, ...] = ()
        if status == "review":
            review_keys = ("review",)
            final_keys = review_keys
        channel_scores = self._channel_scores(article, totals, reasons_by_route, set(final_keys), noise_score)
        top_score = max((score.score for score in channel_scores), default=0)
        explanation = self._explanation(
            article,
            accepted,
            blocked,
            source_evidence,
            primary_keys,
            mirror_keys,
            review_keys,
            final_keys,
            status,
            reason,
            channel_scores,
        )
        return RoutingDecision(
            content_mode="title_and_stub" if article.summary else "title_only",
            matched_entries=self._matched_entries(accepted, source_evidence),
            emitted_tags=(),
            expanded_tags=(),
            channel_scores=channel_scores,
            selected_channel_keys=final_keys,
            decision_status=status,
            top_score=top_score,
            explanation=tuple(explanation),
            primary_channel_keys=primary_keys,
            mirror_channel_keys=mirror_keys,
            review_channel_keys=review_keys,
            final_channel_keys=final_keys,
            reason=reason,
        )

    def _accepted_evidence(self, candidate) -> AcceptedEvidence:
        multiplier = self.config.field_multipliers.get(candidate.field, 1.0)
        weighted = {
            route_key: int(round(delta * multiplier))
            for route_key, delta in candidate.rule.scores.items()
            if int(round(delta * multiplier)) != 0
        }
        return AcceptedEvidence(candidate=candidate, weighted_scores=weighted)

    def _primary_scores(
        self,
        article: RoutingArticle,
        totals: dict[str, int],
        reasons_by_route: dict[str, list[str]],
    ) -> list[tuple[WeightedRoute, int, list[str]]]:
        scores: list[tuple[WeightedRoute, int, list[str]]] = []
        for route in self.config.routes:
            if not route.enabled or route.pseudo or route.destination_class != "primary":
                continue
            source_block_reason = _route_source_block_reason(route, article)
            if source_block_reason is not None:
                scores.append((route, 0, [source_block_reason]))
                continue
            score = int(totals.get(route.key, 0))
            scores.append((route, score, reasons_by_route.get(route.key, ["no score contributions"])))
        scores.sort(key=lambda item: (-item[1], -item[0].priority, item[0].key))
        return scores

    def _route_score(
        self,
        route_key: str,
        totals: dict[str, int],
        reasons_by_route: dict[str, list[str]],
    ) -> tuple[WeightedRoute | None, int, list[str]]:
        route = self.routes.get(route_key)
        return route, int(totals.get(route_key, 0)), reasons_by_route.get(route_key, ["no score contributions"])

    def _select_primary_routes(
        self,
        primary_scores: list[tuple[WeightedRoute, int, list[str]]],
        review_score,
        noise_score,
        primary_evidence_routes: set[str],
    ) -> tuple[tuple[str, ...], str, str | None]:
        eligible_scores = [item for item in primary_scores if item[0].key in primary_evidence_routes]
        best_route, best_score, _reasons = eligible_scores[0] if eligible_scores else (None, 0, [])
        _review_route, review_value, _review_reasons = review_score
        _noise_route, noise_value, _noise_reasons = noise_score
        if noise_value >= self.config.noise_threshold and noise_value >= best_score:
            return (), "review", "noise_candidate"
        if review_value >= self.config.review_threshold and review_value > best_score:
            return (), "review", "review_scored_highest"
        if best_route is None or best_score < best_route.threshold:
            return (), "review", "no_route_threshold"

        selected = [best_route.key]
        floor = best_score * (100 - self.config.secondary_within_percent) / 100
        for route, score, _ in eligible_scores[1:]:
            if len(selected) >= self.config.max_primary_destinations:
                break
            if score >= route.threshold and score >= floor:
                selected.append(route.key)
        return tuple(selected), "routed", None

    def _select_mirrors(self, article: RoutingArticle, *, has_primary: bool) -> tuple[str, ...]:
        if not has_primary:
            return ()
        selected: list[MirrorRule] = []
        for rule in self.config.mirror_rules:
            if rule.enabled and _mirror_rule_matches(rule, article):
                selected.append(rule)
        selected.sort(key=lambda item: (-item.priority, item.channel_key))
        return tuple(rule.channel_key for rule in selected)

    def _channel_scores(
        self,
        article: RoutingArticle,
        totals: dict[str, int],
        reasons_by_route: dict[str, list[str]],
        selected_keys: set[str],
        noise_score,
    ) -> tuple[ChannelScore, ...]:
        scores: list[ChannelScore] = []
        for route in self.config.routes:
            if not route.enabled:
                scores.append(
                    ChannelScore(
                        channel_key=route.key,
                        destination_class=route.destination_class,
                        score=0,
                        minimum_score=route.threshold,
                        priority=route.priority,
                        selected=False,
                        reasons=("disabled",),
                    )
                )
                continue
            source_block_reason = _route_source_block_reason(route, article) if route.destination_class == "primary" else None
            if source_block_reason is not None:
                scores.append(
                    ChannelScore(
                        channel_key=route.key,
                        destination_class=route.destination_class,
                        score=0,
                        minimum_score=route.threshold,
                        priority=route.priority,
                        selected=False,
                        reasons=(source_block_reason,),
                    )
                )
                continue
            score = int(totals.get(route.key, 0))
            selected = route.key in selected_keys or (route.key == "noise" and noise_score[1] >= self.config.noise_threshold)
            scores.append(
                ChannelScore(
                    channel_key=route.key,
                    destination_class=route.destination_class,
                    score=score,
                    minimum_score=route.threshold,
                    priority=route.priority,
                    selected=selected,
                    reasons=tuple(reasons_by_route.get(route.key, ["no score contributions"]))[:12],
                )
            )
        scores.sort(key=lambda item: (-item.score, -item.selected, -item.priority, item.channel_key))
        return tuple(scores)

    def _matched_entries(
        self,
        accepted: tuple[AcceptedEvidence, ...],
        source_evidence: tuple[MatchedSourceScore, ...],
    ) -> tuple[KnowledgeMatch, ...]:
        matches: list[KnowledgeMatch] = []
        for item in accepted:
            score = sum(max(0, value) for value in item.weighted_scores.values())
            matches.append(
                KnowledgeMatch(
                    knowledge_entry_id=item.candidate.rule.id,
                    matched_alias=item.candidate.text,
                    match_start=item.candidate.start,
                    match_end=item.candidate.end,
                    emitted_tags=(),
                    priority=item.candidate.rule.priority,
                    score=score,
                )
            )
        for source_match in source_evidence:
            rule = source_match.rule
            matches.append(
                KnowledgeMatch(
                    knowledge_entry_id=f"source:{rule.id}",
                    matched_alias=", ".join(source_match.labels) or rule.id,
                    match_start=0,
                    match_end=0,
                    emitted_tags=(),
                    priority=rule.priority,
                    score=sum(max(0, value) for value in rule.scores.values()),
                )
            )
        return tuple(matches)

    def _explanation(
        self,
        article: RoutingArticle,
        accepted: tuple[AcceptedEvidence, ...],
        blocked: tuple[BlockedEvidence, ...],
        source_evidence: tuple[MatchedSourceScore, ...],
        primary_keys: tuple[str, ...],
        mirror_keys: tuple[str, ...],
        review_keys: tuple[str, ...],
        final_keys: tuple[str, ...],
        status: str,
        reason: str | None,
        channel_scores: tuple[ChannelScore, ...],
    ) -> list[str]:
        lines = [
            "engine=weighted_v2",
            f"content_mode={'title_and_stub' if article.summary else 'title_only'}",
            f"source_name={article.source_name or 'unknown'}",
            f"source_id={article.source_id or 'unknown'}",
            f"source_class={article.source_class or 'unknown'}",
        ]
        if accepted:
            lines.append(
                "accepted="
                + "; ".join(
                    f"{item.candidate.field}:{item.candidate.rule.id}({item.candidate.text}) "
                    f"{_format_scores(item.weighted_scores)}"
                    for item in accepted[:12]
                )
            )
        else:
            lines.append("accepted=none")
        if blocked:
            lines.append(
                "blocked="
                + "; ".join(
                    f"{item.candidate.field}:{item.candidate.rule.id} by {item.blocker_id} ({item.reason})"
                    for item in blocked[:12]
                )
            )
        else:
            lines.append("blocked=none")
        lines.append(
            "source_scores="
            + (
                "; ".join(
                    f"{match.rule.id}({', '.join(match.labels) or 'matched'}) {_format_scores(match.rule.scores)}"
                    for match in source_evidence[:12]
                )
                if source_evidence
                else "none"
            )
        )
        lines.append(f"primary_channels={', '.join(primary_keys) or 'none'}")
        lines.append(f"mirror_channels={', '.join(mirror_keys) or 'none'}")
        lines.append(f"review_channels={', '.join(review_keys) or 'none'}")
        lines.append(f"final_channels={', '.join(final_keys) or 'none'}")
        top_scores = [score for score in channel_scores if score.score != 0][:10]
        lines.append(
            "top_scores="
            + (
                "; ".join(f"{score.channel_key}:{score.score}/{score.minimum_score}" for score in top_scores)
                if top_scores
                else "none"
            )
        )
        lines.append(f"decision={status}")
        if reason:
            lines.append(f"reason={reason}")
        return lines


def _article_fields(article: RoutingArticle) -> dict[str, str]:
    return {
        "title": article.title or "",
        "summary": (article.summary or "")[:SUMMARY_MATCH_LIMIT],
        "url_slug": _url_slug_text(article.url),
        "source_name": article.source_name or "",
    }


def _url_slug_text(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return parsed.path.replace("-", " ").replace("_", " ")


def _matched_source_rules(article: RoutingArticle, rules: tuple[SourceScoreRule, ...]) -> list[MatchedSourceScore]:
    source_id = (article.source_id or "").casefold()
    source_class = (article.source_class or "").casefold()
    source_name = article.source_name or ""
    source_host, source_path = _source_url_parts(article.source_url)
    matched: list[MatchedSourceScore] = []
    for rule in rules:
        labels: list[str] = []
        has_non_url_match = False
        has_url_match = False
        if rule.source_ids and source_id not in {value.casefold() for value in rule.source_ids}:
            continue
        if rule.source_ids:
            labels.append(f"source_id={article.source_id or 'unknown'}")
            has_non_url_match = True
        if rule.source_classes and source_class not in {value.casefold() for value in rule.source_classes}:
            continue
        if rule.source_classes:
            labels.append(f"source_class={article.source_class or 'unknown'}")
            has_non_url_match = True
        if rule.source_name_pattern is not None and not rule.source_name_pattern.search(source_name):
            continue
        if rule.source_name_pattern is not None:
            labels.append(f"source_name={article.source_name or 'unknown'}")
            has_non_url_match = True
        url_labels = _source_url_rule_labels(rule, source_host, source_path)
        if _has_source_url_criteria(rule):
            if not url_labels:
                continue
            labels.extend(url_labels)
            has_url_match = True
        if not (has_non_url_match or has_url_match):
            continue
        matched.append(
            MatchedSourceScore(
                rule=rule,
                labels=tuple(labels),
                has_non_url_match=has_non_url_match,
                has_url_match=has_url_match,
            )
        )
    return matched


def _source_url_parts(source_url: str | None) -> tuple[str, str]:
    if not source_url:
        return "", ""
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parsed.path or "/").casefold()
    return host, path


def _has_source_url_criteria(rule: SourceScoreRule) -> bool:
    return bool(rule.source_url_hosts or rule.source_url_path_terms or rule.source_url_path_patterns)


def _source_url_rule_labels(rule: SourceScoreRule, host: str, path: str) -> tuple[str, ...]:
    labels: list[str] = []
    if rule.source_url_hosts:
        if not host or host not in {value.casefold() for value in rule.source_url_hosts}:
            return ()
        labels.append(f"host={host}")
    if rule.source_url_path_terms:
        matched_terms = [term for term in rule.source_url_path_terms if _path_term_matches(term, path)]
        if not matched_terms:
            return ()
        labels.append("path_term=" + "|".join(matched_terms[:3]))
    if rule.source_url_path_patterns:
        matched_patterns = [
            pattern_text
            for pattern, pattern_text in zip(
                rule.source_url_path_patterns,
                rule.source_url_path_pattern_texts,
                strict=False,
            )
            if pattern.search(path)
        ]
        if not matched_patterns:
            return ()
        labels.append("path_regex=" + "|".join(matched_patterns[:2]))
    return tuple(labels)


def _path_term_matches(term: str, path: str) -> bool:
    term_tokens = PATH_TOKEN_RE.findall(term.casefold())
    if not term_tokens:
        return False
    path_tokens = PATH_TOKEN_RE.findall(path.casefold())
    if len(term_tokens) > len(path_tokens):
        return False
    window_size = len(term_tokens)
    return any(tuple(path_tokens[index : index + window_size]) == tuple(term_tokens) for index in range(len(path_tokens) - window_size + 1))


def _mirror_rule_matches(rule: MirrorRule, article: RoutingArticle) -> bool:
    source_id = (article.source_id or "unknown").casefold()
    source_class = (article.source_class or "unknown").casefold()
    required_ids = {value.casefold() for value in rule.required_source_ids}
    excluded_ids = {value.casefold() for value in rule.excluded_source_ids}
    required_classes = {value.casefold() for value in rule.required_source_classes}
    excluded_classes = {value.casefold() for value in rule.excluded_source_classes}
    if required_ids and source_id not in required_ids:
        return False
    if source_id in excluded_ids:
        return False
    if required_classes and source_class not in required_classes:
        return False
    if source_class in excluded_classes:
        return False
    return True


def _route_source_block_reason(route: WeightedRoute, article: RoutingArticle) -> str | None:
    source_id = (article.source_id or "unknown").casefold()
    source_class = (article.source_class or "unknown").casefold()
    required_ids = {value.casefold() for value in route.required_source_ids}
    excluded_ids = {value.casefold() for value in route.excluded_source_ids}
    required_classes = {value.casefold() for value in route.required_source_classes}
    excluded_classes = {value.casefold() for value in route.excluded_source_classes}
    if required_ids and source_id not in required_ids:
        return "required_source_ids not met"
    if source_id in excluded_ids:
        return "excluded_source_ids matched"
    if required_classes and source_class not in required_classes:
        return "required_source_classes not met"
    if source_class in excluded_classes:
        return "excluded_source_classes matched"
    return None


def _format_scores(scores: dict[str, int]) -> str:
    if not scores:
        return "{}"
    return "{" + ", ".join(f"{key}:{value:+}" for key, value in sorted(scores.items())) + "}"
