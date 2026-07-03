# Changelog

## 2026-07-02

- Promoted weighted V2 routing to the primary/default routing engine.
- Added weighted evidence routing in `config/routing_v2/evidence.json`, where each literal phrase or regex can add or subtract points for any route.
- Added whole-word phrase matching, regex-backed variants, and longest-overlap precedence so specific phrases such as `sub sandwich` can block weaker word matches.
- Added weighted source identity and configured feed URL scoring in `config/routing_v2/sources.json`, including bias-only positive URL scores and always-applied negative URL scores.
- Added weighted route definitions, pseudo routes, review/noise outcomes, secondary-route selection, source gates, and source mirror rules under `config/routing_v2/`.
- Added Discord routing teaching workflows: **Teach routing term**, **Teach feed URL**, `/rss teach`, `/rss teach-feed-url`, `/rss preview-rule`, `/rss undo-rule`, `/rss rule-history`, and `/rss rule-help`.
- Added duplicate-rule handling for Discord teaching with current-config snippets, merge previews, and **Merge**, **Replace**, or **Cancel** actions.
- Added route-name alias/autocomplete support so friendly route names resolve to canonical route keys.
- Added routing-teach changelog embeds to the configured Discord changelog channel.
- Added sports, weather, economy, feed URL, North America subdivision, NatSec source-gate, and review/noise routing coverage in the weighted config.
- Updated docs, setup instructions, GPT helper files, and legacy editor messaging so new routing work targets weighted V2 instead of the old taxonomy/rag-style config.
- Kept the legacy tag/concept router available behind `settings.routing.engine: "legacy"` for compatibility and validation.

Validation for this release should include:

```powershell
python -m app.main --validate-config --validate-env
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --route-backtest 50
python -m pytest -q
```

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
