from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.models import AppConfig
from app.routing.models import RoutingArticle, RoutingDecision
from app.routing_v2 import WeightedRoutingConfigError, WeightedRoutingEngine, load_weighted_routing_config
from app.routing_v2.matcher import literal_to_regex

DEFAULT_FIELDS = ("title", "summary", "url_slug")
VALID_FIELDS = {"title", "summary", "url_slug", "source_name"}
VALID_RULE_TYPES = {"literal", "pattern"}
SCORE_VALUE_RE = re.compile(r"^[+-]?\d+$")
SLUG_RE = re.compile(r"[^a-z0-9]+")


class RoutingTeachError(ValueError):
    pass


@dataclass(frozen=True)
class TeachingRule:
    id: str
    term: str
    rule_type: str
    fields: tuple[str, ...]
    scores: dict[str, int]
    notes: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.rule_type,
            "scores": dict(sorted(self.scores.items())),
            "fields": list(self.fields),
            "priority": 75,
        }
        if self.rule_type == "literal":
            data["phrase"] = self.term
        else:
            data["pattern"] = self.term
        if self.notes:
            data["notes"] = self.notes
        return data


@dataclass(frozen=True)
class SourceUrlTeachingRule:
    id: str
    scores: dict[str, int]
    source_url_hosts: tuple[str, ...] = ()
    source_url_path_terms: tuple[str, ...] = ()
    source_url_path_patterns: tuple[str, ...] = ()
    notes: str | None = None
    url_bias_only: bool = True

    @property
    def term(self) -> str:
        parts: list[str] = []
        if self.source_url_hosts:
            parts.append("host=" + ", ".join(self.source_url_hosts))
        if self.source_url_path_terms:
            parts.append("path_term=" + ", ".join(self.source_url_path_terms))
        if self.source_url_path_patterns:
            parts.append("path_regex=" + ", ".join(self.source_url_path_patterns))
        return "; ".join(parts) or "feed URL"

    @property
    def rule_type(self) -> str:
        return "source_url"

    @property
    def fields(self) -> tuple[str, ...]:
        return ()

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "scores": dict(sorted(self.scores.items())),
            "priority": 70,
            "url_bias_only": self.url_bias_only,
        }
        if self.source_url_hosts:
            data["source_url_hosts"] = list(self.source_url_hosts)
        if self.source_url_path_terms:
            data["source_url_path_terms"] = list(self.source_url_path_terms)
        if self.source_url_path_patterns:
            data["source_url_path_patterns"] = list(self.source_url_path_patterns)
        if self.notes:
            data["notes"] = self.notes
        return data


@dataclass(frozen=True)
class TeachingPreview:
    rule: TeachingRule | SourceUrlTeachingRule
    before: RoutingDecision
    after: RoutingDecision


@dataclass(frozen=True)
class TeachingApplyResult:
    rule: TeachingRule | SourceUrlTeachingRule
    before: RoutingDecision
    after: RoutingDecision
    backup_path: Path


@dataclass(frozen=True)
class DuplicateEvidenceRule:
    index: int
    rule_id: str
    match_type: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class DuplicateTeachingPreview:
    submitted: TeachingRule
    duplicate: DuplicateEvidenceRule
    before: RoutingDecision
    merge_after: RoutingDecision
    replace_after: RoutingDecision
    current_json: dict[str, Any]
    merge_json: dict[str, Any]
    replace_json: dict[str, Any]


