from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.models import AppConfig
from app.routing.models import ChannelRule, KnowledgeEntry, RoutingConfig, TaxonomyTag
from app.routing.taxonomy import find_taxonomy_cycles


KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class RoutingConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def load_routing_config(config_dir: str | Path, app_config: AppConfig) -> RoutingConfig:
    root = Path(config_dir)
    errors: list[str] = []
    taxonomy_raw = _read_json(root / "taxonomy.json", errors)
    knowledge_raw = _read_json(root / "knowledge_base.json", errors)
    channels_raw = _read_json(root / "channels.json", errors)
    if errors:
        raise RoutingConfigError(errors)

    taxonomy_version, taxonomy = _parse_taxonomy(taxonomy_raw, errors)
    knowledge_base_version, knowledge_entries = _parse_knowledge(knowledge_raw, taxonomy, errors)
    channels_version, max_destinations, review_tags, skip_tags, channel_rules = _parse_channels(
        channels_raw,
        taxonomy,
        app_config,
        errors,
    )
    errors.extend(find_taxonomy_cycles(taxonomy))

    if errors:
        raise RoutingConfigError(errors)
    return RoutingConfig(
        taxonomy_version=taxonomy_version,
        knowledge_base_version=knowledge_base_version,
        channels_version=channels_version,
        taxonomy=taxonomy,
        knowledge_entries=tuple(knowledge_entries),
        channel_rules=tuple(channel_rules),
        max_destinations=max_destinations,
        review_tags=tuple(review_tags),
        skip_tags=tuple(skip_tags),
    )


def _read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"routing config file not found: {path}")
        return {}
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{path}: root must be a JSON object")
        return {}
    return raw


def _parse_taxonomy(raw: dict[str, Any], errors: list[str]) -> tuple[int, dict[str, TaxonomyTag]]:
    version = _int(raw.get("version", 1), "taxonomy.version", errors, min_value=1)
    tags_raw = raw.get("tags")
    if not isinstance(tags_raw, dict) or not tags_raw:
        errors.append("taxonomy.tags must be a non-empty object.")
        return version, {}

    taxonomy: dict[str, TaxonomyTag] = {}
    for tag, tag_raw in tags_raw.items():
        path = f"taxonomy.tags.{tag}"
        if not _valid_key(tag):
            errors.append(f"{path} must use lowercase letters, numbers, hyphens, or underscores.")
        tag_obj = _object(tag_raw, path, errors)
        parents = _string_list(tag_obj.get("parent_tags", []), f"{path}.parent_tags", errors)
        description = tag_obj.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"{path}.description must be a string when provided.")
            description = None
        taxonomy[tag] = TaxonomyTag(tag=tag, parent_tags=tuple(parents), description=description)

    for tag, item in taxonomy.items():
        for parent in item.parent_tags:
            if parent not in taxonomy:
                errors.append(f"taxonomy tag {tag} references unknown parent tag {parent}.")
            if parent == tag:
                errors.append(f"taxonomy tag {tag} cannot be its own parent.")
    return version, taxonomy


def _parse_knowledge(
    raw: dict[str, Any],
    taxonomy: dict[str, TaxonomyTag],
    errors: list[str],
) -> tuple[int, list[KnowledgeEntry]]:
    version = _int(raw.get("version", 1), "knowledge_base.version", errors, min_value=1)
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        errors.append("knowledge_base.entries must be an array.")
        return version, []

    entries: list[KnowledgeEntry] = []
    seen_ids: set[str] = set()
    for index, entry_raw in enumerate(entries_raw):
        path = f"knowledge_base.entries[{index}]"
        entry_obj = _object(entry_raw, path, errors)
        entry_id = _string(entry_obj.get("id"), f"{path}.id", errors)
        if entry_id and not _valid_key(entry_id):
            errors.append(f"{path}.id must use lowercase letters, numbers, hyphens, or underscores.")
        if entry_id in seen_ids:
            errors.append(f"{path}.id duplicates another knowledge entry: {entry_id}")
        seen_ids.add(entry_id)
        aliases = _string_list(entry_obj.get("aliases"), f"{path}.aliases", errors, non_empty=True)
        tags = _string_list(entry_obj.get("tags"), f"{path}.tags", errors, non_empty=True)
        for tag in tags:
            if tag not in taxonomy:
                errors.append(f"{path}.tags emits unknown tag: {tag}")
        priority = _int(entry_obj.get("priority", 0), f"{path}.priority", errors)
        score = _int(entry_obj.get("score", 1), f"{path}.score", errors)
        description = entry_obj.get("description")
        if description is not None and not isinstance(description, str):
            errors.append(f"{path}.description must be a string when provided.")
            description = None
        entries.append(
            KnowledgeEntry(
                id=entry_id,
                aliases=tuple(aliases),
                tags=tuple(tags),
                priority=priority,
                score=score,
                description=description,
            )
        )
    return version, entries


