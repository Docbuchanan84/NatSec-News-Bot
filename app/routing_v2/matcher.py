from __future__ import annotations

import re
from collections.abc import Mapping

from app.routing_v2.models import BlockedEvidence, CompiledEvidenceRule, EvidenceCandidate

TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
SEPARATOR_RE = r"[\s\-_/.,'’:&]+"
LEFT_BOUNDARY = r"(?<![A-Za-z0-9])"
RIGHT_BOUNDARY = r"(?![A-Za-z0-9])"


def literal_to_regex(phrase: str) -> str:
    tokens = TOKEN_RE.findall(phrase)
    if not tokens:
        return r"a^"
    pieces = [_token_to_regex(token) for token in tokens]
    body = SEPARATOR_RE.join(pieces)
    return rf"{LEFT_BOUNDARY}{body}(?:['’]s)?{RIGHT_BOUNDARY}"


def find_evidence_candidates(
    fields: Mapping[str, str],
    rules: tuple[CompiledEvidenceRule, ...],
) -> tuple[EvidenceCandidate, ...]:
    candidates: list[EvidenceCandidate] = []
    for rule in rules:
        for field in rule.fields:
            value = fields.get(field) or ""
            if not value:
                continue
            for match in rule.pattern.finditer(value):
                candidates.append(
                    EvidenceCandidate(
                        rule=rule,
                        field=field,
                        text=match.group(0),
                        start=match.start(),
                        end=match.end(),
                    )
                )
    return tuple(candidates)


def accept_longest_matches(
    candidates: tuple[EvidenceCandidate, ...],
) -> tuple[tuple[EvidenceCandidate, ...], tuple[BlockedEvidence, ...]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.field,
            -item.span_length,
            -item.rule.priority,
            -item.rule.confidence,
            item.start,
            item.rule.id,
        ),
    )
    accepted: list[EvidenceCandidate] = []
    blocked: list[BlockedEvidence] = []
    for candidate in ordered:
        blocker, reason = _blocked_by(candidate, accepted)
        if blocker is not None:
            blocked.append(BlockedEvidence(candidate=candidate, blocker_id=blocker.rule.id, reason=reason))
            continue
        accepted.append(candidate)
    accepted.sort(key=lambda item: (item.field, item.start, item.end, item.rule.id))
    blocked.sort(key=lambda item: (item.candidate.field, item.candidate.start, item.candidate.rule.id))
    return tuple(accepted), tuple(blocked)


def _blocked_by(candidate: EvidenceCandidate, accepted: list[EvidenceCandidate]) -> tuple[EvidenceCandidate | None, str]:
    for existing in accepted:
        if candidate.field != existing.field:
            continue
        if _overlaps(candidate.start, candidate.end, existing.start, existing.end):
            return existing, "overlap"
        if candidate.rule.id in existing.rule.blocks:
            window_start = max(0, existing.start - existing.rule.block_window_before)
            window_end = existing.end + existing.rule.block_window_after
            if candidate.start < window_end and candidate.end > window_start:
                return existing, "blocked_by_context_window"
    return None, ""


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def _token_to_regex(token: str) -> str:
    if token.isalpha() and token.isupper() and 2 <= len(token) <= 5:
        return r"\.?".join(re.escape(char) for char in token) + r"\.?"
    escaped = re.escape(token)
    if token.isalpha() and len(token) > 3:
        if token.endswith("y"):
            stem = re.escape(token[:-1])
            return rf"(?:{escaped}|{stem}ies)"
        if token.endswith("s"):
            return escaped
        return rf"{escaped}s?"
    return escaped
