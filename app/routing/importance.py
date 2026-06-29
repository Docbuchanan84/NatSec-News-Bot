from __future__ import annotations

import re
from dataclasses import replace

from app.routing.models import RoutingArticle, RoutingDecision

MAX_IMPORTANCE = 10

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
}

TITLE_TERM_WEIGHTS = {
    "breaking": 1,
    "urgent": 1,
    "major": 1,
    "killed": 1,
    "dead": 1,
    "attack": 1,
    "strike": 1,
    "strikes": 1,
    "missile": 1,
    "drone": 1,
    "invasion": 1,
    "ceasefire": 1,
    "evacuate": 1,
    "evacuation": 1,
    "nuclear": 1,
}

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


def apply_importance(decision: RoutingDecision, article: RoutingArticle) -> RoutingDecision:
    score, reasons = score_importance(decision, article)
    return replace(decision, importance_score=score, importance_reasons=reasons)


def score_importance(decision: RoutingDecision, article: RoutingArticle) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    match_ids = {match.knowledge_entry_id for match in decision.matched_entries}
    tags = set(decision.emitted_tags) | set(decision.expanded_tags)

    for concept, value in HIGH_IMPACT_CONCEPTS.items():
        if concept in match_ids:
            score += value
            reasons.append(f"concept +{value}: {concept}")

    for tag, value in TAG_WEIGHTS.items():
        if tag in tags:
            score += value
            reasons.append(f"tag +{value}: {tag}")

    source_class = (article.source_class or "").casefold()
    source_value = SOURCE_CLASS_WEIGHTS.get(source_class, 0)
    if source_value:
        score += source_value
        reasons.append(f"source_class +{source_value}: {source_class}")

    title_terms = _matched_title_terms(article.title)
    for term in title_terms[:4]:
        value = TITLE_TERM_WEIGHTS[term]
        score += value
        reasons.append(f"title +{value}: {term}")

    if decision.decision_status == "review":
        score += 1
        reasons.append("review +1")
    elif decision.decision_status not in {"routed", "review"} and not (tags & LOW_SIGNAL_CAP_EXEMPT_TAGS):
        score = min(score, 2)
        reasons.append("low_signal_cap 2")

    return max(0, min(MAX_IMPORTANCE, score)), tuple(reasons) or ("no importance signals",)


def _matched_title_terms(title: str) -> tuple[str, ...]:
    words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    return tuple(term for term in TITLE_TERM_WEIGHTS if term in words)
