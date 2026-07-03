from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.models import AppConfig
from app.routing_v2.matcher import literal_to_regex
from app.routing_v2.models import (
    CompiledEvidenceRule,
    MirrorRule,
    SourceScoreRule,
    WeightedRoute,
    WeightedRoutingConfig,
)

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
DANGEROUS_REGEX_RE = re.compile(r"\((?:\?:)?[^)]*[+*][^)]*\)[+*]")
VALID_FIELDS = {"title", "summary", "url_slug", "source_name"}
VALID_RULE_TYPES = {"literal", "pattern"}


class WeightedRoutingConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def load_weighted_routing_config(config_dir: str | Path, app_config: AppConfig) -> WeightedRoutingConfig:
    root = Path(config_dir)
    errors: list[str] = []
    routes_raw = _read_json(root / "routes.json", errors)
    evidence_raw = _read_json(root / "evidence.json", errors)
    sources_raw = _read_json(root / "sources.json", errors)
    mirrors_raw = _read_json(root / "mirrors.json", errors)
    if errors:
        raise WeightedRoutingConfigError(errors)

    channel_keys = {channel.key for channel in app_config.channels}
    version, routes, route_keys, pseudo_keys, settings = _parse_routes(routes_raw, channel_keys, errors)
    evidence = _parse_evidence(evidence_raw, route_keys | pseudo_keys, errors)
    sources = _parse_sources(sources_raw, route_keys | pseudo_keys, errors)
    mirrors = _parse_mirrors(mirrors_raw, channel_keys, errors)

    if errors:
        raise WeightedRoutingConfigError(errors)
    return WeightedRoutingConfig(
        version=version,
        routes=tuple(routes),
        evidence_rules=tuple(evidence),
        source_rules=tuple(sources),
        mirror_rules=tuple(mirrors),
        field_multipliers=dict(settings["field_multipliers"]),
        primary_threshold=int(settings["primary_threshold"]),
        review_threshold=int(settings["review_threshold"]),
        noise_threshold=int(settings["noise_threshold"]),
        secondary_within_percent=int(settings["secondary_within_percent"]),
        max_primary_destinations=int(settings["max_primary_destinations"]),
    )


def _parse_routes(
    raw: dict[str, Any],
    channel_keys: set[str],
    errors: list[str],
) -> tuple[int, list[WeightedRoute], set[str], set[str], dict[str, object]]:
    version = _int(raw.get("version", 1), "routes.version", errors, min_value=1)
    settings = _object(raw.get("settings", {}), "routes.settings", errors)
    field_multipliers = _float_map(
        settings.get(
            "field_multipliers",
            {"title": 1.0, "summary": 0.7, "url_slug": 0.4, "source_name": 0.8},
        ),
        "routes.settings.field_multipliers",
        errors,
    )
    for field_name in field_multipliers:
        if field_name not in VALID_FIELDS:
            errors.append(f"routes.settings.field_multipliers has unknown field: {field_name}")
    parsed_settings = {
        "field_multipliers": field_multipliers,
        "primary_threshold": _int(settings.get("primary_threshold", 25), "routes.settings.primary_threshold", errors),
        "review_threshold": _int(settings.get("review_threshold", 20), "routes.settings.review_threshold", errors),
        "noise_threshold": _int(settings.get("noise_threshold", 35), "routes.settings.noise_threshold", errors),
        "secondary_within_percent": _int(
            settings.get("secondary_within_percent", 10),
            "routes.settings.secondary_within_percent",
            errors,
            min_value=0,
            max_value=100,
        ),
        "max_primary_destinations": _int(
            settings.get("max_primary_destinations", 2),
            "routes.settings.max_primary_destinations",
            errors,
            min_value=1,
            max_value=10,
        ),
    }

    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, list):
        errors.append("routes.routes must be an array.")
        return version, [], set(), set(), parsed_settings
    routes: list[WeightedRoute] = []
    route_keys: set[str] = set()
    pseudo_keys: set[str] = set()
    seen: set[str] = set()
    for index, item in enumerate(routes_raw):
        path = f"routes.routes[{index}]"
        obj = _object(item, path, errors)
        key = _key(obj.get("key"), f"{path}.key", errors)
        if key in seen:
            errors.append(f"{path}.key duplicates route {key}")
        seen.add(key)
        pseudo = _bool(obj.get("pseudo", False), f"{path}.pseudo", errors)
        if pseudo:
            pseudo_keys.add(key)
        else:
            route_keys.add(key)
            if key not in channel_keys:
                errors.append(f"{path}.key is not present in config/config.json: {key}")
        destination_class = _choice(
            obj.get("destination_class", "primary"),
            f"{path}.destination_class",
            errors,
            {"primary", "review", "mirror", "pseudo"},
        )
        routes.append(
            WeightedRoute(
                key=key,
                destination_class=destination_class,
                threshold=_int(obj.get("threshold", parsed_settings["primary_threshold"]), f"{path}.threshold", errors),
                priority=_int(obj.get("priority", 0), f"{path}.priority", errors),
                enabled=_bool(obj.get("enabled", True), f"{path}.enabled", errors),
                pseudo=pseudo,
                required_source_ids=tuple(
                    _string_list(obj.get("required_source_ids", []), f"{path}.required_source_ids", errors)
                ),
                excluded_source_ids=tuple(
                    _string_list(obj.get("excluded_source_ids", []), f"{path}.excluded_source_ids", errors)
                ),
                required_source_classes=tuple(
                    _string_list(obj.get("required_source_classes", []), f"{path}.required_source_classes", errors)
                ),
                excluded_source_classes=tuple(
                    _string_list(obj.get("excluded_source_classes", []), f"{path}.excluded_source_classes", errors)
                ),
            )
        )
    for required in ("noise", "review"):
        if required not in route_keys | pseudo_keys:
            errors.append(f"routes.routes must include {required}")
    return version, routes, route_keys, pseudo_keys, parsed_settings


