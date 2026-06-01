from __future__ import annotations

import re
from functools import lru_cache

from app.routing.models import KnowledgeEntry, KnowledgeMatch


def match_knowledge_entries(text: str, entries: tuple[KnowledgeEntry, ...]) -> tuple[KnowledgeMatch, ...]:
    candidates: list[KnowledgeMatch] = []
    for entry in entries:
        for alias in entry.aliases:
            pattern = _alias_pattern(alias)
            for match in pattern.finditer(text):
                candidates.append(
                    KnowledgeMatch(
                        knowledge_entry_id=entry.id,
                        matched_alias=alias,
                        match_start=match.start(),
                        match_end=match.end(),
                        emitted_tags=entry.tags,
                        priority=entry.priority,
                        score=entry.score,
                    )
                )

    candidates.sort(
        key=lambda item: (
            -(item.match_end - item.match_start),
            -item.priority,
            item.match_start,
            item.knowledge_entry_id,
        )
    )
    selected: list[KnowledgeMatch] = []
    occupied: list[range] = []
    for candidate in candidates:
        span = range(candidate.match_start, candidate.match_end)
        if any(_overlaps(span, existing) for existing in occupied):
            continue
        selected.append(candidate)
        occupied.append(span)

    selected.sort(key=lambda item: (item.match_start, item.match_end, item.knowledge_entry_id))
    return tuple(selected)


@lru_cache(maxsize=4096)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias.strip())
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    escaped = escaped.replace(r"\-", r"[\s-]+")
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)


def _overlaps(left: range, right: range) -> bool:
    return left.start < right.stop and right.start < left.stop
