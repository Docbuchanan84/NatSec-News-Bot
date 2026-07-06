from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.routing.models import RoutingArticle, RoutingDecision

MAX_IMPORTANCE = 100
MAX_REASONS = 12


@dataclass(frozen=True)
class ImportanceTerm:
    term: str
    weight: int
    category: str = "watch"
    enabled: bool = True
    notes: str | None = None
    expires_at: datetime | None = None
    source: str = "human"
    last_reviewed_at: datetime | None = None


@dataclass(frozen=True)
class ImportanceConfig:
    watch_terms: tuple[ImportanceTerm, ...] = ()
    now: datetime | None = None
    recent_articles: tuple[Mapping[str, Any], ...] = ()


HIGH_IMPACT_CONCEPTS = {
    "ukraine_war": 18,
    "iran_war": 18,
    "gaza_war": 16,
    "lebanon_conflict": 15,
    "taiwan_strait": 20,
    "south_china_sea": 14,
    "strait_of_hormuz": 18,
    "bab_el_mandeb": 14,
    "india_security_crisis": 18,
}

TAG_WEIGHTS = {
    "active_conflict": 18,
    "attack": 12,
    "missile": 10,
    "drone": 8,
    "disaster": 10,
    "weather_alert": 8,
    "earthquake": 10,
    "wildfire": 8,
    "cyber": 9,
    "nuclear_weapon": 18,
    "strategic_weapon": 12,
    "nuclear_deterrence": 10,
    "icbm": 12,
    "slbm": 12,
    "intelligence": 5,
    "national_security": 5,
    "sanctions": 5,
    "diplomacy": 4,
    "humanitarian": 4,
    "government": 3,
    "legislation": 3,
    "election": 4,
}

SOURCE_CLASS_WEIGHTS = {
    "wire_service": 8,
    "official_us_defense": 7,
    "official_allied_defense": 7,
    "official_us_gov": 4,
    "official_allied_gov": 4,
    "think_tank": 3,
    "defense_media": 4,
    "osint": 5,
    "social_core": 4,
    "social_breaking_news": 8,
    "newsletter": 3,
}

DEFAULT_WATCH_TERMS = (
    ImportanceTerm("breaking news", 16, "urgency", source="default"),
    ImportanceTerm("breaking", 10, "urgency", source="default"),
    ImportanceTerm("urgent", 10, "urgency", source="default"),
    ImportanceTerm("developing", 7, "urgency", source="default"),
    ImportanceTerm("live updates", 6, "urgency", source="default"),
    ImportanceTerm("sunk", 28, "major_event", source="default"),
    ImportanceTerm("sinks", 28, "major_event", source="default"),
    ImportanceTerm("sank", 28, "major_event", source="default"),
    ImportanceTerm("shoots down", 20, "major_event", source="default"),
    ImportanceTerm("shot down", 20, "major_event", source="default"),
    ImportanceTerm("downed", 14, "major_event", source="default"),
    ImportanceTerm("killed", 16, "casualties", source="default"),
    ImportanceTerm("dead", 10, "casualties", source="default"),
    ImportanceTerm("deaths", 10, "casualties", source="default"),
    ImportanceTerm("wounded", 8, "casualties", source="default"),
    ImportanceTerm("injured", 8, "casualties", source="default"),
    ImportanceTerm("mass casualty", 28, "casualties", source="default"),
    ImportanceTerm("casualties", 10, "casualties", source="default"),
    ImportanceTerm("fatalities", 10, "casualties", source="default"),
    ImportanceTerm("invasion", 30, "escalation", source="default"),
    ImportanceTerm("invades", 30, "escalation", source="default"),
    ImportanceTerm("incursion", 12, "escalation", source="default"),
    ImportanceTerm("escalates", 10, "escalation", source="default"),
    ImportanceTerm("ceasefire", 10, "escalation", source="default"),
    ImportanceTerm("missile strike", 20, "attack", source="default"),
    ImportanceTerm("airstrike", 18, "attack", source="default"),
    ImportanceTerm("air strike", 18, "attack", source="default"),
    ImportanceTerm("drone attack", 16, "attack", source="default"),
    ImportanceTerm("attack", 5, "attack", source="default"),
    ImportanceTerm("strike", 5, "attack", source="default"),
    ImportanceTerm("strikes", 5, "attack", source="default"),
    ImportanceTerm("missile", 6, "weapons", source="default"),
    ImportanceTerm("drone", 5, "weapons", source="default"),
    ImportanceTerm("hypersonic", 12, "weapons", source="default"),
    ImportanceTerm("ballistic missile", 18, "weapons", source="default"),
    ImportanceTerm("nuclear", 20, "strategic", source="default"),
    ImportanceTerm("icbm", 18, "strategic", source="default"),
    ImportanceTerm("chemical weapon", 28, "strategic", source="default"),
    ImportanceTerm("evacuate", 10, "civilian_impact", source="default"),
    ImportanceTerm("evacuation", 10, "civilian_impact", source="default"),
    ImportanceTerm("blackout", 10, "civilian_impact", source="default"),
    ImportanceTerm("ransomware", 10, "cyber", source="default"),
    ImportanceTerm("zero-day", 16, "cyber", source="default"),
    ImportanceTerm("critical infrastructure", 12, "cyber", source="default"),
)

