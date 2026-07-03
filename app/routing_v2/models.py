from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WeightedRoute:
    key: str
    destination_class: str = "primary"
    threshold: int = 25
    priority: int = 0
    enabled: bool = True
    pseudo: bool = False
    required_source_ids: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    required_source_classes: tuple[str, ...] = ()
    excluded_source_classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledEvidenceRule:
    id: str
    type: str
    pattern: re.Pattern[str]
    pattern_text: str
    scores: dict[str, int]
    fields: tuple[str, ...]
    priority: int = 0
    confidence: int = 0
    blocks: tuple[str, ...] = ()
    block_window_before: int = 0
    block_window_after: int = 0
    notes: str | None = None


@dataclass(frozen=True)
class SourceScoreRule:
    id: str
    source_ids: tuple[str, ...] = ()
    source_classes: tuple[str, ...] = ()
    source_name_pattern: re.Pattern[str] | None = None
    source_url_hosts: tuple[str, ...] = ()
    source_url_path_terms: tuple[str, ...] = ()
    source_url_path_patterns: tuple[re.Pattern[str], ...] = ()
    source_url_path_pattern_texts: tuple[str, ...] = ()
    url_bias_only: bool = True
    scores: dict[str, int] = field(default_factory=dict)
    priority: int = 0
    notes: str | None = None


@dataclass(frozen=True)
class MirrorRule:
    channel_key: str
    required_source_ids: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    required_source_classes: tuple[str, ...] = ()
    excluded_source_classes: tuple[str, ...] = ()
    enabled: bool = True
    priority: int = 0


@dataclass(frozen=True)
class WeightedRoutingConfig:
    version: int
    routes: tuple[WeightedRoute, ...]
    evidence_rules: tuple[CompiledEvidenceRule, ...]
    source_rules: tuple[SourceScoreRule, ...]
    mirror_rules: tuple[MirrorRule, ...]
    field_multipliers: dict[str, float]
    primary_threshold: int = 25
    review_threshold: int = 20
    noise_threshold: int = 35
    secondary_within_percent: int = 10
    max_primary_destinations: int = 2


@dataclass(frozen=True)
class EvidenceCandidate:
    rule: CompiledEvidenceRule
    field: str
    text: str
    start: int
    end: int

    @property
    def span_length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class AcceptedEvidence:
    candidate: EvidenceCandidate
    weighted_scores: dict[str, int]


@dataclass(frozen=True)
class BlockedEvidence:
    candidate: EvidenceCandidate
    blocker_id: str
    reason: str
