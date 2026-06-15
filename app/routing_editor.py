from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.config_loader import load_config
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.models import RoutingArticle
from app.routing.reporting import format_decision


KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
SCORE_KINDS = {"tag_boost", "tag_penalty", "concept_boost", "concept_penalty", "term_boost", "term_penalty"}
CHANNEL_MAPS = {
    "tag_boost": "tag_boosts",
    "tag_penalty": "tag_penalties",
    "concept_boost": "concept_boosts",
    "concept_penalty": "concept_penalties",
    "term_boost": "term_boosts",
    "term_penalty": "term_penalties",
}
LIST_FIELDS = {
    "required_tags",
    "required_concepts",
    "excluded_tags",
    "excluded_concepts",
    "required_any",
    "excluded_any",
    "review_tags",
    "skip_tags",
}
PROFILE_BY_CHANNEL = {
    "africa": "region_primary",
    "europe": "region_primary",
    "indo-pacific": "region_primary",
    "middle-east": "region_primary",
    "north-america": "region_primary",
    "south-central-america": "region_primary",
    "arctic": "region_primary",
    "sea": "military_domain_primary",
    "air": "military_domain_primary",
    "land": "military_domain_primary",
    "space": "military_domain_primary",
    "special-operations": "military_domain_primary",
    "training-and-doctrine": "military_domain_primary",
    "strategic-weapons": "military_domain_primary",
    "science-technology": "science_technology",
    "industrial-base": "industrial_base",
    "natsec-news": "natsec",
    "the-hill": "government_source_primary",
    "the-white-house": "government_source_primary",
    "dept-of-war": "source_mirror",
    "review": "review",
}
HELP_TOPICS = {
    "tags": (
        "Tags are the routing labels emitted by knowledge entries. "
        "They can inherit parent tags through taxonomy.json, so switzerland can also count as europe."
    ),
    "ids": (
        "Knowledge IDs are stable machine names for concepts. Aliases match article text, then the ID emits tags."
    ),
    "aliases": (
        "Aliases are the actual words or phrases matched in titles and summaries. "
        "Specific aliases usually route better than broad one-word aliases."
    ),
    "scores": (
        "Channel scores come from tag/term boosts minus tag/term penalties. "
        "A channel must meet its minimum score and any required_any gate before it can receive a post."
    ),
    "ripple": (
        "Ripple edits rename a tag or knowledge ID everywhere the routing config references it, "
        "including channel score maps and required/excluded lists."
    ),
    "deploy": (
        "Config-only routing edits need validation and a container recreate, not an image rebuild. "
        "Use ops/apply-routing-changes.ps1 or the config double-click redeploy launcher."
    ),
}


@dataclass
class RoutingFiles:
    routing_dir: Path
    taxonomy: dict[str, Any]
    knowledge: dict[str, Any]
    channels: dict[str, Any]
    suppressions: dict[str, Any]

    def as_named(self) -> dict[str, dict[str, Any]]:
        return {
            "taxonomy.json": self.taxonomy,
            "knowledge_base.json": self.knowledge,
            "channels.json": self.channels,
            "suppressions.json": self.suppressions,
        }


@dataclass(frozen=True)
class LintIssue:
    severity: str
    message: str
    fix: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            return run_wizard(args)
        return args.func(args)
    except KeyboardInterrupt:
        print("\nCanceled.")
        return 130
    except EditorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except RoutingConfigError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.routing_editor",
        description="Inspect and safely edit RSS bot routing tags, knowledge IDs, aliases, and channel scores.",
    )
    parser.add_argument("--routing-dir", default="config/routing", help="Routing config directory.")
    parser.add_argument("--config-path", default="config/config.json", help="Bot config path used for validation.")
    parser.add_argument("--yes", action="store_true", help="Skip write confirmation prompts.")
    parser.add_argument("--no-coaching", action="store_true", help="Suppress explanatory help for this run.")
    sub = parser.add_subparsers(dest="command")

    add_simple(sub, "wizard", "Open the guided editor.", run_wizard)

    find = add_simple(sub, "find", "Search tags, IDs, aliases, channels, and score maps.", cmd_find)
    find.add_argument("query")

    show_tag = add_simple(sub, "show-tag", "Show one taxonomy tag.", cmd_show_tag)
    show_tag.add_argument("tag")

    show_entry = add_simple(sub, "show-entry", "Show one knowledge entry.", cmd_show_entry)
    show_entry.add_argument("id")

    show_channel = add_simple(sub, "show-channel", "Show one channel routing rule.", cmd_show_channel)
    show_channel.add_argument("channel")

    add_simple(sub, "list-tags", "List taxonomy tags.", cmd_list_tags)
    add_simple(sub, "list-entries", "List knowledge IDs.", cmd_list_entries)

    channel_scores = add_simple(sub, "list-channel-scores", "List boosts and penalties for one channel.", cmd_channel_scores)
    channel_scores.add_argument("channel")

    lint = add_simple(sub, "lint", "Find routing mistakes and suspicious config.", cmd_lint)
    lint.add_argument("--strict", action="store_true", help="Return failure when warnings are present.")

    set_help = add_simple(sub, "set-help", "Turn detailed wizard explanations on or off.", cmd_set_help)
    set_help.add_argument("state", choices=("on", "off"))

    help_topic = add_simple(sub, "help-topic", "Explain routing editor concepts.", cmd_help_topic)
    help_topic.add_argument("topic", nargs="?", choices=sorted(HELP_TOPICS))

    add_tag = add_simple(sub, "add-tag", "Add a taxonomy tag.", cmd_add_tag)
    add_tag.add_argument("tag")
    add_tag.add_argument("--parent", action="append", default=[], help="Parent tag. Can be repeated.")
    add_tag.add_argument("--parents", help="Comma-separated parent tags.")
    add_tag.add_argument("--description")

    add_entry = add_simple(sub, "add-entry", "Add a knowledge ID.", cmd_add_entry)
    add_entry.add_argument("id")
    add_entry.add_argument("--alias", action="append", required=True, help="Alias phrase. Can be repeated.")
    add_entry.add_argument("--tag", action="append", required=True, help="Emitted tag. Can be repeated.")
    add_entry.add_argument("--priority", type=int, default=10)
    add_entry.add_argument("--score", type=int, default=1)
    add_entry.add_argument("--description")
    add_entry.add_argument("--create-missing-tags", action="store_true")

    add_alias = add_simple(sub, "add-alias", "Add aliases to an existing knowledge ID.", cmd_add_alias)
    add_alias.add_argument("id")
    add_alias.add_argument("--alias", action="append", required=True, help="Alias phrase. Can be repeated.")

    set_score = add_simple(sub, "set-channel-score", "Set a channel tag/term boost or penalty.", cmd_set_channel_score)
    set_score.add_argument("channel")
    set_score.add_argument("--kind", choices=sorted(SCORE_KINDS), required=True)
    set_score.add_argument("--key", required=True)
    set_score.add_argument("--score", type=int, required=True)
    set_score.add_argument("--create-missing-tag", action="store_true")

    rename_tag = add_simple(sub, "rename-tag", "Rename a tag everywhere routing config references it.", cmd_rename_tag)
    rename_tag.add_argument("old")
    rename_tag.add_argument("new")

    rename_entry = add_simple(sub, "rename-entry", "Rename a knowledge ID and related term scores.", cmd_rename_entry)
    rename_entry.add_argument("old")
    rename_entry.add_argument("new")

    route_test = add_simple(sub, "route-test", "Run the current routing model against a title.", cmd_route_test)
    route_test.add_argument("title")
    route_test.add_argument("--summary")
    route_test.add_argument("--source")
    route_test.add_argument("--source-id")
    route_test.add_argument("--source-class")
    route_test.add_argument("--url")

    add_simple(sub, "list-suppressions", "List suppression IDs.", cmd_list_suppressions)
    show_suppression = add_simple(sub, "show-suppression", "Show one suppression entry.", cmd_show_suppression)
    show_suppression.add_argument("id")
    add_suppression = add_simple(sub, "add-suppression", "Add a skip suppression.", cmd_add_suppression)
    add_suppression.add_argument("id")
    add_suppression.add_argument("--alias", action="append", required=True, help="Alias phrase. Can be repeated.")
    add_suppression.add_argument("--unless-tag", action="append", default=[], help="Tag that prevents suppression. Can be repeated.")
    add_suppression.add_argument("--description")
    add_suppression.add_argument("--priority", type=int, default=50)

    add_simple(sub, "migrate-typed-routing", "Migrate current routing JSON to typed fields and suppressions.", cmd_migrate)
    return parser