def parse_score_string(value: str, allowed_routes: set[str], route_aliases: dict[str, str] | None = None) -> dict[str, int]:
    raw_parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
    if not raw_parts:
        raise RoutingTeachError("Scores must look like `sea:+35, air:-5`.")
    scores: dict[str, int] = {}
    errors: list[str] = []
    aliases = {_normalize_route_input(key): route for key, route in (route_aliases or {}).items()}
    for part in raw_parts:
        if ":" not in part:
            errors.append(f"`{part}` must look like `route:+10` or `route:-10`.")
            continue
        raw_route_key, raw_score = [piece.strip() for piece in part.split(":", 1)]
        if not raw_route_key or not SCORE_VALUE_RE.match(raw_score):
            errors.append(f"`{part}` must look like `route:+10` or `route:-10`.")
            continue
        route_key = _resolve_route_key(raw_route_key, allowed_routes, aliases)
        if route_key not in allowed_routes:
            errors.append(f"`{raw_route_key}` is not a known route. Try one of: {_route_hint(allowed_routes, aliases)}.")
            continue
        score = int(raw_score)
        if score == 0:
            errors.append(f"`{route_key}` must use a non-zero score.")
            continue
        if route_key in scores:
            errors.append(f"`{route_key}` appears more than once.")
            continue
        scores[route_key] = score
    if errors:
        raise RoutingTeachError(" ".join(errors))
    return scores


def parse_fields(value: str | None) -> tuple[str, ...]:
    if not value or not value.strip():
        return DEFAULT_FIELDS
    fields: list[str] = []
    errors: list[str] = []
    for item in value.split(","):
        field = item.strip()
        if not field:
            continue
        if field not in VALID_FIELDS:
            errors.append(f"`{field}` is not a valid field.")
            continue
        if field not in fields:
            fields.append(field)
    if errors:
        raise RoutingTeachError(" ".join(errors))
    if not fields:
        raise RoutingTeachError("At least one field is required.")
    return tuple(fields)


def parse_rule_type(value: str | None) -> str:
    rule_type = (value or "literal").strip().casefold()
    if rule_type == "regex":
        rule_type = "pattern"
    if rule_type not in VALID_RULE_TYPES:
        raise RoutingTeachError("Type must be `literal`, `pattern`, or `regex`.")
    return rule_type


def make_teaching_rule(
    *,
    term: str,
    scores: str,
    app_config: AppConfig,
    rule_type: str | None = None,
    fields: str | None = None,
    notes: str | None = None,
) -> TeachingRule:
    clean_term = " ".join(str(term or "").strip().split())
    if not clean_term:
        raise RoutingTeachError("Term is required.")
    parsed_type = parse_rule_type(rule_type)
    parsed_fields = parse_fields(fields)
    allowed_routes = _allowed_route_keys(app_config)
    root = _routing_root(app_config)
    parsed_scores = parse_score_string(scores, allowed_routes, _route_aliases(root, allowed_routes))
    evidence = _read_evidence(root)
    rule_id = _unique_rule_id(clean_term, _existing_rule_ids(evidence))
    rule = TeachingRule(
        id=rule_id,
        term=clean_term,
        rule_type=parsed_type,
        fields=parsed_fields,
        scores=parsed_scores,
        notes=" ".join(notes.strip().split()) if notes and notes.strip() else None,
    )
    _validate_candidate(root, app_config, _candidate_evidence(evidence, rule))
    return rule


def make_source_url_teaching_rule(
    *,
    scores: str,
    app_config: AppConfig,
    host: str | None = None,
    path_term: str | None = None,
    path_regex: str | None = None,
    source_url: str | None = None,
    notes: str | None = None,
    url_bias_only: bool = True,
) -> SourceUrlTeachingRule:
    parsed_hosts = tuple(dict.fromkeys(_parse_csv_values(host)))
    if not parsed_hosts and source_url:
        inferred_host = _host_from_url(source_url)
        if inferred_host:
            parsed_hosts = (inferred_host,)
    parsed_hosts = tuple(_normalize_host(value) for value in parsed_hosts if _normalize_host(value))
    parsed_terms = tuple(_normalize_path_term(value) for value in _parse_csv_values(path_term) if _normalize_path_term(value))
    parsed_patterns = (path_regex.strip(),) if path_regex and path_regex.strip() else ()
    if not (parsed_hosts or parsed_terms or parsed_patterns):
        raise RoutingTeachError("Provide host, path_term, path_regex, or an article/message that can infer a feed host.")
    allowed_routes = _allowed_route_keys(app_config)
    root = _routing_root(app_config)
    parsed_scores = parse_score_string(scores, allowed_routes, _route_aliases(root, allowed_routes))
    sources = _read_sources(root)
    base = " ".join(parsed_hosts or parsed_terms or ("source-url",))
    rule = SourceUrlTeachingRule(
        id=_unique_source_rule_id(base, _existing_source_rule_ids(sources)),
        scores=parsed_scores,
        source_url_hosts=parsed_hosts,
        source_url_path_terms=parsed_terms,
        source_url_path_patterns=parsed_patterns,
        notes=" ".join(notes.strip().split()) if notes and notes.strip() else None,
        url_bias_only=bool(url_bias_only),
    )
    _validate_source_candidate(root, app_config, _candidate_sources(sources, rule))
    return rule