def _parse_evidence(raw: dict[str, Any], allowed_routes: set[str], errors: list[str]) -> list[CompiledEvidenceRule]:
    entries = raw.get("evidence", [])
    if not isinstance(entries, list):
        errors.append("evidence.evidence must be an array.")
        return []
    parsed: list[CompiledEvidenceRule] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        path = f"evidence.evidence[{index}]"
        obj = _object(item, path, errors)
        rule_id = _key(obj.get("id"), f"{path}.id", errors)
        if rule_id in seen:
            errors.append(f"{path}.id duplicates evidence rule {rule_id}")
        seen.add(rule_id)
        rule_type = _choice(obj.get("type", "literal"), f"{path}.type", errors, VALID_RULE_TYPES)
        fields = tuple(_string_list(obj.get("fields", ["title", "summary", "url_slug"]), f"{path}.fields", errors))
        for field in fields:
            if field not in VALID_FIELDS:
                errors.append(f"{path}.fields references unknown field: {field}")
        scores = _score_map(obj.get("scores", {}), f"{path}.scores", allowed_routes, errors)
        pattern_text = ""
        if rule_type == "literal":
            phrase = _string(obj.get("phrase"), f"{path}.phrase", errors)
            pattern_text = literal_to_regex(phrase)
        else:
            pattern_text = _string(obj.get("pattern"), f"{path}.pattern", errors)
        compiled = _compile_pattern(pattern_text, path, errors)
        parsed.append(
            CompiledEvidenceRule(
                id=rule_id,
                type=rule_type,
                pattern=compiled,
                pattern_text=pattern_text,
                scores=scores,
                fields=fields,
                priority=_int(obj.get("priority", 0), f"{path}.priority", errors),
                confidence=_int(obj.get("confidence", 0), f"{path}.confidence", errors),
                blocks=tuple(_string_list(obj.get("blocks", []), f"{path}.blocks", errors)),
                block_window_before=_int(
                    obj.get("block_window_before", 0),
                    f"{path}.block_window_before",
                    errors,
                    min_value=0,
                    max_value=500,
                ),
                block_window_after=_int(
                    obj.get("block_window_after", 0),
                    f"{path}.block_window_after",
                    errors,
                    min_value=0,
                    max_value=500,
                ),
                notes=obj.get("notes") if isinstance(obj.get("notes"), str) else None,
            )
        )
    return parsed


def _parse_sources(raw: dict[str, Any], allowed_routes: set[str], errors: list[str]) -> list[SourceScoreRule]:
    entries = raw.get("sources", [])
    if not isinstance(entries, list):
        errors.append("sources.sources must be an array.")
        return []
    parsed: list[SourceScoreRule] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        path = f"sources.sources[{index}]"
        obj = _object(item, path, errors)
        rule_id = _key(obj.get("id"), f"{path}.id", errors)
        if rule_id in seen:
            errors.append(f"{path}.id duplicates source rule {rule_id}")
        seen.add(rule_id)
        source_name_pattern = None
        if obj.get("source_name_pattern") is not None:
            source_name_pattern = _compile_pattern(
                _string(obj.get("source_name_pattern"), f"{path}.source_name_pattern", errors),
                path,
                errors,
            )
        path_pattern_texts = tuple(
            _string_list(obj.get("source_url_path_patterns", []), f"{path}.source_url_path_patterns", errors)
        )
        path_patterns = tuple(
            _compile_pattern(pattern_text, f"{path}.source_url_path_patterns[{pattern_index}]", errors)
            for pattern_index, pattern_text in enumerate(path_pattern_texts)
        )
        parsed.append(
            SourceScoreRule(
                id=rule_id,
                source_ids=tuple(_string_list(obj.get("source_ids", []), f"{path}.source_ids", errors)),
                source_classes=tuple(_string_list(obj.get("source_classes", []), f"{path}.source_classes", errors)),
                source_name_pattern=source_name_pattern,
                source_url_hosts=tuple(
                    _normalize_url_host(value)
                    for value in _string_list(obj.get("source_url_hosts", []), f"{path}.source_url_hosts", errors)
                ),
                source_url_path_terms=tuple(
                    _normalize_path_term(value)
                    for value in _string_list(obj.get("source_url_path_terms", []), f"{path}.source_url_path_terms", errors)
                ),
                source_url_path_patterns=path_patterns,
                source_url_path_pattern_texts=path_pattern_texts,
                url_bias_only=_bool(obj.get("url_bias_only", True), f"{path}.url_bias_only", errors),
                scores=_score_map(obj.get("scores", {}), f"{path}.scores", allowed_routes, errors),
                priority=_int(obj.get("priority", 0), f"{path}.priority", errors),
                notes=obj.get("notes") if isinstance(obj.get("notes"), str) else None,
            )
        )
    return parsed