def add_simple(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
    func: Any,
) -> argparse.ArgumentParser:
    command = subparsers.add_parser(name, help=help_text)
    command.set_defaults(func=func)
    return command


def cmd_find(args: argparse.Namespace) -> int:
    files = load_files(args)
    query = args.query.casefold()
    print_coaching(args, "Search checks IDs, aliases, tags, channel names, and score-map keys.")
    found = False
    for tag, item in sorted(tags(files).items()):
        if query in tag.casefold() or query in str(item.get("description", "")).casefold():
            print(f"TAG {tag} parents={item.get('parent_tags', [])}")
            found = True
    for entry in entries(files):
        haystack = " ".join([entry.get("id", ""), *(entry.get("aliases") or []), *(entry.get("tags") or [])]).casefold()
        if query in haystack:
            print(
                f"ENTRY {entry.get('id')} tags={entry.get('tags', [])} "
                f"priority={entry.get('priority', 0)} score={entry.get('score', 1)}"
            )
            for alias in entry.get("aliases") or []:
                if query in alias.casefold():
                    print(f"  alias: {alias}")
            found = True
    for suppression in suppressions(files):
        haystack = " ".join(
            [suppression.get("id", ""), *(suppression.get("aliases") or []), *(suppression.get("unless_tags_any") or [])]
        ).casefold()
        if query in haystack:
            print(
                f"SUPPRESSION {suppression.get('id')} action={suppression.get('action', 'skip')} "
                f"unless={suppression.get('unless_tags_any', [])}"
            )
            for alias in suppression.get("aliases") or []:
                if query in alias.casefold():
                    print(f"  alias: {alias}")
            found = True
    for channel in channel_rules(files):
        channel_key = channel.get("channel_key", "")
        score_hits = score_map_hits(channel, query)
        if query in channel_key.casefold() or score_hits:
            print(f"CHANNEL {channel_key} class={channel.get('destination_class', 'primary')}")
            for hit in score_hits:
                print(f"  {hit}")
            found = True
    if not found:
        print("No matches.")
        return 1
    return 0


def cmd_show_tag(args: argparse.Namespace) -> int:
    files = load_files(args)
    item = tags(files).get(args.tag)
    if item is None:
        raise EditorError(f"unknown tag: {args.tag}")
    print(f"Tag: {args.tag}")
    print(f"Parents: {', '.join(item.get('parent_tags', [])) or 'none'}")
    if item.get("description"):
        print(f"Description: {item['description']}")
    emitters = [entry.get("id") for entry in entries(files) if args.tag in (entry.get("tags") or [])]
    channels = channel_tag_references(files, args.tag)
    print(f"Knowledge entries: {', '.join(emitters) or 'none'}")
    print(f"Channel references: {', '.join(channels) or 'none'}")
    return 0


def cmd_show_entry(args: argparse.Namespace) -> int:
    files = load_files(args)
    entry = find_entry(files, args.id)
    if entry is None:
        raise EditorError(f"unknown knowledge ID: {args.id}")
    print_entry(entry)
    refs = channel_term_references(files, args.id)
    print(f"Channel term references: {', '.join(refs) or 'none'}")
    return 0


def cmd_show_channel(args: argparse.Namespace) -> int:
    files = load_files(args)
    channel = find_channel(files, args.channel)
    if channel is None:
        raise EditorError(f"unknown channel: {args.channel}")
    print_channel(channel)
    return 0


def cmd_list_tags(args: argparse.Namespace) -> int:
    files = load_files(args)
    for tag, item in sorted(tags(files).items()):
        parents = ", ".join(item.get("parent_tags", [])) or "root"
        description = f" - {item['description']}" if item.get("description") else ""
        print(f"{tag} [{parents}]{description}")
    return 0


def cmd_list_entries(args: argparse.Namespace) -> int:
    files = load_files(args)
    for entry in sorted(entries(files), key=lambda item: item.get("id", "")):
        print(f"{entry.get('id')} tags={entry.get('tags', [])} aliases={len(entry.get('aliases') or [])}")
    return 0