def preview_teaching_rule(article: RoutingArticle, app_config: AppConfig, rule: TeachingRule) -> TeachingPreview:
    root = _routing_root(app_config)
    try:
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    evidence = _read_evidence(root)
    try:
        candidate_config = _load_candidate_config(root, app_config, _candidate_evidence(evidence, rule))
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    return TeachingPreview(rule=rule, before=before, after=after)


def preview_source_url_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: SourceUrlTeachingRule,
) -> TeachingPreview:
    root = _routing_root(app_config)
    try:
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    sources = _read_sources(root)
    try:
        candidate_config = _load_candidate_config(root, app_config, sources=_candidate_sources(sources, rule))
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    return TeachingPreview(rule=rule, before=before, after=after)


def preview_duplicate_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: TeachingRule,
) -> DuplicateTeachingPreview | None:
    root = _routing_root(app_config)
    evidence = _read_evidence(root)
    duplicate = _find_duplicate_rule(evidence, rule)
    if duplicate is None:
        return None
    before = _route_current(article, root, app_config)
    merge_json = _merge_rule_json(duplicate.raw, rule)
    replace_json = _replace_rule_json(duplicate.raw, rule)
    try:
        merge_config = _load_candidate_config(root, app_config, _candidate_evidence_update(evidence, duplicate.index, merge_json))
        replace_config = _load_candidate_config(root, app_config, _candidate_evidence_update(evidence, duplicate.index, replace_json))
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return DuplicateTeachingPreview(
        submitted=rule,
        duplicate=duplicate,
        before=before,
        merge_after=WeightedRoutingEngine(merge_config).route(article),
        replace_after=WeightedRoutingEngine(replace_config).route(article),
        current_json=json.loads(json.dumps(duplicate.raw)),
        merge_json=merge_json,
        replace_json=replace_json,
    )


def preview_duplicate_source_url_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: SourceUrlTeachingRule,
) -> DuplicateTeachingPreview | None:
    root = _routing_root(app_config)
    sources = _read_sources(root)
    duplicate = _find_duplicate_source_rule(sources, rule)
    if duplicate is None:
        return None
    before = _route_current(article, root, app_config)
    merge_json = _merge_source_rule_json(duplicate.raw, rule)
    replace_json = _replace_source_rule_json(duplicate.raw, rule)
    try:
        merge_config = _load_candidate_config(root, app_config, sources=_candidate_sources_update(sources, duplicate.index, merge_json))
        replace_config = _load_candidate_config(root, app_config, sources=_candidate_sources_update(sources, duplicate.index, replace_json))
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return DuplicateTeachingPreview(
        submitted=rule,
        duplicate=duplicate,
        before=before,
        merge_after=WeightedRoutingEngine(merge_config).route(article),
        replace_after=WeightedRoutingEngine(replace_config).route(article),
        current_json=json.loads(json.dumps(duplicate.raw)),
        merge_json=merge_json,
        replace_json=replace_json,
    )