LOW_SIGNAL_CAP_EXEMPT_TAGS = {
    "active_conflict",
    "attack",
    "missile",
    "drone",
    "disaster",
    "weather_alert",
    "earthquake",
    "wildfire",
    "nuclear_weapon",
    "strategic_weapon",
}

ROUTINE_DAMPENER_PATTERNS = (
    ("roundup", re.compile(r"\b(roundup|weekly briefing|daily briefing|week in review)\b", re.IGNORECASE)),
    ("podcast", re.compile(r"\b(podcast|listen:|transcript)\b", re.IGNORECASE)),
    ("opinion", re.compile(r"\b(opinion|analysis|commentary|explainer)\b", re.IGNORECASE)),
    ("gallery", re.compile(r"\b(in pictures|photos of the week|photo essay)\b", re.IGNORECASE)),
    ("markets", re.compile(r"\b(shares rise|shares fall|earnings|stock market)\b", re.IGNORECASE)),
)

ACTION_TERMS = {
    "attack",
    "attacks",
    "strike",
    "strikes",
    "hit",
    "hits",
    "launch",
    "launches",
    "invade",
    "invades",
    "kill",
    "killed",
    "shoots",
    "sinks",
    "sunk",
    "seize",
    "seizes",
}
TARGET_TERMS = {
    "base",
    "airbase",
    "embassy",
    "port",
    "ship",
    "vessel",
    "tanker",
    "destroyer",
    "carrier",
    "submarine",
    "pipeline",
    "grid",
    "infrastructure",
    "capital",
    "airport",
    "nuclear",
}
MAJOR_ACTOR_TERMS = {
    "china",
    "russia",
    "iran",
    "north korea",
    "taiwan",
    "nato",
    "ukraine",
    "israel",
    "hamas",
    "hezbollah",
    "houthi",
    "pakistan",
    "india",
    "kashmir",
}
NUMBER_WORD_RE = re.compile(r"\b(\d{2,}|dozens|scores|hundreds|thousands)\b", re.IGNORECASE)
CASUALTY_RE = re.compile(r"\b(killed|dead|deaths|wounded|injured|casualties|fatalities)\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")


def apply_importance(
    decision: RoutingDecision,
    article: RoutingArticle,
    config: ImportanceConfig | None = None,
) -> RoutingDecision:
    score, reasons = score_importance(decision, article, config)
    return replace(decision, importance_score=score, importance_reasons=reasons)