def cmd_channel_scores(args: argparse.Namespace) -> int:
    files = load_files(args)
    channel = find_channel(files, args.channel)
    if channel is None:
        raise EditorError(f"unknown channel: {args.channel}")
    for key in ("tag_boosts", "tag_penalties", "concept_boosts", "concept_penalties", "term_boosts", "term_penalties"):
        print(f"{key}:")
        values = channel.get(key) or {}
        if not values:
            print("  none")
        for item, score in sorted(values.items()):
            print(f"  {item}: {score}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    files = load_files(args)
    issues = lint_files(files)
    if not issues:
        print("Routing lint OK.")
        return 0
    for issue in issues:
        print(f"{issue.severity}: {issue.message}")
        if issue.fix:
            print(f"  fix: {issue.fix}")
    if any(issue.severity == "ERROR" for issue in issues):
        return 1
    if getattr(args, "strict", False) and any(issue.severity == "WARN" for issue in issues):
        return 1
    return 0


def cmd_set_help(args: argparse.Namespace) -> int:
    settings_path = settings_file(args)
    current = load_settings(settings_path)
    current["coaching"] = args.state == "on"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(settings_path, current)
    print(f"Detailed editor help is now {args.state}.")
    return 0


def cmd_list_suppressions(args: argparse.Namespace) -> int:
    files = load_files(args)
    for item in sorted(suppressions(files), key=lambda value: value.get("id", "")):
        print(
            f"{item.get('id')} action={item.get('action', 'skip')} "
            f"aliases={len(item.get('aliases') or [])} unless={item.get('unless_tags_any', [])}"
        )
    return 0


def cmd_show_suppression(args: argparse.Namespace) -> int:
    files = load_files(args)
    item = find_suppression(files, args.id)
    if item is None:
        raise EditorError(f"unknown suppression ID: {args.id}")
    print(f"Suppression ID: {item.get('id')}")
    print(f"Action: {item.get('action', 'skip')}")
    print(f"Aliases: {', '.join(item.get('aliases') or [])}")
    print(f"Unless tags: {', '.join(item.get('unless_tags_any') or []) or 'none'}")
    print(f"Priority: {item.get('priority', 0)}")
    if item.get("description"):
        print(f"Description: {item['description']}")
    return 0


def cmd_add_suppression(args: argparse.Namespace) -> int:
    files = load_files(args)
    suppression_id = normalize_key(args.id)
    ensure_valid_key(suppression_id, "suppression ID")
    if find_suppression(files, suppression_id):
        raise EditorError(f"suppression ID already exists: {suppression_id}")
    aliases = unique_preserve(args.alias)
    candidate = deepcopy(files)
    unless_tags = [normalize_key(tag) for tag in args.unless_tag]
    for tag in unless_tags:
        if tag not in tags(candidate):
            raise EditorError(f"unknown unless tag: {tag}")
    item: dict[str, Any] = {
        "id": suppression_id,
        "aliases": aliases,
        "action": "skip",
        "unless_tags_any": unless_tags,
        "priority": args.priority,
    }
    if args.description:
        item["description"] = args.description
    suppressions(candidate).append(item)
    return save_candidate(args, files, candidate, [f"Add suppression {suppression_id}"])


def cmd_migrate(args: argparse.Namespace) -> int:
    files = load_files(args)
    candidate = deepcopy(files)
    changes = migrate_typed_routing(candidate)
    return save_candidate(args, files, candidate, changes or ["No migration changes"])


def migrate_typed_routing(files: RoutingFiles) -> list[str]:
    changes: list[str] = []
    taxonomy = set(tags(files))
    concept_ids = {entry.get("id") for entry in entries(files)}
    alias_to_concepts = alias_index(files)

    moved = move_skip_only_entries_to_suppressions(files)
    if moved:
        changes.append(f"Move {moved} skip-only false-positive knowledge entries to suppressions.json")

    for channel in channel_rules(files):
        channel_key = channel.get("channel_key", "")
        desired_profile = "source_mirror" if channel.get("destination_class") == "mirror" else PROFILE_BY_CHANNEL.get(channel_key)
        if desired_profile and channel.get("profile") != desired_profile:
            channel["profile"] = desired_profile
            changes.append(f"Set {channel_key} profile={channel['profile']}")

        split_legacy_list(channel, "required_any", "required_tags", "required_concepts", taxonomy, concept_ids)
        split_legacy_list(channel, "excluded_any", "excluded_tags", "excluded_concepts", taxonomy, concept_ids)
        split_legacy_score_map(channel, "term_boosts", "concept_boosts", concept_ids, alias_to_concepts)
        split_legacy_score_map(channel, "term_penalties", "concept_penalties", concept_ids, alias_to_concepts)

        if channel_key in {"sea", "air", "land"} and "active_conflict" in taxonomy:
            suppressions_list = unique_preserve([*(channel.get("suppress_when_tags_any") or []), "active_conflict"])
            if channel.get("suppress_when_tags_any") != suppressions_list:
                channel["suppress_when_tags_any"] = suppressions_list
                changes.append(f"Use active-conflict suppression policy on {channel_key}")
            channel["excluded_tags"] = [tag for tag in channel.get("excluded_tags", []) if tag != "active_conflict"]
            if not channel["excluded_tags"]:
                channel.pop("excluded_tags", None)

        if channel.get("profile") == "source_mirror" and (
            channel.get("required_source_ids") or channel.get("required_source_classes")
        ):
            if channel.pop("source_biases", None):
                changes.append(f"Remove source-name scoring from source mirror {channel_key}")
            if channel.pop("required_source_any", None):
                changes.append(f"Remove source-name gate from source mirror {channel_key}")
            for field in ("required_tags", "required_concepts", "excluded_tags", "excluded_concepts"):
                if channel.pop(field, None):
                    changes.append(f"Remove topical gate {field} from source mirror {channel_key}")
            for field in ("tag_boosts", "tag_penalties", "concept_boosts", "concept_penalties"):
                if channel.pop(field, None):
                    changes.append(f"Remove topical scoring {field} from source mirror {channel_key}")
            if channel.get("required_any") == []:
                channel.pop("required_any", None)

        prune_empty_channel_fields(channel)

    return changes


def move_skip_only_entries_to_suppressions(files: RoutingFiles) -> int:
    existing_suppressions = {item.get("id") for item in suppressions(files)}
    available_tags = set(tags(files))
    default_unless_tags = [tag for tag in ("military", "government", "disaster") if tag in available_tags]
    migrated_entries: list[dict[str, Any]] = []
    kept_entries: list[dict[str, Any]] = []
    for entry in entries(files):
        entry_tags = set(entry.get("tags") or [])
        entry_id = str(entry.get("id", ""))
        if entry_tags == {"skip_candidate"}:
            migrated_entries.append(entry)
        else:
            kept_entries.append(entry)
    if not migrated_entries:
        return 0
    files.knowledge["entries"] = kept_entries
    for entry in migrated_entries:
        entry_id = str(entry.get("id", ""))
        if entry_id in existing_suppressions:
            continue
        item: dict[str, Any] = {
            "id": entry_id,
            "aliases": entry.get("aliases") or [],
            "action": "skip",
            "unless_tags_any": default_unless_tags,
            "priority": entry.get("priority", 50),
        }
        if entry.get("description"):
            item["description"] = entry["description"]
        else:
            item["description"] = "Migrated false-positive suppression from knowledge_base.json."
        suppressions(files).append(item)
        existing_suppressions.add(entry_id)
    return len(migrated_entries)


def split_legacy_list(
    channel: dict[str, Any],
    legacy_field: str,
    tag_field: str,
    concept_field: str,
    taxonomy: set[str],
    concept_ids: set[str],
) -> None:
    values = channel.get(legacy_field) or []
    if not values:
        channel.pop(legacy_field, None)
        return
    legacy_left: list[str] = []
    tag_values = list(channel.get(tag_field) or [])
    concept_values = list(channel.get(concept_field) or [])
    for value in values:
        if value in taxonomy:
            tag_values.append(value)
        elif value in concept_ids:
            concept_values.append(value)
        else:
            legacy_left.append(value)
    if tag_values:
        channel[tag_field] = unique_preserve(tag_values)
    if concept_values:
        channel[concept_field] = unique_preserve(concept_values)
    if legacy_left:
        channel[legacy_field] = unique_preserve(legacy_left)
    else:
        channel.pop(legacy_field, None)


def split_legacy_score_map(
    channel: dict[str, Any],
    legacy_field: str,
    concept_field: str,
    concept_ids: set[str],
    alias_to_concepts: dict[str, set[str]],
) -> None:
    values = channel.get(legacy_field) or {}
    if not values:
        channel.pop(legacy_field, None)
        return
    concept_values = dict(channel.get(concept_field) or {})
    legacy_left: dict[str, int] = {}
    for key, score in values.items():
        if key in concept_ids:
            concept_values[key] = score
            continue
        owners = alias_to_concepts.get(str(key).casefold(), set())
        if len(owners) == 1:
            concept_values[next(iter(owners))] = score
            continue
        legacy_left[key] = score
    if concept_values:
        channel[concept_field] = concept_values
    if legacy_left:
        channel[legacy_field] = legacy_left
    else:
        channel.pop(legacy_field, None)


def prune_empty_channel_fields(channel: dict[str, Any]) -> None:
    for field in (
        "required_any",
        "excluded_any",
        "required_tags",
        "required_concepts",
        "excluded_tags",
        "excluded_concepts",
        "suppress_when_tags_any",
    ):
        if channel.get(field) == []:
            channel.pop(field, None)
    for field in (
        "term_boosts",
        "term_penalties",
        "concept_boosts",
        "concept_penalties",
        "tag_boosts",
        "tag_penalties",
        "source_biases",
    ):
        if channel.get(field) == {}:
            channel.pop(field, None)


def alias_index(files: RoutingFiles) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for entry in entries(files):
        entry_id = str(entry.get("id", ""))
        for alias in entry.get("aliases") or []:
            result.setdefault(str(alias).casefold(), set()).add(entry_id)
    return result


def cmd_help_topic(args: argparse.Namespace) -> int:
    if not args.topic:
        print("Available help topics:")
        for topic in sorted(HELP_TOPICS):
            print(f"  {topic}")
        return 0
    print(f"{args.topic}: {HELP_TOPICS[args.topic]}")
    return 0


def cmd_add_tag(args: argparse.Namespace) -> int:
    files = load_files(args)
    tag = normalize_key(args.tag)
    ensure_valid_key(tag, "tag")
    if tag in tags(files):
        raise EditorError(f"tag already exists: {tag}")
    parent_tags = split_values(args.parent, args.parents)
    for parent in parent_tags:
        ensure_valid_key(parent, "parent tag")
    candidate = deepcopy(files)
    tag_obj: dict[str, Any] = {"parent_tags": parent_tags}
    if args.description:
        tag_obj["description"] = args.description
    tags(candidate)[tag] = tag_obj
    return save_candidate(args, files, candidate, [f"Add taxonomy tag {tag}"])


def cmd_add_entry(args: argparse.Namespace) -> int:
    files = load_files(args)
    entry_id = normalize_key(args.id)
    ensure_valid_key(entry_id, "knowledge ID")
    if find_entry(files, entry_id):
        raise EditorError(f"knowledge ID already exists: {entry_id}")
    aliases = unique_preserve(args.alias)
    if not aliases:
        raise EditorError("at least one alias is required")
    ensure_aliases_available(files, aliases)
    candidate = deepcopy(files)
    entry_tags = [normalize_key(tag) for tag in args.tag]
    missing = [tag for tag in entry_tags if tag not in tags(candidate)]
    if missing and not args.create_missing_tags:
        raise EditorError(
            "unknown tags: "
            + ", ".join(missing)
            + ". Re-run with --create-missing-tags or add the tags first."
        )
    for tag in missing:
        tags(candidate)[tag] = {"parent_tags": ["world"] if "world" in tags(candidate) else []}
    entry: dict[str, Any] = {
        "id": entry_id,
        "aliases": aliases,
        "tags": entry_tags,
        "priority": args.priority,
        "score": args.score,
    }
    if args.description:
        entry["description"] = args.description
    entries(candidate).append(entry)
    changes = [f"Add knowledge ID {entry_id}"]
    changes.extend(f"Create missing tag {tag}" for tag in missing)
    return save_candidate(args, files, candidate, changes)


def cmd_add_alias(args: argparse.Namespace) -> int:
    files = load_files(args)
    candidate = deepcopy(files)
    entry = find_entry(candidate, args.id)
    if entry is None:
        raise EditorError(f"unknown knowledge ID: {args.id}")
    aliases = unique_preserve(args.alias)
    ensure_aliases_available(files, aliases, allowed_id=args.id)
    existing = entry.setdefault("aliases", [])
    for alias in aliases:
        if alias.casefold() not in {value.casefold() for value in existing}:
            existing.append(alias)
    return save_candidate(args, files, candidate, [f"Add {len(aliases)} alias(es) to {args.id}"])


def cmd_set_channel_score(args: argparse.Namespace) -> int:
    files = load_files(args)
    candidate = deepcopy(files)
    channel = find_channel(candidate, args.channel)
    if channel is None:
        raise EditorError(f"unknown channel: {args.channel}")
    map_name = CHANNEL_MAPS[args.kind]
    key = normalize_key(args.key) if args.kind.startswith(("tag_", "concept_")) else args.key
    if args.kind.startswith("tag_") and key not in tags(candidate):
        if not args.create_missing_tag:
            raise EditorError(f"unknown tag: {key}. Re-run with --create-missing-tag or add it first.")
        tags(candidate)[key] = {"parent_tags": ["world"] if "world" in tags(candidate) else []}
    if args.kind.startswith("concept_") and find_entry(candidate, key) is None:
        raise EditorError(f"unknown knowledge ID for concept score: {key}")
    channel.setdefault(map_name, {})[key] = args.score
    changes = [f"Set {args.channel}.{map_name}.{key} = {args.score}"]
    return save_candidate(args, files, candidate, changes)


def cmd_rename_tag(args: argparse.Namespace) -> int:
    files = load_files(args)
    old = normalize_key(args.old)
    new = normalize_key(args.new)
    ensure_valid_key(new, "new tag")
    if old not in tags(files):
        raise EditorError(f"unknown tag: {old}")
    if new in tags(files):
        raise EditorError(f"tag already exists: {new}")
    candidate = deepcopy(files)
    tag_obj = tags(candidate).pop(old)
    tags(candidate)[new] = tag_obj
    for item in tags(candidate).values():
        item["parent_tags"] = replace_list_value(item.get("parent_tags", []), old, new)
    for entry in entries(candidate):
        entry["tags"] = replace_list_value(entry.get("tags", []), old, new)
    replace_channel_tag_references(candidate, old, new)
    return save_candidate(args, files, candidate, [f"Rename tag {old} -> {new} everywhere"])


def cmd_rename_entry(args: argparse.Namespace) -> int:
    files = load_files(args)
    old = normalize_key(args.old)
    new = normalize_key(args.new)
    ensure_valid_key(new, "new knowledge ID")
    if find_entry(files, old) is None:
        raise EditorError(f"unknown knowledge ID: {old}")
    if find_entry(files, new) is not None:
        raise EditorError(f"knowledge ID already exists: {new}")
    candidate = deepcopy(files)
    find_entry(candidate, old)["id"] = new  # type: ignore[index]
    for channel in channel_rules(candidate):
        for map_name in ("term_boosts", "term_penalties", "concept_boosts", "concept_penalties"):
            score_map = channel.get(map_name) or {}
            if old in score_map:
                score_map[new] = score_map.pop(old)
                channel[map_name] = score_map
        for field in ("required_any", "excluded_any", "required_concepts", "excluded_concepts"):
            channel[field] = replace_list_value(channel.get(field, []), old, new)
    return save_candidate(args, files, candidate, [f"Rename knowledge ID {old} -> {new} everywhere"])


def cmd_route_test(args: argparse.Namespace) -> int:
    app_config = load_config(Path(args.config_path))
    routing_config = load_routing_config(Path(args.routing_dir), app_config)
    decision = RoutingEngine(routing_config).route(
        RoutingArticle(
            title=args.title,
            summary=args.summary,
            source_name=args.source,
            source_id=args.source_id,
            source_class=args.source_class,
            url=args.url,
        )
    )
    print(format_decision(decision, limit=10000))
    return 0


def run_wizard(args: argparse.Namespace) -> int:
    print("RSS Bot Routing Editor")
    print("======================")
    print_coaching(
        args,
        "Use this wizard to inspect and edit routing config safely. "
        "Every save is previewed, backed up, and validated before it replaces the live config files.",
    )
    while True:
        print(
            "\nChoose an action:\n"
            "  1. Find tags, IDs, aliases, or channels\n"
            "  2. Add alias to existing knowledge ID\n"
            "  3. Add new knowledge ID\n"
            "  4. Add new taxonomy tag\n"
            "  5. Set channel boost or penalty\n"
            "  6. Rename tag everywhere\n"
            "  7. Rename knowledge ID everywhere\n"
            "  8. Route-test a title\n"
            "  9. Lint routing config\n"
            "  ?. Show routing help\n"
            "  h. Toggle detailed help\n"
            "  q. Quit"
        )
        choice = input("> ").strip().casefold()
        try:
            if choice == "1":
                print_coaching(args, HELP_TOPICS["ids"])
                args.query = input("Search query: ").strip()
                cmd_find(args)
            elif choice == "2":
                print_coaching(args, HELP_TOPICS["aliases"])
                args.id = input("Knowledge ID: ").strip()
                args.alias = prompt_repeated("Alias phrase")
                cmd_add_alias(args)
            elif choice == "3":
                print_coaching(args, HELP_TOPICS["ids"])
                print_coaching(args, HELP_TOPICS["tags"])
                args.id = input("New knowledge ID: ").strip()
                args.alias = prompt_repeated("Alias phrase")
                args.tag = prompt_repeated("Tag")
                args.priority = prompt_int("Priority", 10)
                args.score = prompt_int("Score", 1)
                args.description = prompt_optional("Description")
                args.create_missing_tags = yes_no("Create missing tags if needed?", default=True)
                cmd_add_entry(args)
            elif choice == "4":
                print_coaching(args, HELP_TOPICS["tags"])
                args.tag = input("New tag: ").strip()
                args.parent = prompt_repeated("Parent tag", allow_empty=True)
                args.parents = None
                args.description = prompt_optional("Description")
                cmd_add_tag(args)
            elif choice == "5":
                print_coaching(args, HELP_TOPICS["scores"])
                args.channel = input("Channel key: ").strip()
                args.kind = prompt_choice("Kind", sorted(SCORE_KINDS))
                args.key = input("Tag or term key: ").strip()
                args.score = prompt_int("Score", 1)
                args.create_missing_tag = args.kind.startswith("tag_") and yes_no("Create missing tag if needed?", True)
                cmd_set_channel_score(args)
            elif choice == "6":
                print_coaching(args, HELP_TOPICS["ripple"])
                args.old = input("Old tag: ").strip()
                args.new = input("New tag: ").strip()
                cmd_rename_tag(args)
            elif choice == "7":
                print_coaching(args, HELP_TOPICS["ripple"])
                args.old = input("Old knowledge ID: ").strip()
                args.new = input("New knowledge ID: ").strip()
                cmd_rename_entry(args)
            elif choice == "8":
                print_coaching(args, "Route tests show the exact matched IDs, emitted tags, channel scores, and final destinations.")
                args.title = input("Title: ").strip()
                args.summary = prompt_optional("Summary")
                args.source = prompt_optional("Source name")
                args.source_id = prompt_optional("Source ID")
                args.source_class = prompt_optional("Source class")
                args.url = prompt_optional("URL")
                cmd_route_test(args)
            elif choice == "9":
                cmd_lint(args)
            elif choice == "?":
                cmd_help_topic(argparse.Namespace(topic=None))
                topic = input("Topic to explain (blank to return): ").strip().casefold()
                if topic:
                    if topic in HELP_TOPICS:
                        cmd_help_topic(argparse.Namespace(topic=topic))
                    else:
                        print("Unknown help topic.")
            elif choice == "h":
                current = coaching_enabled(args)
                args.state = "off" if current else "on"
                cmd_set_help(args)
            elif choice == "q":
                return 0
            else:
                print("Unknown choice.")
        except EditorError as exc:
            print(f"ERROR: {exc}")
        except RoutingConfigError as exc:
            for error in exc.errors:
                print(f"ERROR: {error}")


def save_candidate(args: argparse.Namespace, original: RoutingFiles, candidate: RoutingFiles, changes: list[str]) -> int:
    validate_candidate(args, candidate)
    changed = changed_files(original, candidate)
    if not changed:
        print("No changes needed.")
        return 0
    print("Planned changes:")
    for change in changes:
        print(f"  - {change}")
    print_preview(original, candidate, changed)
    if not args.yes and not yes_no("Save these changes?", default=False):
        print("No files changed.")
        return 1
    write_changed_files(original.routing_dir, changed, candidate)
    print("Saved routing changes.")
    print("Next checks:")
    print("  python -m app.main --validate-routing")
    print("  python -m app.routing_editor route-test \"Your title here\"")
    print("To make config-only routing changes live:")
    print("  powershell -NoProfile -ExecutionPolicy Bypass -File .\\ops\\apply-routing-changes.ps1")
    return 0


def validate_candidate(args: argparse.Namespace, candidate: RoutingFiles) -> None:
    app_config = load_config(Path(args.config_path))
    with tempfile.TemporaryDirectory(prefix="rssbot-routing-editor-") as temp_dir:
        temp = Path(temp_dir)
        write_json(temp / "taxonomy.json", candidate.taxonomy)
        write_json(temp / "knowledge_base.json", candidate.knowledge)
        write_json(temp / "channels.json", candidate.channels)
        load_routing_config(temp, app_config)


def changed_files(original: RoutingFiles, candidate: RoutingFiles) -> list[str]:
    changed: list[str] = []
    for name, original_data in original.as_named().items():
        if original_data != candidate.as_named()[name]:
            changed.append(name)
    return changed


def write_changed_files(routing_dir: Path, changed: Iterable[str], candidate: RoutingFiles) -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = routing_dir / ".editor_backups" / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in changed:
        path = routing_dir / name
        backup = backup_dir / name
        if path.exists():
            shutil.copy2(path, backup)
        temp = path.with_suffix(path.suffix + ".tmp")
        write_json(temp, candidate.as_named()[name])
        temp.replace(path)


def print_preview(original: RoutingFiles, candidate: RoutingFiles, changed: Iterable[str]) -> None:
    for name in changed:
        before = json.dumps(original.as_named()[name], indent=2, ensure_ascii=False).splitlines()
        after = json.dumps(candidate.as_named()[name], indent=2, ensure_ascii=False).splitlines()
        diff = list(difflib.unified_diff(before, after, fromfile=name, tofile=name, lineterm=""))
        print(f"\n{name} preview:")
        for line in diff[:160]:
            print(line)
        if len(diff) > 160:
            print(f"... {len(diff) - 160} more diff lines omitted")


def lint_files(files: RoutingFiles) -> list[LintIssue]:
    issues: list[LintIssue] = []
    taxonomy = tags(files)
    entry_ids: set[str] = set()
    alias_owner: dict[str, str] = {}
    alias_occurrences: dict[str, list[tuple[str, str]]] = {}
    emitted_tags: set[str] = set()
    scored_tags: set[str] = set()
    referenced_tags: set[str] = set()

    for tag, item in taxonomy.items():
        if not KEY_RE.fullmatch(tag):
            issues.append(LintIssue("ERROR", f"taxonomy tag has invalid key: {tag}"))
        parents = item.get("parent_tags", [])
        if not isinstance(parents, list):
            issues.append(LintIssue("ERROR", f"{tag}.parent_tags must be a list"))
            continue
        for parent in parents:
            referenced_tags.add(parent)
            if parent not in taxonomy:
                issues.append(
                    LintIssue("ERROR", f"tag {tag} references unknown parent tag {parent}", f"add-tag {parent}")
                )

    for entry in entries(files):
        entry_id = str(entry.get("id", ""))
        if entry_id in entry_ids:
            issues.append(LintIssue("ERROR", f"duplicate knowledge ID: {entry_id}"))
        entry_ids.add(entry_id)
        if not KEY_RE.fullmatch(entry_id):
            issues.append(LintIssue("ERROR", f"knowledge ID has invalid key: {entry_id}"))
        for alias in entry.get("aliases") or []:
            alias_key = str(alias).casefold()
            alias_occurrences.setdefault(alias_key, []).append((entry_id, str(alias)))
            if alias_key in alias_owner and alias_owner[alias_key] != entry_id:
                issues.append(
                    LintIssue("WARN", f"alias {alias!r} is used by both {alias_owner[alias_key]} and {entry_id}")
                )
            alias_owner[alias_key] = entry_id
            if len(str(alias).strip()) <= 2:
                issues.append(LintIssue("WARN", f"very short alias {alias!r} on {entry_id} can over-match"))
        for tag in entry.get("tags") or []:
            emitted_tags.add(tag)
            referenced_tags.add(tag)
            if tag not in taxonomy:
                issues.append(
                    LintIssue("ERROR", f"knowledge ID {entry_id} emits unknown tag {tag}", f"add-tag {tag}")
                )
        if set(entry.get("tags") or []) == {"skip_candidate"}:
            issues.append(
                LintIssue(
                    "WARN",
                    f"knowledge ID {entry_id} only emits skip_candidate; move it to suppressions.json",
                    "migrate-typed-routing",
                )
            )

    for item in suppressions(files):
        suppression_id = str(item.get("id", ""))
        if not KEY_RE.fullmatch(suppression_id):
            issues.append(LintIssue("ERROR", f"suppression ID has invalid key: {suppression_id}"))
        for tag in item.get("unless_tags_any") or []:
            if tag not in taxonomy:
                issues.append(LintIssue("ERROR", f"suppression {suppression_id} references unknown unless tag {tag}"))
        for alias in item.get("aliases") or []:
            alias_occurrences.setdefault(str(alias).casefold(), []).append((suppression_id, str(alias)))

    for channel in channel_rules(files):
        channel_key = channel.get("channel_key", "unknown")
        if channel.get("required_any"):
            issues.append(LintIssue("WARN", f"channel {channel_key}.required_any is legacy; use required_tags/concepts"))
        if channel.get("excluded_any"):
            issues.append(LintIssue("WARN", f"channel {channel_key}.excluded_any is legacy; use excluded_tags/concepts"))
        if channel.get("term_boosts"):
            issues.append(LintIssue("WARN", f"channel {channel_key}.term_boosts is legacy; use concept_boosts"))
        if channel.get("term_penalties"):
            issues.append(LintIssue("WARN", f"channel {channel_key}.term_penalties is legacy; use concept_penalties"))
        if channel.get("destination_class") == "mirror":
            if channel.get("enabled") is False:
                continue
            has_source_gate = bool(channel.get("required_source_ids") or channel.get("required_source_classes"))
            if channel.get("tag_boosts") or channel.get("concept_boosts") or channel.get("term_boosts"):
                issues.append(LintIssue("WARN", f"mirror channel {channel_key} uses topical scoring; prefer source gates only"))
            if not has_source_gate:
                issues.append(LintIssue("WARN", f"mirror channel {channel_key} has no source ID/class gate"))
            if channel.get("required_source_any") and has_source_gate:
                issues.append(LintIssue("WARN", f"mirror channel {channel_key} mixes source-name hints with stable source gates"))
        source_gate_count = sum(
            1
            for field in ("required_source_any", "required_source_ids", "required_source_classes")
            if channel.get(field)
        )
        if source_gate_count > 1 and channel.get("destination_class") != "mirror":
            issues.append(LintIssue("WARN", f"channel {channel_key} mixes multiple source gate styles"))
        for tag in channel.get("required_tags") or []:
            referenced_tags.add(tag)
            if tag not in taxonomy:
                issues.append(LintIssue("ERROR", f"channel {channel_key}.required_tags references unknown tag {tag}"))
        for tag in channel.get("excluded_tags") or []:
            referenced_tags.add(tag)
            if tag not in taxonomy:
                issues.append(LintIssue("ERROR", f"channel {channel_key}.excluded_tags references unknown tag {tag}"))
        for tag in channel.get("suppress_when_tags_any") or []:
            referenced_tags.add(tag)
            if tag not in taxonomy:
                issues.append(LintIssue("ERROR", f"channel {channel_key}.suppress_when_tags_any references unknown tag {tag}"))
        for concept in channel.get("required_concepts") or []:
            if concept not in entry_ids:
                issues.append(
                    LintIssue("ERROR", f"channel {channel_key}.required_concepts references unknown knowledge ID {concept}")
                )
        for concept in channel.get("excluded_concepts") or []:
            if concept not in entry_ids:
                issues.append(
                    LintIssue("ERROR", f"channel {channel_key}.excluded_concepts references unknown knowledge ID {concept}")
                )
        for map_name in ("concept_boosts", "concept_penalties"):
            for concept in (channel.get(map_name) or {}):
                if concept not in entry_ids:
                    issues.append(
                        LintIssue("ERROR", f"channel {channel_key}.{map_name} references unknown knowledge ID: {concept}")
                    )
        for map_name in ("tag_boosts", "tag_penalties"):
            for tag in (channel.get(map_name) or {}):
                referenced_tags.add(tag)
                scored_tags.add(tag)
                if tag not in taxonomy:
                    issues.append(
                        LintIssue("ERROR", f"channel {channel_key}.{map_name} references unknown tag {tag}", f"add-tag {tag}")
                    )
        for map_name in ("term_boosts", "term_penalties"):
            for term in (channel.get(map_name) or {}):
                if term in entry_ids:
                    continue
                owners = alias_occurrences.get(str(term).casefold(), [])
                if owners:
                    issues.append(
                        LintIssue(
                            "WARN",
                            f"channel {channel_key}.{map_name} uses alias string {term!r}; use concept ID {owners[0][0]}",
                        )
                    )
                else:
                    issues.append(
                        LintIssue("WARN", f"channel {channel_key}.{map_name} references no known knowledge ID: {term}")
                    )
        for field in ("required_any", "excluded_any"):
            for value in channel.get(field) or []:
                if value in taxonomy:
                    referenced_tags.add(value)
                elif value not in entry_ids:
                    owners = alias_occurrences.get(str(value).casefold(), [])
                    if owners:
                        issues.append(
                            LintIssue(
                                "WARN",
                                f"channel {channel_key}.{field} uses alias string {value!r}; use typed tag/concept fields",
                            )
                        )
                    else:
                        issues.append(
                            LintIssue(
                                "WARN",
                                f"channel {channel_key}.{field} references neither a known tag nor knowledge ID: {value}",
                            )
                        )
    for field in ("review_tags", "skip_tags"):
        for tag in files.channels.get(field) or []:
            referenced_tags.add(tag)
            if tag not in taxonomy:
                issues.append(LintIssue("ERROR", f"channels.{field} references unknown tag {tag}", f"add-tag {tag}"))

    for tag in sorted(set(taxonomy) - referenced_tags):
        issues.append(LintIssue("WARN", f"tag exists but is not referenced anywhere: {tag}"))
    for tag in sorted(emitted_tags - scored_tags):
        issues.append(LintIssue("WARN", f"tag is emitted by knowledge but not directly scored by any channel: {tag}"))
    return issues


def load_files(args: argparse.Namespace) -> RoutingFiles:
    routing_dir = Path(args.routing_dir)
    return RoutingFiles(
        routing_dir=routing_dir,
        taxonomy=read_json(routing_dir / "taxonomy.json"),
        knowledge=read_json(routing_dir / "knowledge_base.json"),
        channels=read_json(routing_dir / "channels.json"),
        suppressions=read_optional_json(routing_dir / "suppressions.json"),
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EditorError(f"routing file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EditorError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise EditorError(f"{path}: root must be a JSON object")
    return data


def read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": []}
    return read_json(path)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def settings_file(args: argparse.Namespace) -> Path:
    return Path(args.routing_dir) / "editor_settings.json"


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"coaching": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"coaching": True}
    return data if isinstance(data, dict) else {"coaching": True}


def coaching_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_coaching", False):
        return False
    return bool(load_settings(settings_file(args)).get("coaching", True))


def print_coaching(args: argparse.Namespace, message: str) -> None:
    if coaching_enabled(args):
        print(f"Help: {message}")


def tags(files: RoutingFiles) -> dict[str, Any]:
    value = files.taxonomy.setdefault("tags", {})
    if not isinstance(value, dict):
        raise EditorError("taxonomy.tags must be an object")
    return value


def entries(files: RoutingFiles) -> list[dict[str, Any]]:
    value = files.knowledge.setdefault("entries", [])
    if not isinstance(value, list):
        raise EditorError("knowledge_base.entries must be an array")
    return value


def suppressions(files: RoutingFiles) -> list[dict[str, Any]]:
    value = files.suppressions.setdefault("entries", [])
    if not isinstance(value, list):
        raise EditorError("suppressions.entries must be an array")
    return value


def channel_rules(files: RoutingFiles) -> list[dict[str, Any]]:
    value = files.channels.setdefault("channels", [])
    if not isinstance(value, list):
        raise EditorError("channels.channels must be an array")
    return value


def find_entry(files: RoutingFiles, entry_id: str) -> dict[str, Any] | None:
    folded = entry_id.casefold()
    for entry in entries(files):
        if str(entry.get("id", "")).casefold() == folded:
            return entry
    return None


def find_suppression(files: RoutingFiles, suppression_id: str) -> dict[str, Any] | None:
    folded = suppression_id.casefold()
    for item in suppressions(files):
        if str(item.get("id", "")).casefold() == folded:
            return item
    return None


def find_channel(files: RoutingFiles, channel_key: str) -> dict[str, Any] | None:
    folded = channel_key.casefold()
    for channel in channel_rules(files):
        if str(channel.get("channel_key", "")).casefold() == folded:
            return channel
    return None


def print_entry(entry: dict[str, Any]) -> None:
    print(f"Knowledge ID: {entry.get('id')}")
    print(f"Aliases: {', '.join(entry.get('aliases') or [])}")
    print(f"Tags: {', '.join(entry.get('tags') or [])}")
    print(f"Priority: {entry.get('priority', 0)}")
    print(f"Score: {entry.get('score', 1)}")
    if entry.get("description"):
        print(f"Description: {entry['description']}")


def print_channel(channel: dict[str, Any]) -> None:
    print(f"Channel: {channel.get('channel_key')}")
    print(f"Class: {channel.get('destination_class', 'primary')}")
    if channel.get("profile"):
        print(f"Profile: {channel['profile']}")
    print(f"Priority: {channel.get('priority', 0)}")
    print(f"Minimum score: {channel.get('minimum_score', 'default')}")
    for field in (
        "required_tags",
        "required_concepts",
        "excluded_tags",
        "excluded_concepts",
        "suppress_when_tags_any",
        "required_any",
        "excluded_any",
        "required_source_ids",
        "excluded_source_ids",
        "required_source_classes",
        "excluded_source_classes",
        "required_source_any",
    ):
        if channel.get(field):
            print(f"{field}: {', '.join(channel[field])}")
    for map_name in ("tag_boosts", "tag_penalties", "concept_boosts", "concept_penalties", "term_boosts", "term_penalties"):
        if channel.get(map_name):
            print(f"{map_name}: {channel[map_name]}")


def score_map_hits(channel: dict[str, Any], query: str) -> list[str]:
    hits: list[str] = []
    for map_name in ("tag_boosts", "tag_penalties", "concept_boosts", "concept_penalties", "term_boosts", "term_penalties"):
        for key, value in (channel.get(map_name) or {}).items():
            if query in str(key).casefold():
                hits.append(f"{map_name}.{key}={value}")
    return hits


def channel_tag_references(files: RoutingFiles, tag: str) -> list[str]:
    refs: list[str] = []
    for channel in channel_rules(files):
        channel_key = channel.get("channel_key", "unknown")
        for map_name in ("tag_boosts", "tag_penalties"):
            if tag in (channel.get(map_name) or {}):
                refs.append(f"{channel_key}.{map_name}")
        for field in ("required_any", "excluded_any"):
            if tag in (channel.get(field) or []):
                refs.append(f"{channel_key}.{field}")
    return refs


def channel_term_references(files: RoutingFiles, entry_id: str) -> list[str]:
    refs: list[str] = []
    for channel in channel_rules(files):
        channel_key = channel.get("channel_key", "unknown")
        for map_name in ("term_boosts", "term_penalties"):
            if entry_id in (channel.get(map_name) or {}):
                refs.append(f"{channel_key}.{map_name}")
        for field in ("required_any", "excluded_any"):
            if entry_id in (channel.get(field) or []):
                refs.append(f"{channel_key}.{field}")
    return refs


def replace_channel_tag_references(files: RoutingFiles, old: str, new: str) -> None:
    for field in ("review_tags", "skip_tags"):
        files.channels[field] = replace_list_value(files.channels.get(field, []), old, new)
    for item in suppressions(files):
        item["unless_tags_any"] = replace_list_value(item.get("unless_tags_any", []), old, new)
    for channel in channel_rules(files):
        for map_name in ("tag_boosts", "tag_penalties"):
            score_map = channel.get(map_name) or {}
            if old in score_map:
                score_map[new] = score_map.pop(old)
                channel[map_name] = score_map
        for field in ("required_any", "excluded_any", "required_tags", "excluded_tags", "suppress_when_tags_any"):
            channel[field] = replace_list_value(channel.get(field, []), old, new)


def replace_list_value(values: Iterable[str], old: str, new: str) -> list[str]:
    return [new if value == old else value for value in values]


def split_values(repeated: list[str], comma_values: str | None) -> list[str]:
    values = list(repeated)
    if comma_values:
        values.extend(value.strip() for value in comma_values.split(","))
    return [normalize_key(value) for value in unique_preserve(value for value in values if value.strip())]


def normalize_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def ensure_valid_key(value: str, label: str) -> None:
    if not KEY_RE.fullmatch(value):
        raise EditorError(f"{label} must use lowercase letters, numbers, hyphens, or underscores: {value}")


def ensure_aliases_available(files: RoutingFiles, aliases: Iterable[str], allowed_id: str | None = None) -> None:
    owners: dict[str, str] = {}
    for entry in entries(files):
        entry_id = str(entry.get("id", ""))
        for alias in entry.get("aliases") or []:
            owners[str(alias).casefold()] = entry_id
    for alias in aliases:
        owner = owners.get(alias.casefold())
        if owner and owner != allowed_id:
            raise EditorError(f"alias {alias!r} already belongs to {owner}")


def unique_preserve(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def prompt_repeated(label: str, *, allow_empty: bool = False) -> list[str]:
    print(f"{label}s. Enter one per line; leave blank when done.")
    values: list[str] = []
    while True:
        value = input(f"{label}: ").strip()
        if not value:
            if values or allow_empty:
                return values
            print("At least one value is required.")
            continue
        values.append(value)


def prompt_optional(label: str) -> str | None:
    value = input(f"{label} (optional): ").strip()
    return value or None


def prompt_int(label: str, default: int) -> int:
    value = input(f"{label} [{default}]: ").strip()
    return int(value) if value else default


def prompt_choice(label: str, choices: list[str]) -> str:
    while True:
        print(f"{label}: {', '.join(choices)}")
        value = input("> ").strip()
        if value in choices:
            return value
        print("Choose one of the listed values.")


def yes_no(question: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{question} [{suffix}] ").strip().casefold()
    if not value:
        return default
    return value in {"y", "yes"}


class EditorError(Exception):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