def apply_teaching_rule(article: RoutingArticle, app_config: AppConfig, rule: TeachingRule) -> TeachingApplyResult:
    root = _routing_root(app_config)
    evidence_path = root / "evidence.json"
    evidence = _read_evidence(root)
    if rule.id in _existing_rule_ids(evidence):
        rule = TeachingRule(
            id=_unique_rule_id(rule.term, _existing_rule_ids(evidence)),
            term=rule.term,
            rule_type=rule.rule_type,
            fields=rule.fields,
            scores=rule.scores,
            notes=rule.notes,
        )
    candidate_evidence = _candidate_evidence(evidence, rule)
    try:
        candidate_config = _load_candidate_config(root, app_config, candidate_evidence)
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    backup_path = latest_backup_path(root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(evidence_path, backup_path)
    _atomic_write_json(evidence_path, candidate_evidence)
    try:
        load_weighted_routing_config(root, app_config)
    except WeightedRoutingConfigError as exc:
        restore_latest_backup(root)
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return TeachingApplyResult(rule=rule, before=before, after=after, backup_path=backup_path)


def apply_source_url_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: SourceUrlTeachingRule,
) -> TeachingApplyResult:
    root = _routing_root(app_config)
    sources_path = root / "sources.json"
    sources = _read_sources(root)
    if rule.id in _existing_source_rule_ids(sources):
        rule = SourceUrlTeachingRule(
            id=_unique_source_rule_id(rule.term, _existing_source_rule_ids(sources)),
            scores=rule.scores,
            source_url_hosts=rule.source_url_hosts,
            source_url_path_terms=rule.source_url_path_terms,
            source_url_path_patterns=rule.source_url_path_patterns,
            notes=rule.notes,
            url_bias_only=rule.url_bias_only,
        )
    candidate_sources = _candidate_sources(sources, rule)
    try:
        candidate_config = _load_candidate_config(root, app_config, sources=candidate_sources)
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    backup_path = latest_source_backup_path(root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sources_path, backup_path)
    _atomic_write_json(sources_path, candidate_sources)
    try:
        load_weighted_routing_config(root, app_config)
    except WeightedRoutingConfigError as exc:
        restore_latest_backup(root)
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return TeachingApplyResult(rule=rule, before=before, after=after, backup_path=backup_path)


