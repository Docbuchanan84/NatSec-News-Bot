from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaxonomyTag:
    tag: str
    parent_tags: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    priority: int = 0
    score: int = 1
    description: str | None = None


@dataclass(frozen=True)
class ChannelRule:
    channel_key: str
    enabled: bool
    minimum_score: int
    priority: int
    destination_class: str = "primary"
    profile: str | None = None
    required_tags: tuple[str, ...] = ()
    required_concepts: tuple[str, ...] = ()
    excluded_tags: tuple[str, ...] = ()
    excluded_concepts: tuple[str, ...] = ()
    concept_boosts: dict[str, int] = field(default_factory=dict)
    concept_penalties: dict[str, int] = field(default_factory=dict)
    suppress_when_tags_any: tuple[str, ...] = ()
    term_boosts: dict[str, int] = field(default_factory=dict)
    tag_boosts: dict[str, int] = field(default_factory=dict)
    term_penalties: dict[str, int] = field(default_factory=dict)
    tag_penalties: dict[str, int] = field(default_factory=dict)
    required_any: tuple[str, ...] = ()
    excluded_any: tuple[str, ...] = ()
    required_source_ids: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    required_source_classes: tuple[str, ...] = ()
    excluded_source_classes: tuple[str, ...] = ()
    required_source_any: tuple[str, ...] = ()
    excluded_source_any: tuple[str, ...] = ()
    source_biases: dict[str, int] = field(default_factory=dict)
    content_mode_adjustments: dict[str, int] = field(default_factory=dict)
    notes: str | None = None


@dataclass(frozen=True)
class RoutingConfig:
    taxonomy_version: int
    knowledge_base_version: int
    channels_version: int
    taxonomy: dict[str, TaxonomyTag]
    knowledge_entries: tuple[KnowledgeEntry, ...]
    channel_rules: tuple[ChannelRule, ...]
    suppression_entries: tuple["SuppressionEntry", ...] = ()
    max_destinations: int = 3
    max_primary_destinations: int | None = None
    review_tags: tuple[str, ...] = ("review_required", "ambiguous")
    skip_tags: tuple[str, ...] = ("skip_candidate",)


@dataclass(frozen=True)
class RoutingArticle:
    title: str
    summary: str | None = None
    source_name: str | None = None
    source_id: str | None = None
    source_class: str | None = None
    url: str | None = None
    article_id: int | None = None
    normalized_title: str | None = None
    routing_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeMatch:
    knowledge_entry_id: str
    matched_alias: str
    match_start: int
    match_end: int
    emitted_tags: tuple[str, ...]
    priority: int
    score: int


@dataclass(frozen=True)
class SuppressionEntry:
    id: str
    aliases: tuple[str, ...]
    action: str = "skip"
    unless_tags_any: tuple[str, ...] = ()
    priority: int = 0
    description: str | None = None


@dataclass(frozen=True)
class SuppressionMatch:
    suppression_id: str
    matched_alias: str
    match_start: int
    match_end: int
    action: str
    unless_tags_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelScore:
    channel_key: str
    destination_class: str
    score: int
    minimum_score: int
    priority: int
    selected: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RoutingDecision:
    content_mode: str
    matched_entries: tuple[KnowledgeMatch, ...]
    emitted_tags: tuple[str, ...]
    expanded_tags: tuple[str, ...]
    channel_scores: tuple[ChannelScore, ...]
    selected_channel_keys: tuple[str, ...]
    decision_status: str
    top_score: int
    explanation: tuple[str, ...]
    suppression_matches: tuple[SuppressionMatch, ...] = ()
    primary_channel_keys: tuple[str, ...] = ()
    mirror_channel_keys: tuple[str, ...] = ()
    review_channel_keys: tuple[str, ...] = ()
    final_channel_keys: tuple[str, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.final_channel_keys:
            object.__setattr__(self, "final_channel_keys", self.selected_channel_keys)
        if not self.selected_channel_keys and self.final_channel_keys:
            object.__setattr__(self, "selected_channel_keys", self.final_channel_keys)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "content_mode": self.content_mode,
            "matched_entries": [
                {
                    "knowledge_entry_id": match.knowledge_entry_id,
                    "matched_alias": match.matched_alias,
                    "match_start": match.match_start,
                    "match_end": match.match_end,
                    "emitted_tags": list(match.emitted_tags),
                    "priority": match.priority,
                    "score": match.score,
                }
                for match in self.matched_entries
            ],
            "emitted_tags": list(self.emitted_tags),
            "suppression_matches": [
                {
                    "suppression_id": match.suppression_id,
                    "matched_alias": match.matched_alias,
                    "match_start": match.match_start,
                    "match_end": match.match_end,
                    "action": match.action,
                    "unless_tags_any": list(match.unless_tags_any),
                }
                for match in self.suppression_matches
            ],
            "expanded_tags": list(self.expanded_tags),
            "channel_scores": [
                {
                    "channel_key": score.channel_key,
                    "destination_class": score.destination_class,
                    "score": score.score,
                    "minimum_score": score.minimum_score,
                    "priority": score.priority,
                    "selected": score.selected,
                    "reasons": list(score.reasons),
                }
                for score in self.channel_scores
            ],
            "selected_channel_keys": list(self.selected_channel_keys),
            "primary_channel_keys": list(self.primary_channel_keys),
            "mirror_channel_keys": list(self.mirror_channel_keys),
            "review_channel_keys": list(self.review_channel_keys),
            "final_channel_keys": list(self.final_channel_keys),
            "decision_status": self.decision_status,
            "reason": self.reason,
            "top_score": self.top_score,
            "explanation": list(self.explanation),
        }