def score_importance(
    decision: RoutingDecision,
    article: RoutingArticle,
    config: ImportanceConfig | None = None,
) -> tuple[int, tuple[str, ...]]:
    config = config or build_importance_config()
    now = _ensure_utc(config.now or datetime.now(UTC))
    score = 0
    reasons: list[str] = []
    text_score = 0
    match_ids = {match.knowledge_entry_id for match in decision.matched_entries}
    tags = set(decision.emitted_tags) | set(decision.expanded_tags)
    text = _article_text(article)
    words = _word_set(text)

    for concept, value in HIGH_IMPACT_CONCEPTS.items():
        if concept in match_ids:
            score += value
            reasons.append(f"concept +{value}: {concept}")

    for tag, value in TAG_WEIGHTS.items():
        if tag in tags:
            score += value
            reasons.append(f"tag +{value}: {tag}")

    route_value = _route_strength(decision)
    if route_value:
        score += route_value
        reasons.append(f"route_strength +{route_value}: top_score {decision.top_score}")

    match_value = _match_strength(decision)
    if match_value:
        score += match_value
        reasons.append(f"match_strength +{match_value}")

    source_class = (article.source_class or "").casefold()
    source_value = SOURCE_CLASS_WEIGHTS.get(source_class, 0)
    if source_value:
        score += source_value
        reasons.append(f"source_class +{source_value}: {source_class}")

    for term in _matched_watch_terms(text, config.watch_terms)[:8]:
        score += term.weight
        text_score += term.weight
        reasons.append(f"watch {_signed(term.weight)}: {term.term}")

    context_value = _context_score(text, words, tags)
    if context_value:
        score += context_value
        text_score += context_value
        reasons.append(f"context +{context_value}")

    casualty_value = _casualty_scale_score(text)
    if casualty_value:
        score += casualty_value
        text_score += casualty_value
        reasons.append(f"casualty_scale +{casualty_value}")

    recency_value = _recency_score(article)
    if recency_value:
        score += recency_value
        reasons.append(f"recency +{recency_value}")

    similarity_value, similarity_reason = _similarity_penalty(article, config.recent_articles, now=now)
    if similarity_value:
        score -= similarity_value
        reasons.append(f"similarity -{similarity_value}: {similarity_reason}")

    dampener_value, dampener_name = _routine_dampener(text, tags)
    if dampener_value:
        score -= dampener_value
        reasons.append(f"dampener -{dampener_value}: {dampener_name}")

    if decision.decision_status == "review":
        score += 1
        reasons.append("review +1")
    elif decision.decision_status not in {"routed", "review"}:
        if _has_critical_signal(text_score, tags):
            score = min(score, 60)
            reasons.append("unrouted_critical_cap 60")
        elif not (tags & LOW_SIGNAL_CAP_EXEMPT_TAGS):
            score = min(score, 20)
            reasons.append("low_signal_cap 20")

    return max(0, min(MAX_IMPORTANCE, score)), _limit_reasons(reasons)