def apply_duplicate_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: TeachingRule,
    mode: str,
) -> TeachingApplyResult:
    if mode not in {"merge", "replace"}:
        raise RoutingTeachError("Duplicate handling mode must be `merge` or `replace`.")
    root = _routing_root(app_config)
    evidence_path = root / "evidence.json"
    evidence = _read_evidence(root)
    duplicate = _find_duplicate_rule(evidence, rule)
    if duplicate is None:
        raise RoutingTeachError("The duplicate rule was not found. Preview again before applying.")
    updated_rule_json = _merge_rule_json(duplicate.raw, rule) if mode == "merge" else _replace_rule_json(duplicate.raw, rule)
    candidate_evidence = _candidate_evidence_update(evidence, duplicate.index, updated_rule_json)
    try:
        candidate_config = _load_candidate_config(root, app_config, candidate_evidence)
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    backup_path = latest_backup_path(root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(evidence_path, backup_path)
    _atomic_write_json(evidence_path, candidate_evidence)
    try:
        load_weighted_routing_config(root, app_config)
    except WeightedRoutingConfigError as exc:
        restore_latest_backup(root)
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return TeachingApplyResult(rule=_teaching_rule_from_json(updated_rule_json), before=before, after=after, backup_path=backup_path)


def apply_duplicate_source_url_teaching_rule(
    article: RoutingArticle,
    app_config: AppConfig,
    rule: SourceUrlTeachingRule,
    mode: str,
) -> TeachingApplyResult:
    if mode not in {"merge", "replace"}:
        raise RoutingTeachError("Duplicate handling mode must be `merge` or `replace`.")
    root = _routing_root(app_config)
    sources_path = root / "sources.json"
    sources = _read_sources(root)
    duplicate = _find_duplicate_source_rule(sources, rule)
    if duplicate is None:
        raise RoutingTeachError("The duplicate feed URL rule was not found. Preview again before applying.")
    updated_rule_json = _merge_source_rule_json(duplicate.raw, rule) if mode == "merge" else _replace_source_rule_json(duplicate.raw, rule)
    candidate_sources = _candidate_sources_update(sources, duplicate.index, updated_rule_json)
    try:
        candidate_config = _load_candidate_config(root, app_config, sources=candidate_sources)
        before = WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    after = WeightedRoutingEngine(candidate_config).route(article)
    backup_path = latest_source_backup_path(root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sources_path, backup_path)
    _atomic_write_json(sources_path, candidate_sources)
    try:
        load_weighted_routing_config(root, app_config)
    except WeightedRoutingConfigError as exc:
        restore_latest_backup(root)
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return TeachingApplyResult(rule=_source_url_rule_from_json(updated_rule_json), before=before, after=after, backup_path=backup_path)


def restore_latest_backup(config_dir: str | Path) -> Path:
    root = Path(config_dir)
    candidates = [
        (latest_backup_path(root), root / "evidence.json"),
        (latest_source_backup_path(root), root / "sources.json"),
    ]
    existing = [(backup, target) for backup, target in candidates if backup.exists()]
    if not existing:
        raise RoutingTeachError("No routing evidence backup exists yet.")
    backup_path, target_path = max(existing, key=lambda item: item[0].stat().st_mtime)
    shutil.copy2(backup_path, target_path)
    backup_path.unlink(missing_ok=True)
    return target_path


def latest_backup_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / ".discord_backups" / "evidence.latest.json"


def latest_source_backup_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / ".discord_backups" / "sources.latest.json"


def decision_summary(decision: RoutingDecision) -> str:
    final = ", ".join(decision.final_channel_keys) or "none"
    top_scores = ", ".join(
        f"{score.channel_key}:{score.score}/{score.minimum_score}"
        for score in decision.channel_scores[:4]
        if int(score.score) != 0 or score.selected
    )
    return f"{decision.decision_status} -> {final} | top {decision.top_score} | {top_scores or 'no scored routes'}"


def route_score_suggestions(app_config: AppConfig, current: str, limit: int = 25) -> list[tuple[str, str]]:
    root = _routing_root(app_config)
    allowed_routes = _allowed_route_keys(app_config)
    aliases = _route_aliases(root, allowed_routes)
    base = ""
    token = current or ""
    if "," in token:
        base, token = token.rsplit(",", 1)
        base = base.rstrip() + ", "
    query = _normalize_route_input(token.split(":", 1)[0])
    route_labels = [(route, route, route) for route in sorted(allowed_routes)]
    alias_labels = [(alias, f"{_display_route(alias)} -> {route}", route) for alias, route in sorted(aliases.items())]
    ranked_candidates = []
    for key, label, route in alias_labels + route_labels:
        rank = _route_match_rank(key, route, query)
        if rank is not None:
            ranked_candidates.append((rank, label, key, route))
    ranked_candidates.sort(key=lambda item: (item[0], item[1]))
    suggestions: list[tuple[str, str]] = []
    seen_values: set[str] = set()
    for _rank, label, _key, route in ranked_candidates:
        value = f"{base}{route}:+50"
        if value in seen_values:
            continue
        seen_values.add(value)
        suggestions.append((label[:100], value[:100]))
        if len(suggestions) >= limit:
            break
    return suggestions


def evidence_json_snippet(value: dict[str, Any], limit: int = 850) -> str:
    text = json.dumps(value, indent=2, sort_keys=False)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)].rstrip() + "\n... truncated ..."


def _route_current(article: RoutingArticle, root: Path, app_config: AppConfig) -> RoutingDecision:
    try:
        return WeightedRoutingEngine(load_weighted_routing_config(root, app_config)).route(article)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc


def _routing_root(app_config: AppConfig) -> Path:
    return Path(app_config.settings.routing.weighted_config_dir)


def _allowed_route_keys(app_config: AppConfig) -> set[str]:
    root = _routing_root(app_config)
    try:
        routing_config = load_weighted_routing_config(root, app_config)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc
    return {route.key for route in routing_config.routes}


def _route_aliases(root: Path, allowed_routes: set[str]) -> dict[str, str]:
    routes_raw = _read_json_file(root / "routes.json")
    settings = routes_raw.get("settings") if isinstance(routes_raw.get("settings"), dict) else {}
    raw_aliases = settings.get("route_aliases", {})
    if not isinstance(raw_aliases, dict):
        return {}
    aliases: dict[str, str] = {}
    for raw_alias, raw_route in raw_aliases.items():
        if not isinstance(raw_alias, str) or not isinstance(raw_route, str):
            continue
        route_key = _normalize_route_input(raw_route)
        if route_key in allowed_routes:
            aliases[_normalize_route_input(raw_alias)] = route_key
    return aliases


