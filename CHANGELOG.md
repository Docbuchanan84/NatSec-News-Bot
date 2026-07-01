# Changelog

## 2026-07-01

- Unified media handling across RSS/Atom, email, Bluesky, X/social link enrichment, and custom source metadata.
- Added direct Discord uploads for suitable images and playable video files so media appears above embeds instead of as bare URLs.
- Preserved multi-image payloads when sources expose them, including Bluesky image sets and RSS media collections.
- Added video persistence fields to stored articles and backfilled missing media metadata on duplicate/article updates.
- Updated embed footers to remove literal Discord timestamp markup, rely on native viewer-local embed timestamps, and shorten importance display to `Imp N`.
- Added regression coverage for media extraction, Discord attachment uploads, video persistence, social-link video selection, multi-image posts, and compact footer formatting.

Validation for this release should include:

```powershell
python -m app.main --validate-config --validate-env
python -m app.main --validate-routing
python -m pytest -q
```

## 2026-06-27

- Added support for RSS feed-level `initialBackfillHours` so first-run backfill windows are enforced for normal feeds, not only email sources.
- Added support for RSS feed-level `routingTags` so tightly scoped feeds can carry safe source-level routing hints into the routing engine.
- Expanded the live NSN source set by 15 validated RSS/Atom feeds across allied government, defense media, think tank/legal analysis, maritime, Indo-Pacific, cyber, and industrial-base coverage.
- Improved routing knowledge for law-of-armed-conflict, irregular warfare, Indo-Pacific security, maritime security, Eurasia influence/security, cyber defense, C4ISR battle networks, and European defense industry coverage.
- Cleaned up short or duplicated routing aliases that were likely to over-match, including broad acronyms and duplicate country/place aliases.
- Added regression tests for new-source metadata, feed backfill behavior, feed routing tags, and representative routing outcomes.

Validation for this release should include:

```powershell
python -m app.main --validate-config --validate-env
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.routing_editor lint
python -m app.main --route-backtest 50
python -m pytest -q
```