def build_importance_config(
    watch_terms: Iterable[ImportanceTerm | Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    recent_articles: Iterable[Mapping[str, Any]] | None = None,
    include_defaults: bool = True,
) -> ImportanceConfig:
    merged: dict[str, ImportanceTerm] = {}
    normalized_now = _ensure_utc(now or datetime.now(UTC))
    if include_defaults:
        for term in DEFAULT_WATCH_TERMS:
            merged[normalize_watch_term(term.term)] = term
    for raw_term in watch_terms or ():
        term = _coerce_term(raw_term)
        normalized = normalize_watch_term(term.term)
        if not normalized:
            continue
        if term.expires_at is not None and _ensure_utc(term.expires_at) <= normalized_now and normalized in merged:
            continue
        if normalized:
            merged[normalized] = replace(term, term=normalized)
    return ImportanceConfig(
        watch_terms=tuple(
            term
            for term in merged.values()
            if term.enabled
            and term.weight != 0
            and (term.expires_at is None or _ensure_utc(term.expires_at) > normalized_now)
        ),
        now=now,
        recent_articles=tuple(recent_articles or ()),
    )


def default_importance_terms() -> tuple[ImportanceTerm, ...]:
    return DEFAULT_WATCH_TERMS


def normalize_watch_term(term: str) -> str:
    return re.sub(r"\s+", " ", str(term or "").strip().casefold())


def _coerce_term(raw_term: ImportanceTerm | Mapping[str, Any]) -> ImportanceTerm:
    if isinstance(raw_term, ImportanceTerm):
        return raw_term
    term = str(raw_term.get("term") or raw_term.get("normalized_term") or "").strip()
    weight = int(raw_term.get("weight") or 0)
    category = str(raw_term.get("category") or "watch").strip() or "watch"
    enabled = bool(raw_term.get("enabled", True))
    notes_raw = raw_term.get("notes")
    notes = str(notes_raw).strip() if notes_raw is not None else None
    expires_at = _parse_datetime_value(raw_term.get("expires_at"))
    last_reviewed_at = _parse_datetime_value(raw_term.get("last_reviewed_at"))
    source = str(raw_term.get("source") or "human").strip() or "human"
    return ImportanceTerm(
        term=term,
        weight=weight,
        category=category,
        enabled=enabled,
        notes=notes or None,
        expires_at=expires_at,
        source=source,
        last_reviewed_at=last_reviewed_at,
    )


def _article_text(article: RoutingArticle) -> str:
    return "\n".join(
        value
        for value in (
            article.title or "",
            article.summary or "",
            article.source_name or "",
        )
        if value
    )


def _word_set(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.casefold()))


def _matched_watch_terms(text: str, watch_terms: tuple[ImportanceTerm, ...]) -> tuple[ImportanceTerm, ...]:
    matches: list[ImportanceTerm] = []
    for term in watch_terms:
        if _term_matches(text, term.term):
            matches.append(term)
    matches.sort(key=lambda item: (-item.weight, -len(item.term), item.term))
    return tuple(matches)


def _term_matches(text: str, term: str) -> bool:
    if not term:
        return False
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text, flags=re.IGNORECASE) is not None


def _route_strength(decision: RoutingDecision) -> int:
    if decision.decision_status not in {"routed", "review"}:
        return 0
    if decision.top_score >= 14:
        return 10
    if decision.top_score >= 8:
        return 5
    selected = [score for score in decision.channel_scores if score.selected]
    if any(score.score >= score.minimum_score + 3 for score in selected):
        return 5
    return 0


def _match_strength(decision: RoutingDecision) -> int:
    if not decision.matched_entries:
        return 0
    best_priority = max((match.priority for match in decision.matched_entries), default=0)
    total_score = sum(max(0, match.score) for match in decision.matched_entries)
    value = 0
    if best_priority >= 20:
        value += 8
    elif best_priority >= 10:
        value += 4
    if total_score >= 5:
        value += 4
    return min(value, 12)


def _context_score(text: str, words: set[str], tags: set[str]) -> int:
    lowered = text.casefold()
    has_action = bool(words & ACTION_TERMS)
    has_target = bool(words & TARGET_TERMS)
    has_actor = any(_term_matches(lowered, actor) for actor in MAJOR_ACTOR_TERMS)
    value = 0
    if has_action and has_target:
        value += 10
    if has_action and has_actor:
        value += 6
    if has_action and tags & {"active_conflict", "national_security", "military", "attack"}:
        value += 5
    return min(value, 15)


def _casualty_scale_score(text: str) -> int:
    if not CASUALTY_RE.search(text):
        return 0
    if NUMBER_WORD_RE.search(text):
        return 10
    return 0