def _parse_mirrors(raw: dict[str, Any], channel_keys: set[str], errors: list[str]) -> list[MirrorRule]:
    entries = raw.get("mirrors", [])
    if not isinstance(entries, list):
        errors.append("mirrors.mirrors must be an array.")
        return []
    parsed: list[MirrorRule] = []
    for index, item in enumerate(entries):
        path = f"mirrors.mirrors[{index}]"
        obj = _object(item, path, errors)
        channel_key = _key(obj.get("channel_key"), f"{path}.channel_key", errors)
        if channel_key not in channel_keys:
            errors.append(f"{path}.channel_key is not present in config/config.json: {channel_key}")
        parsed.append(
            MirrorRule(
                channel_key=channel_key,
                required_source_ids=tuple(_string_list(obj.get("required_source_ids", []), f"{path}.required_source_ids", errors)),
                excluded_source_ids=tuple(_string_list(obj.get("excluded_source_ids", []), f"{path}.excluded_source_ids", errors)),
                required_source_classes=tuple(
                    _string_list(obj.get("required_source_classes", []), f"{path}.required_source_classes", errors)
                ),
                excluded_source_classes=tuple(
                    _string_list(obj.get("excluded_source_classes", []), f"{path}.excluded_source_classes", errors)
                ),
                enabled=_bool(obj.get("enabled", True), f"{path}.enabled", errors),
                priority=_int(obj.get("priority", 0), f"{path}.priority", errors),
            )
        )
    return parsed


def _compile_pattern(pattern_text: str, path: str, errors: list[str]) -> re.Pattern[str]:
    if len(pattern_text) > 1000:
        errors.append(f"{path} regex is too long.")
        pattern_text = r"a^"
    if DANGEROUS_REGEX_RE.search(pattern_text):
        errors.append(f"{path} regex contains nested quantifiers that are not allowed.")
        pattern_text = r"a^"
    try:
        return re.compile(pattern_text, re.IGNORECASE)
    except re.error as exc:
        errors.append(f"{path} regex is invalid: {exc}")
        return re.compile(r"a^")


def _read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"weighted routing config file not found: {path}")
        return {}
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{path}: root must be a JSON object")
        return {}
    return raw


def _object(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    errors.append(f"{path} must be an object.")
    return {}


def _string(value: Any, path: str, errors: list[str]) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    errors.append(f"{path} must be a non-empty string.")
    return ""


def _string_list(value: Any, path: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array of strings.")
        return []
    parsed: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            parsed.append(item.strip())
        else:
            errors.append(f"{path}[{index}] must be a non-empty string.")
    return parsed


def _normalize_url_host(value: str) -> str:
    text = value.strip().casefold()
    parsed = urlparse(text if "://" in text else f"//{text}")
    host = (parsed.hostname or text).strip().casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _normalize_path_term(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _key(value: Any, path: str, errors: list[str]) -> str:
    key = _string(value, path, errors)
    if key and not KEY_RE.match(key):
        errors.append(f"{path} must use lowercase letters, numbers, hyphens, or underscores.")
    return key


def _score_map(value: Any, path: str, allowed_routes: set[str], errors: list[str]) -> dict[str, int]:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object with integer values.")
        return {}
    parsed: dict[str, int] = {}
    for key, raw_score in value.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{path} keys must be non-empty strings.")
            continue
        if key not in allowed_routes:
            errors.append(f"{path} references unknown route: {key}")
        if isinstance(raw_score, bool) or not isinstance(raw_score, int):
            errors.append(f"{path}.{key} must be an integer.")
            continue
        parsed[key] = raw_score
    return parsed


def _float_map(value: Any, path: str, errors: list[str]) -> dict[str, float]:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object with numeric values.")
        return {}
    parsed: dict[str, float] = {}
    for key, raw_value in value.items():
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            errors.append(f"{path}.{key} must be numeric.")
            continue
        parsed[str(key)] = float(raw_value)
    return parsed


def _int(value: Any, path: str, errors: list[str], min_value: int | None = None, max_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{path} must be an integer.")
        return 0
    if min_value is not None and value < min_value:
        errors.append(f"{path} must be at least {min_value}.")
    if max_value is not None and value > max_value:
        errors.append(f"{path} must be at most {max_value}.")
    return value


def _bool(value: Any, path: str, errors: list[str]) -> bool:
    if isinstance(value, bool):
        return value
    errors.append(f"{path} must be true or false.")
    return False


def _choice(value: Any, path: str, errors: list[str], allowed: set[str]) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    errors.append(f"{path} must be one of: {', '.join(sorted(allowed))}.")
    return sorted(allowed)[0]
