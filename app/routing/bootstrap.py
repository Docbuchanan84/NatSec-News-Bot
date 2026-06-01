from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.database import Database
from app.models import AppConfig


def bootstrap_routing_config(
    app_config: AppConfig,
    db: Database,
    config_dir: str | Path,
    force: bool = False,
    days: int = 7,
) -> str:
    root = Path(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    skipped: list[str] = []

    targets = {
        "taxonomy.json": _starter_taxonomy(),
        "knowledge_base.json": _starter_knowledge_base(db, days),
        "channels.json": _starter_channels(app_config),
    }
    for filename, payload in targets.items():
        path = root / filename
        if path.exists() and not force:
            skipped.append(filename)
            continue
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        created.append(filename)

    report = [
        f"routing bootstrap directory: {root}",
        "created/updated: " + (", ".join(created) or "none"),
        "skipped existing: " + (", ".join(skipped) or "none"),
    ]
    return "\n".join(report)


def recent_seed_report(db: Database, limit: int = 200, days: int = 7) -> str:
    rows = db.recent_articles_for_routing(limit=limit, days=days)
    hints = Counter()
    watched = {
        "china": ("china", "chinese", "beijing"),
        "taiwan": ("taiwan",),
        "russia": ("russia", "russian", "moscow"),
        "ukraine": ("ukraine", "ukrainian", "kyiv"),
        "iran": ("iran", "iranian", "tehran"),
        "israel": ("israel", "israeli"),
        "yemen": ("yemen", "houthi"),
        "ghana": ("ghana", "ghanaian"),
        "naval": ("naval", "navy", "carrier", "warship"),
        "air_defense": ("air defense", "patriot", "thaad"),
        "sanctions": ("sanctions", "ofac"),
    }
    for row in rows:
        text = " ".join(str(row[key] or "") for key in ("title", "source_name", "url")).casefold()
        for hint, needles in watched.items():
            if any(needle in text for needle in needles):
                hints[hint] += 1
    if not hints:
        return f"No conservative seed hints found in {len(rows)} recent articles."
    return "Conservative seed hints: " + ", ".join(f"{key}={count}" for key, count in hints.most_common())


def _starter_taxonomy() -> dict:
    return {
        "version": 1,
        "tags": {
            "world": {"parent_tags": []},
            "review_required": {"parent_tags": []},
            "skip_candidate": {"parent_tags": []},
            "ambiguous": {"parent_tags": ["review_required"]},
        },
    }


def _starter_knowledge_base(db: Database, days: int) -> dict:
    # The checked-in starter files are richer. Bootstrap stays conservative to avoid junk terms.
    report = recent_seed_report(db, days=days)
    return {
        "version": 1,
        "bootstrap_report": report,
        "entries": [],
    }


def _starter_channels(app_config: AppConfig) -> dict:
    return {
        "version": 1,
        "max_destinations": 3,
        "review_tags": ["review_required", "ambiguous"],
        "skip_tags": ["skip_candidate"],
        "rule_defaults": {
            "enabled": True,
            "minimum_score": 4,
            "priority": 0,
            "tag_penalties": {"skip_candidate": 8},
            "content_mode_adjustments": {"title_only": -1},
        },
        "channels": [
            {
                "channel_key": channel.key,
                "tag_boosts": {},
                "notes": "Neutral bootstrap placeholder; tune manually.",
            }
            for channel in app_config.channels
        ],
    }