def _recency_score(article: RoutingArticle) -> int:
    if article.published_at is None or article.ingested_at is None:
        return 0
    if article.timestamp_status not in {"valid", "timezone_corrected"}:
        return 0
    published_at = _ensure_utc(article.published_at)
    ingested_at = _ensure_utc(article.ingested_at)
    age_hours = (ingested_at - published_at).total_seconds() / 3600
    if age_hours < 0:
        return 0
    if age_hours <= 0.5:
        return 18
    if age_hours <= 1.5:
        return 14
    if age_hours <= 3:
        return 8
    if age_hours <= 6:
        return 4
    if age_hours <= 24:
        return 1
    return 0


def _similarity_penalty(
    article: RoutingArticle,
    recent_articles: tuple[Mapping[str, Any], ...],
    *,
    now: datetime,
) -> tuple[int, str | None]:
    if not recent_articles:
        return 0, None
    current_title = normalize_watch_term(article.normalized_title or article.title)
    current_signature = normalize_watch_term(getattr(article, "title_signature", None) or _title_signature(article.title))
    current_cluster = str(getattr(article, "story_cluster_key", None) or "").strip()
    current_terms = _similarity_terms(article.title)
    best = 0
    reason: str | None = None
    for row in recent_articles:
        try:
            if article.article_id is not None and int(row.get("id") or 0) == int(article.article_id):
                continue
        except (TypeError, ValueError):
            pass
        row_time = _parse_datetime_value(row.get("normalized_published_at") or row.get("first_seen_at"))
        if row_time is not None and _ensure_utc(row_time) > now:
            continue
        row_title = normalize_watch_term(str(row.get("normalized_title") or row.get("title") or ""))
        row_signature = normalize_watch_term(str(row.get("title_signature") or _title_signature(str(row.get("title") or ""))))
        row_cluster = str(row.get("story_cluster_key") or "").strip()
        if current_title and row_title and current_title == row_title:
            best, reason = max((best, reason or ""), (12, "same normalized title"), key=lambda item: item[0])
            continue
        if current_cluster and row_cluster and current_cluster == row_cluster:
            best, reason = max((best, reason or ""), (8, "same story cluster"), key=lambda item: item[0])
            continue
        if current_signature and row_signature and current_signature == row_signature:
            best, reason = max((best, reason or ""), (8, "same title signature"), key=lambda item: item[0])
            continue
        row_terms = _similarity_terms(str(row.get("title") or row.get("normalized_title") or ""))
        overlap = _jaccard(current_terms, row_terms)
        if overlap >= 0.72 and len(current_terms | row_terms) >= 4:
            best, reason = max((best, reason or ""), (5, "similar title tokens"), key=lambda item: item[0])
    return best, reason


def _routine_dampener(text: str, tags: set[str]) -> tuple[int, str | None]:
    if tags & LOW_SIGNAL_CAP_EXEMPT_TAGS:
        return 0, None
    for name, pattern in ROUTINE_DAMPENER_PATTERNS:
        if pattern.search(text):
            return 12, name
    return 0, None


def _has_critical_signal(text_score: int, tags: set[str]) -> bool:
    return text_score >= 24 or bool(tags & LOW_SIGNAL_CAP_EXEMPT_TAGS)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime_value(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _title_signature(title: str | None) -> str:
    tokens = [token for token in TOKEN_RE.findall((title or "").casefold()) if token not in {"the", "and", "for", "with"}]
    return " ".join(tokens)


def _similarity_terms(title: str | None) -> set[str]:
    return {token for token in TOKEN_RE.findall((title or "").casefold()) if len(token) > 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _limit_reasons(reasons: list[str]) -> tuple[str, ...]:
    if not reasons:
        return ("no importance signals",)
    if len(reasons) <= MAX_REASONS:
        return tuple(reasons)
    kept = reasons[:MAX_REASONS]
    kept.append(f"additional_signals +{len(reasons) - MAX_REASONS}")
    return tuple(kept)