def _parse_channels(
    raw: dict[str, Any],
    taxonomy: dict[str, TaxonomyTag],
    app_config: AppConfig,
    errors: list[str],
) -> tuple[int, int, list[str], list[str], list[ChannelRule]]:
    version = _int(raw.get("version", 1), "channels.version", errors, min_value=1)
    max_destinations = _int(raw.get("max_destinations", 3), "channels.max_destinations", errors, min_value=1, max_value=25)
    review_tags = _string_list(raw.get("review_tags", ["review_required", "ambiguous"]), "channels.review_tags", errors)
    skip_tags = _string_list(raw.get("skip_tags", ["skip_candidate"]), "channels.skip_tags", errors)
    defaults = {
        "content_mode_adjustments": {"title_only": -1},
        **_object(raw.get("rule_defaults", {}), "channels.rule_defaults", errors),
    }
    for tag in review_tags + skip_tags:
        if tag not in taxonomy:
            errors.append(f"channels references unknown behavior tag: {tag}")

    configured_keys = {channel.key for channel in app_config.channels}
    rules_raw = raw.get("channels")
    if not isinstance(rules_raw, list):
        errors.append("channels.channels must be an array.")
        return version, max_destinations, review_tags, skip_tags, []
    seen_keys: set[str] = set()
    rules: list[ChannelRule] = []
    for index, rule_raw in enumerate(rules_raw):
        path = f"channels.channels[{index}]"
        rule_obj = _object(rule_raw, path, errors)
        channel_key = _string(rule_obj.get("channel_key"), f"{path}.channel_key", errors)
        if channel_key and channel_key not in configured_keys:
            errors.append(f"{path}.channel_key is not present in config/config.json: {channel_key}")
        if channel_key in seen_keys:
            errors.append(f"{path}.channel_key duplicates another routing rule: {channel_key}")
        seen_keys.add(channel_key)

        required_any = _string_list(_rule_value(rule_obj, defaults, "required_any", []), f"{path}.required_any", errors)
        for required in required_any:
            if required not in taxonomy and not _valid_key(required):
                errors.append(f"{path}.required_any has invalid entry: {required}")
        excluded_any = _string_list(_rule_value(rule_obj, defaults, "excluded_any", []), f"{path}.excluded_any", errors)
        for excluded in excluded_any:
            if excluded not in taxonomy and not _valid_key(excluded):
                errors.append(f"{path}.excluded_any has invalid entry: {excluded}")
        term_boosts = _merged_score_map(rule_obj, defaults, "term_boosts", path, errors)
        tag_boosts = _merged_score_map(rule_obj, defaults, "tag_boosts", path, errors)
        term_penalties = _merged_score_map(rule_obj, defaults, "term_penalties", path, errors)
        tag_penalties = _merged_score_map(rule_obj, defaults, "tag_penalties", path, errors)
        for tag in set(tag_boosts) | set(tag_penalties):
            if tag not in taxonomy:
                errors.append(f"{path} references unknown tag in score map: {tag}")

        rules.append(
            ChannelRule(
                channel_key=channel_key,
                enabled=_bool(_rule_value(rule_obj, defaults, "enabled", True), f"{path}.enabled", errors),
                minimum_score=_int(_rule_value(rule_obj, defaults, "minimum_score", 4), f"{path}.minimum_score", errors),
                priority=_int(_rule_value(rule_obj, defaults, "priority", 0), f"{path}.priority", errors),
                term_boosts=term_boosts,
                tag_boosts=tag_boosts,
                term_penalties=term_penalties,
                tag_penalties=tag_penalties,
                required_any=tuple(required_any),
                excluded_any=tuple(excluded_any),
                source_biases=_merged_score_map(rule_obj, defaults, "source_biases", path, errors),
                content_mode_adjustments=_merged_score_map(rule_obj, defaults, "content_mode_adjustments", path, errors),
                notes=rule_obj.get("notes") if isinstance(rule_obj.get("notes"), str) else None,
            )
        )
    return version, max_destinations, review_tags, skip_tags, rules


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


def _string_list(value: Any, path: str, errors: list[str], non_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array of strings.")
        return []
    parsed: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            parsed.append(item.strip())
        else:
            errors.append(f"{path}[{index}] must be a non-empty string.")
    if non_empty and not parsed:
        errors.append(f"{path} must not be empty.")
    return parsed


def _score_map(value: Any, path: str, errors: list[str]) -> dict[str, int]:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object with integer values.")
        return {}
    parsed: dict[str, int] = {}
    for key, raw_score in value.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{path} keys must be non-empty strings.")
            continue
        if isinstance(raw_score, bool) or not isinstance(raw_score, int):
            errors.append(f"{path}.{key} must be an integer.")
            continue
        parsed[key.strip()] = raw_score
    return parsed


def _rule_value(rule: dict[str, Any], defaults: dict[str, Any], key: str, fallback: Any) -> Any:
    if key in rule:
        return rule[key]
    if key in defaults:
        return defaults[key]
    return fallback


def _merged_score_map(
    rule: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    path: str,
    errors: list[str],
) -> dict[str, int]:
    merged = _score_map(defaults.get(key, {}), f"channels.rule_defaults.{key}", errors)
    merged.update(_score_map(rule.get(key, {}), f"{path}.{key}", errors))
    return merged


def _int(
    value: Any,
    path: str,
    errors: list[str],
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
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


def _valid_key(value: str) -> bool:
    return bool(KEY_RE.match(value))