def _resolve_route_key(raw_route_key: str, allowed_routes: set[str], aliases: dict[str, str]) -> str:
    normalized = _normalize_route_input(raw_route_key)
    if normalized in allowed_routes:
        return normalized
    return aliases.get(normalized, normalized)


def _normalize_route_input(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _route_hint(allowed_routes: set[str], aliases: dict[str, str]) -> str:
    preferred = [
        "europe",
        "the-hill",
        "the-white-house",
        "north-america",
        "sea",
        "air",
        "land",
        "space",
        "strategic-weapons",
        "noise",
        "review",
    ]
    values = [value for value in preferred if value in allowed_routes]
    alias_values = [f"{alias}->{route}" for alias, route in sorted(aliases.items())[:5]]
    return ", ".join(values + alias_values)


def _display_route(value: str) -> str:
    return " ".join(part.upper() if part in {"us", "u", "s"} else part.capitalize() for part in value.split("-"))


def _route_match_rank(key: str, route: str, query: str) -> int | None:
    if not query:
        return 10
    values = {key, route}
    parts = set(key.split("-")) | set(route.split("-"))
    if query in values or query in parts:
        return 0
    if any(value.startswith(query) for value in values) or any(part.startswith(query) for part in parts):
        return 1
    if len(query) > 2 and (any(query in value for value in values) or any(query in part for part in parts)):
        return 2
    return None


def _read_evidence(root: Path) -> dict[str, Any]:
    path = root / "evidence.json"
    data = _read_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("evidence"), list):
        raise RoutingTeachError("Evidence JSON must contain an `evidence` array.")
    return data


