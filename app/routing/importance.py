from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.routing.models import RoutingArticle, RoutingDecision

MAX_IMPORTANCE = 10
MAX_REASONS = 12


@dataclass(frozen=True)
class ImportanceTerm:
    term: str
    weight: int
    category: str = "watch"
    enabled: bool = True
    notes: str | None = None


@dataclass(frozen=True)
class ImportanceConfig:
    watch_terms: tuple[ImportanceTerm, ...] = ()
    now: datetime | None = None


HIGH_IMPACT_CONCEPTS = {
    "ukraine_war": 3,
    "iran_war": 3,
    "gaza_war": 3,
    "lebanon_conflict": 3,
    "taiwan_strait": 3,
    "south_china_sea": 2,
    "strait_of_hormuz": 3,
    "bab_el_mandeb": 2,
    "india_security_crisis": 3,
}

TAG_WEIGHTS = {
    "active_conflict": 3,
    "attack": 2,
    "missile": 2,
    "drone": 2,
    "disaster": 2,
    "weather_alert": 2,
    "earthquake": 2,
    "wildfire": 2,
    "cyber": 2,
    "nuclear_weapon": 3,
    "strategic_weapon": 2,
    "nuclear_deterrence": 2,
    "icbm": 2,
    "slbm": 2,
    "intelligence": 1,
    "national_security": 1,
    "sanctions": 1,
    "diplomacy": 1,
    "humanitarian": 1,
    "government": 1,
    "legislation": 1,
    "election": 1,
}

SOURCE_CLASS_WEIGHTS = {
    "wire_service": 2,
    "official_us_defense": 2,
    "official_allied_defense": 2,
    "official_us_gov": 1,
    "official_allied_gov": 1,
    "think_tank": 1,
    "defense_media": 1,
    "osint": 1,
    "social_core": 1,
    "social_breaking_news": 2,
    "newsletter": 1,
}

