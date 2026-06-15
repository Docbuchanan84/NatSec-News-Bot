from __future__ import annotations

import json
from pathlib import Path

from app import routing_editor
from app.routing import load_routing_config
from app.config_loader import load_config


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def make_config(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "config.json"
    routing_dir = tmp_path / "routing"
    write_json(
        config_path,
        {
            "version": 1,
            "channels": [
                {
                    "key": "europe",
                    "name": "Europe",
                    "discordChannelId": "1" * 18,
                    "feeds": [{"name": "Feed", "url": "https://example.com/feed.rss"}],
                },
                {
                    "key": "air",
                    "name": "Air",
                    "discordChannelId": "2" * 18,
                    "feeds": [{"name": "Feed", "url": "https://example.com/air.rss"}],
                },
            ],
        },
    )
    write_json(
        routing_dir / "taxonomy.json",
        {
            "version": 1,
            "tags": {
                "world": {"parent_tags": []},
                "europe": {"parent_tags": ["world"]},
                "military": {"parent_tags": ["world"]},
                "air": {"parent_tags": ["military"]},
                "skip_candidate": {"parent_tags": []},
                "review_required": {"parent_tags": []},
                "ambiguous": {"parent_tags": ["review_required"]},
            },
        },
    )
    write_json(
        routing_dir / "knowledge_base.json",
        {
            "version": 1,
            "entries": [
                {
                    "id": "region_europe",
                    "aliases": ["Europe"],
                    "tags": ["europe"],
                    "priority": 10,
                    "score": 1,
                }
            ],
        },
    )
    write_json(
        routing_dir / "channels.json",
        {
            "version": 1,
            "max_destinations": 2,
            "review_tags": ["review_required", "ambiguous"],
            "skip_tags": ["skip_candidate"],
            "rule_defaults": {"enabled": True, "minimum_score": 4},
            "channels": [
                {
                    "channel_key": "europe",
                    "priority": 90,
                    "tag_boosts": {"europe": 5},
                    "required_any": ["europe"],
                },
                {
                    "channel_key": "air",
                    "priority": 80,
                    "tag_boosts": {"air": 5},
                    "required_any": ["air"],
                },
            ],
        },
    )
    return config_path, routing_dir


def base_args(config_path: Path, routing_dir: Path) -> list[str]:
    return ["--config-path", str(config_path), "--routing-dir", str(routing_dir), "--yes"]


def assert_valid(config_path: Path, routing_dir: Path) -> None:
    load_routing_config(routing_dir, load_config(config_path))


def test_add_tag_writes_valid_taxonomy(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)

    result = routing_editor.main(
        base_args(config_path, routing_dir)
        + ["add-tag", "switzerland", "--parent", "europe", "--description", "Swiss national coverage."]
    )

    assert result == 0
    assert_valid(config_path, routing_dir)
    taxonomy = json.loads((routing_dir / "taxonomy.json").read_text(encoding="utf-8"))
    assert taxonomy["tags"]["switzerland"]["parent_tags"] == ["europe"]
    assert (routing_dir / ".editor_backups").exists()


def test_add_entry_can_create_missing_tag(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)

    result = routing_editor.main(
        base_args(config_path, routing_dir)
        + [
            "add-entry",
            "switzerland",
            "--alias",
            "Switzerland",
            "--tag",
            "switzerland",
            "--priority",
            "40",
            "--score",
            "3",
            "--create-missing-tags",
        ]
    )

    assert result == 0
    assert_valid(config_path, routing_dir)
    knowledge = json.loads((routing_dir / "knowledge_base.json").read_text(encoding="utf-8"))
    assert any(entry["id"] == "switzerland" for entry in knowledge["entries"])
    taxonomy = json.loads((routing_dir / "taxonomy.json").read_text(encoding="utf-8"))
    assert "switzerland" in taxonomy["tags"]


def test_add_alias_rejects_duplicate_alias(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)

    result = routing_editor.main(base_args(config_path, routing_dir) + ["add-alias", "region_europe", "--alias", "Europe"])

    assert result == 0
    knowledge = json.loads((routing_dir / "knowledge_base.json").read_text(encoding="utf-8"))
    assert knowledge["entries"][0]["aliases"] == ["Europe"]


def test_set_channel_score_updates_score_map(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)
    routing_editor.main(base_args(config_path, routing_dir) + ["add-tag", "switzerland", "--parent", "europe"])

    result = routing_editor.main(
        base_args(config_path, routing_dir)
        + ["set-channel-score", "europe", "--kind", "tag_boost", "--key", "switzerland", "--score", "7"]
    )

    assert result == 0
    assert_valid(config_path, routing_dir)
    channels = json.loads((routing_dir / "channels.json").read_text(encoding="utf-8"))
    europe = channels["channels"][0]
    assert europe["tag_boosts"]["switzerland"] == 7


def test_rename_tag_ripples_across_files(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)

    result = routing_editor.main(base_args(config_path, routing_dir) + ["rename-tag", "europe", "region_europe_tag"])

    assert result == 0
    assert_valid(config_path, routing_dir)
    taxonomy = json.loads((routing_dir / "taxonomy.json").read_text(encoding="utf-8"))
    knowledge = json.loads((routing_dir / "knowledge_base.json").read_text(encoding="utf-8"))
    channels = json.loads((routing_dir / "channels.json").read_text(encoding="utf-8"))
    assert "region_europe_tag" in taxonomy["tags"]
    assert knowledge["entries"][0]["tags"] == ["region_europe_tag"]
    assert channels["channels"][0]["tag_boosts"]["region_europe_tag"] == 5
    assert channels["channels"][0]["required_any"] == ["region_europe_tag"]


def test_validation_failure_does_not_write(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)
    before = (routing_dir / "taxonomy.json").read_text(encoding="utf-8")

    result = routing_editor.main(base_args(config_path, routing_dir) + ["add-tag", "bad_child", "--parent", "missing_parent"])

    assert result == 3
    assert (routing_dir / "taxonomy.json").read_text(encoding="utf-8") == before


def test_find_and_lint_commands(tmp_path: Path, capsys) -> None:
    config_path, routing_dir = make_config(tmp_path)

    find_result = routing_editor.main(base_args(config_path, routing_dir) + ["find", "Europe"])
    lint_result = routing_editor.main(base_args(config_path, routing_dir) + ["lint"])

    output = capsys.readouterr().out
    assert find_result == 0
    assert "ENTRY region_europe" in output
    assert lint_result == 0


def test_migrate_typed_routing_moves_skip_only_entries_and_channel_fields(tmp_path: Path) -> None:
    config_path, routing_dir = make_config(tmp_path)
    knowledge = json.loads((routing_dir / "knowledge_base.json").read_text(encoding="utf-8"))
    knowledge["entries"].append(
        {
            "id": "false_positive_test",
            "aliases": ["sports"],
            "tags": ["skip_candidate"],
            "priority": 50,
            "score": 0,
        }
    )
    (routing_dir / "knowledge_base.json").write_text(json.dumps(knowledge, indent=2), encoding="utf-8")

    result = routing_editor.main(base_args(config_path, routing_dir) + ["migrate-typed-routing"])

    assert result == 0
    assert_valid(config_path, routing_dir)
    migrated_knowledge = json.loads((routing_dir / "knowledge_base.json").read_text(encoding="utf-8"))
    suppressions = json.loads((routing_dir / "suppressions.json").read_text(encoding="utf-8"))
    channels = json.loads((routing_dir / "channels.json").read_text(encoding="utf-8"))
    assert "false_positive_test" not in {entry["id"] for entry in migrated_knowledge["entries"]}
    assert "false_positive_test" in {entry["id"] for entry in suppressions["entries"]}
    assert channels["channels"][0]["required_tags"] == ["europe"]
    assert "required_any" not in channels["channels"][0]