def _read_sources(root: Path) -> dict[str, Any]:
    path = root / "sources.json"
    data = _read_json_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise RoutingTeachError("Sources JSON must contain a `sources` array.")
    return data


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RoutingTeachError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RoutingTeachError(f"{path.name} is invalid at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise RoutingTeachError(f"{path.name} must contain a JSON object.")
    return data


def _candidate_evidence(evidence: dict[str, Any], rule: TeachingRule) -> dict[str, Any]:
    candidate = json.loads(json.dumps(evidence))
    candidate["evidence"].append(rule.to_json())
    return candidate


def _candidate_evidence_update(evidence: dict[str, Any], index: int, updated_rule: dict[str, Any]) -> dict[str, Any]:
    candidate = json.loads(json.dumps(evidence))
    candidate["evidence"][index] = updated_rule
    return candidate


def _candidate_sources(sources: dict[str, Any], rule: SourceUrlTeachingRule) -> dict[str, Any]:
    candidate = json.loads(json.dumps(sources))
    candidate["sources"].append(rule.to_json())
    return candidate


def _candidate_sources_update(sources: dict[str, Any], index: int, updated_rule: dict[str, Any]) -> dict[str, Any]:
    candidate = json.loads(json.dumps(sources))
    candidate["sources"][index] = updated_rule
    return candidate


def _validate_candidate(root: Path, app_config: AppConfig, evidence: dict[str, Any]) -> None:
    try:
        _load_candidate_config(root, app_config, evidence)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc


def _validate_source_candidate(root: Path, app_config: AppConfig, sources: dict[str, Any]) -> None:
    try:
        _load_candidate_config(root, app_config, sources=sources)
    except WeightedRoutingConfigError as exc:
        raise RoutingTeachError(" ".join(exc.errors)) from exc


def _load_candidate_config(
    root: Path,
    app_config: AppConfig,
    evidence: dict[str, Any] | None = None,
    sources: dict[str, Any] | None = None,
):
    with tempfile.TemporaryDirectory(prefix="rssbot-routing-v2-") as tmp:
        tmp_root = Path(tmp)
        for name in ("routes.json", "mirrors.json"):
            shutil.copy2(root / name, tmp_root / name)
        if evidence is None:
            shutil.copy2(root / "evidence.json", tmp_root / "evidence.json")
        else:
            _atomic_write_json(tmp_root / "evidence.json", evidence)
        if sources is None:
            shutil.copy2(root / "sources.json", tmp_root / "sources.json")
        else:
            _atomic_write_json(tmp_root / "sources.json", sources)
        return load_weighted_routing_config(tmp_root, app_config)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _existing_rule_ids(evidence: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in evidence.get("evidence", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return ids


def _existing_source_rule_ids(sources: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for item in sources.get("sources", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return ids


def _find_duplicate_rule(evidence: dict[str, Any], rule: TeachingRule) -> DuplicateEvidenceRule | None:
    entries = evidence.get("evidence", [])
    if not isinstance(entries, list):
        return None
    submitted_literal = _normalize_phrase(rule.term) if rule.rule_type == "literal" else None
    submitted_pattern = rule.term.strip() if rule.rule_type == "pattern" else literal_to_regex(rule.term)
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        existing_id = str(item.get("id") or f"evidence[{index}]")
        existing_type = str(item.get("type") or "literal")
        if rule.rule_type == "literal" and existing_type == "literal":
            existing_phrase = str(item.get("phrase") or "")
            if _normalize_phrase(existing_phrase) == submitted_literal:
                return DuplicateEvidenceRule(index=index, rule_id=existing_id, match_type="literal_phrase", raw=json.loads(json.dumps(item)))
        existing_pattern = str(item.get("pattern") or "")
        if existing_type == "literal" and item.get("phrase"):
            existing_pattern = literal_to_regex(str(item.get("phrase")))
        if existing_pattern and existing_pattern.strip() == submitted_pattern:
            return DuplicateEvidenceRule(index=index, rule_id=existing_id, match_type="pattern_text", raw=json.loads(json.dumps(item)))
    return None


def _find_duplicate_source_rule(sources: dict[str, Any], rule: SourceUrlTeachingRule) -> DuplicateEvidenceRule | None:
    entries = sources.get("sources", [])
    if not isinstance(entries, list):
        return None
    submitted_key = _source_rule_match_key(rule.to_json())
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        existing_key = _source_rule_match_key(item)
        if existing_key == submitted_key:
            existing_id = str(item.get("id") or f"sources[{index}]")
            return DuplicateEvidenceRule(index=index, rule_id=existing_id, match_type="source_url_criteria", raw=json.loads(json.dumps(item)))
    return None


def _source_rule_match_key(raw: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    hosts = tuple(sorted(_normalize_host(value) for value in _string_values(raw.get("source_url_hosts")) if _normalize_host(value)))
    terms = tuple(sorted(_normalize_path_term(value) for value in _string_values(raw.get("source_url_path_terms")) if _normalize_path_term(value)))
    patterns = tuple(sorted(value.strip() for value in _string_values(raw.get("source_url_path_patterns")) if value.strip()))
    return hosts, terms, patterns


def _normalize_phrase(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _merge_rule_json(existing: dict[str, Any], submitted: TeachingRule) -> dict[str, Any]:
    merged = json.loads(json.dumps(existing))
    existing_scores = merged.get("scores") if isinstance(merged.get("scores"), dict) else {}
    merged["scores"] = dict(sorted({**existing_scores, **submitted.scores}.items()))
    existing_fields = [str(item) for item in merged.get("fields", []) if isinstance(item, str)]
    merged["fields"] = list(dict.fromkeys(existing_fields + list(submitted.fields))) or list(DEFAULT_FIELDS)
    if submitted.notes:
        prior_notes = str(merged.get("notes") or "").strip()
        merged["notes"] = f"{prior_notes} | Discord merge: {submitted.notes}" if prior_notes else submitted.notes
    return merged


def _replace_rule_json(existing: dict[str, Any], submitted: TeachingRule) -> dict[str, Any]:
    replaced = json.loads(json.dumps(existing))
    replaced["type"] = submitted.rule_type
    replaced.pop("phrase", None)
    replaced.pop("pattern", None)
    if submitted.rule_type == "literal":
        replaced["phrase"] = submitted.term
    else:
        replaced["pattern"] = submitted.term
    replaced["scores"] = dict(sorted(submitted.scores.items()))
    replaced["fields"] = list(submitted.fields)
    if submitted.notes:
        replaced["notes"] = submitted.notes
    elif "notes" in replaced:
        replaced.pop("notes", None)
    return replaced


def _merge_source_rule_json(existing: dict[str, Any], submitted: SourceUrlTeachingRule) -> dict[str, Any]:
    merged = json.loads(json.dumps(existing))
    existing_scores = merged.get("scores") if isinstance(merged.get("scores"), dict) else {}
    merged["scores"] = dict(sorted({**existing_scores, **submitted.scores}.items()))
    merged["url_bias_only"] = bool(merged.get("url_bias_only", submitted.url_bias_only))
    if submitted.notes:
        prior_notes = str(merged.get("notes") or "").strip()
        merged["notes"] = f"{prior_notes} | Discord feed URL merge: {submitted.notes}" if prior_notes else submitted.notes
    return merged


def _replace_source_rule_json(existing: dict[str, Any], submitted: SourceUrlTeachingRule) -> dict[str, Any]:
    replaced = json.loads(json.dumps(existing))
    for key in ("source_url_hosts", "source_url_path_terms", "source_url_path_patterns", "source_name_pattern"):
        replaced.pop(key, None)
    replaced.update(submitted.to_json())
    replaced["id"] = str(existing.get("id") or submitted.id)
    return replaced


def _teaching_rule_from_json(raw: dict[str, Any]) -> TeachingRule:
    rule_type = str(raw.get("type") or "literal")
    term = str(raw.get("phrase") if rule_type == "literal" else raw.get("pattern") or "")
    fields = tuple(str(item) for item in raw.get("fields", DEFAULT_FIELDS) if isinstance(item, str))
    scores = {str(key): int(value) for key, value in dict(raw.get("scores") or {}).items()}
    return TeachingRule(
        id=str(raw.get("id") or _unique_rule_id(term, set())),
        term=term,
        rule_type=rule_type,
        fields=fields or DEFAULT_FIELDS,
        scores=scores,
        notes=str(raw["notes"]) if isinstance(raw.get("notes"), str) else None,
    )


def _source_url_rule_from_json(raw: dict[str, Any]) -> SourceUrlTeachingRule:
    scores = {str(key): int(value) for key, value in dict(raw.get("scores") or {}).items()}
    return SourceUrlTeachingRule(
        id=str(raw.get("id") or _unique_source_rule_id("source-url", set())),
        scores=scores,
        source_url_hosts=tuple(_normalize_host(value) for value in _string_values(raw.get("source_url_hosts")) if _normalize_host(value)),
        source_url_path_terms=tuple(
            _normalize_path_term(value) for value in _string_values(raw.get("source_url_path_terms")) if _normalize_path_term(value)
        ),
        source_url_path_patterns=tuple(value.strip() for value in _string_values(raw.get("source_url_path_patterns")) if value.strip()),
        notes=str(raw["notes"]) if isinstance(raw.get("notes"), str) else None,
        url_bias_only=bool(raw.get("url_bias_only", True)),
    )


def _unique_rule_id(term: str, existing_ids: set[str]) -> str:
    slug = SLUG_RE.sub("-", term.casefold()).strip("-")[:64] or "rule"
    date_part = datetime.now(UTC).strftime("%Y%m%d")
    base = f"discord-{slug}-{date_part}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _unique_source_rule_id(term: str, existing_ids: set[str]) -> str:
    slug = SLUG_RE.sub("-", term.casefold()).strip("-")[:64] or "source-url"
    date_part = datetime.now(UTC).strftime("%Y%m%d")
    base = f"discord-source-url-{slug}-{date_part}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _parse_csv_values(value: str | None) -> list[str]:
    if not value or not str(value).strip():
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _normalize_host(value: str) -> str:
    text = value.strip().casefold()
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or text).strip().casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _host_from_url(value: str) -> str | None:
    host = _normalize_host(value)
    return host or None


def _normalize_path_term(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