DEFAULT_WATCH_TERMS = (
    ImportanceTerm("breaking news", 2, "urgency"),
    ImportanceTerm("breaking", 1, "urgency"),
    ImportanceTerm("urgent", 1, "urgency"),
    ImportanceTerm("developing", 1, "urgency"),
    ImportanceTerm("live updates", 1, "urgency"),
    ImportanceTerm("sunk", 4, "major_event"),
    ImportanceTerm("sinks", 4, "major_event"),
    ImportanceTerm("sank", 4, "major_event"),
    ImportanceTerm("shoots down", 3, "major_event"),
    ImportanceTerm("shot down", 3, "major_event"),
    ImportanceTerm("downed", 2, "major_event"),
    ImportanceTerm("killed", 3, "casualties"),
    ImportanceTerm("dead", 2, "casualties"),
    ImportanceTerm("deaths", 2, "casualties"),
    ImportanceTerm("wounded", 2, "casualties"),
    ImportanceTerm("injured", 2, "casualties"),
    ImportanceTerm("mass casualty", 4, "casualties"),
    ImportanceTerm("casualties", 2, "casualties"),
    ImportanceTerm("fatalities", 2, "casualties"),
    ImportanceTerm("invasion", 4, "escalation"),
    ImportanceTerm("invades", 4, "escalation"),
    ImportanceTerm("incursion", 2, "escalation"),
    ImportanceTerm("escalates", 2, "escalation"),
    ImportanceTerm("ceasefire", 2, "escalation"),
    ImportanceTerm("missile strike", 3, "attack"),
    ImportanceTerm("airstrike", 3, "attack"),
    ImportanceTerm("air strike", 3, "attack"),
    ImportanceTerm("drone attack", 3, "attack"),
    ImportanceTerm("attack", 1, "attack"),
    ImportanceTerm("strike", 1, "attack"),
    ImportanceTerm("strikes", 1, "attack"),
    ImportanceTerm("missile", 1, "weapons"),
    ImportanceTerm("drone", 1, "weapons"),
    ImportanceTerm("hypersonic", 2, "weapons"),
    ImportanceTerm("ballistic missile", 3, "weapons"),
    ImportanceTerm("nuclear", 3, "strategic"),
    ImportanceTerm("icbm", 3, "strategic"),
    ImportanceTerm("chemical weapon", 4, "strategic"),
    ImportanceTerm("evacuate", 2, "civilian_impact"),
    ImportanceTerm("evacuation", 2, "civilian_impact"),
    ImportanceTerm("blackout", 2, "civilian_impact"),
    ImportanceTerm("ransomware", 2, "cyber"),
    ImportanceTerm("zero-day", 3, "cyber"),
    ImportanceTerm("critical infrastructure", 2, "cyber"),
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
        reasons.append(f"watch +{term.weight}: {term.term}")

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

    dampener_value, dampener_name = _routine_dampener(text, tags)
    if dampener_value:
        score -= dampener_value
        reasons.append(f"dampener -{dampener_value}: {dampener_name}")

    if decision.decision_status == "review":
        score += 1
        reasons.append("review +1")
    elif decision.decision_status not in {"routed", "review"}:
        if _has_critical_signal(text_score, tags):
            score = min(score, 6)
            reasons.append("unrouted_critical_cap 6")
        elif not (tags & LOW_SIGNAL_CAP_EXEMPT_TAGS):
            score = min(score, 2)
            reasons.append("low_signal_cap 2")

    return max(0, min(MAX_IMPORTANCE, score)), _limit_reasons(reasons)


def build_importance_config(
    watch_terms: Iterable[ImportanceTerm | Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    include_defaults: bool = True,
) -> ImportanceConfig:
    merged: dict[str, ImportanceTerm] = {}
    if include_defaults:
        for term in DEFAULT_WATCH_TERMS:
            merged[normalize_watch_term(term.term)] = term
    for raw_term in watch_terms or ():
        term = _coerce_term(raw_term)
        normalized = normalize_watch_term(term.term)
        if normalized:
            merged[normalized] = replace(term, term=normalized)
    return ImportanceConfig(
        watch_terms=tuple(term for term in merged.values() if term.enabled and term.weight > 0),
        now=now,
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
    return ImportanceTerm(term=term, weight=weight, category=category, enabled=enabled, notes=notes or None)


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
        return 2
    if decision.top_score >= 8:
        return 1
    selected = [score for score in decision.channel_scores if score.selected]
    if any(score.score >= score.minimum_score + 3 for score in selected):
        return 1
    return 0


def _match_strength(decision: RoutingDecision) -> int:
    if not decision.matched_entries:
        return 0
    best_priority = max((match.priority for match in decision.matched_entries), default=0)
    total_score = sum(max(0, match.score) for match in decision.matched_entries)
    value = 0
    if best_priority >= 20:
        value += 2
    elif best_priority >= 10:
        value += 1
    if total_score >= 5:
        value += 1
    return min(value, 3)


def _context_score(text: str, words: set[str], tags: set[str]) -> int:
    lowered = text.casefold()
    has_action = bool(words & ACTION_TERMS)
    has_target = bool(words & TARGET_TERMS)
    has_actor = any(_term_matches(lowered, actor) for actor in MAJOR_ACTOR_TERMS)
    value = 0
    if has_action and has_target:
        value += 2
    if has_action and has_actor:
        value += 1
    if has_action and tags & {"active_conflict", "national_security", "military", "attack"}:
        value += 1
    return min(value, 3)


def _casualty_scale_score(text: str) -> int:
    if not CASUALTY_RE.search(text):
        return 0
    if NUMBER_WORD_RE.search(text):
        return 2
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
    if age_hours <= 1:
        return 2
    if age_hours <= 6:
        return 1
    return 0


def _routine_dampener(text: str, tags: set[str]) -> tuple[int, str | None]:
    if tags & LOW_SIGNAL_CAP_EXEMPT_TAGS:
        return 0, None
    for name, pattern in ROUTINE_DAMPENER_PATTERNS:
        if pattern.search(text):
            return 2, name
    return 0, None


def _has_critical_signal(text_score: int, tags: set[str]) -> bool:
    return text_score >= 4 or bool(tags & LOW_SIGNAL_CAP_EXEMPT_TAGS)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _limit_reasons(reasons: list[str]) -> tuple[str, ...]:
    if not reasons:
        return ("no importance signals",)
    if len(reasons) <= MAX_REASONS:
        return tuple(reasons)
    kept = reasons[:MAX_REASONS]
    kept.append(f"additional_signals +{len(reasons) - MAX_REASONS}")
    return tuple(kept)
