# Newsroom Routing

The router is a local-only classifier. It does not use paid APIs, external AI services, or full-text scraping. Feeds are inputs, Discord channels are destinations, and routing policy decides where an article posts.

## Config Shape

Recommended app config uses top-level `feeds` plus destination-only `channels`:

```json
{
  "feeds": [
    {
      "id": "reuters-world",
      "sourceId": "reuters",
      "sourceClass": "wire_service",
      "name": "Reuters World",
      "url": "https://example.com/rss",
      "pollIntervalSeconds": 300,
      "routePolicy": "normal",
      "legacyChannelKeys": ["middle-east"]
    }
  ],
  "channels": [
    {"key": "middle-east", "name": "Middle East", "discordChannelId": "111111111111111111"},
    {"key": "reuters", "name": "Reuters", "discordChannelId": "222222222222222222"},
    {"key": "review", "name": "Review", "discordChannelId": "1511541774642843789"}
  ]
}
```

Legacy channel-scoped `feeds` are still accepted. `legacyChannelKeys` preserves old observe-only posting behavior after migrating feeds to the top level; enforced routing uses the router's final destinations instead.

Top-level feeds can also declare `mirrorChannelKeys`. These are source-level archive/copy destinations that are appended after routing or review selection. They are deliberately not used for `no_match` items, so a mirror cannot make an otherwise unrouted item post.

## Routing Files

Weighted V2 is the primary router when `settings.routing.engine` is `weighted_v2`. Its config lives under `config/routing_v2/`:

- `routes.json` defines real Discord routes plus pseudo routes such as `noise`.
- `evidence.json` defines literal or regex text evidence. Every rule can add or subtract score for any route.
- `sources.json` defines source ID/class/name scoring and configured feed URL scoring.
- `mirrors.json` defines source-gated mirror destinations.

Legacy compatibility files are still present under `config/routing/`:

- `taxonomy.json` defines old tag inheritance.
- `knowledge_base.json` defines old concept aliases and emitted tags.
- `suppressions.json` defines old false-positive skip entries.
- `channels.json` defines old channel scoring.

Use those files only when deliberately running `settings.routing.engine: "legacy"` or `ROUTING_ENGINE=legacy`.

## Weighted Evidence Rules

`evidence.json` is the normal place to add or tune routing behavior. A rule can match literal phrases or regular expressions against article title, routing summary, URL slug, and source name fields. Each rule has route scores, and those scores can be positive or negative for any route:

```json
{
  "id": "strategic_nuclear_deterrence",
  "type": "literal",
  "terms": ["nuclear deterrence", "strategic deterrent"],
  "scores": {
    "strategic-weapons": 60,
    "air": 8,
    "review": -5
  },
  "notes": "Strategic weapons routing evidence."
}
```

Literal terms are compiled into whole-word, case-insensitive patterns with whitespace and hyphen flexibility. Regex rules are available for families of variants that are easier to express as a pattern. Terms and path terms match complete words or complete phrase tokens, so a short term such as `AR` does not match inside `are`.

When evidence overlaps, longer matches win. This is intentional: a phrase such as `sub sandwich` can score `noise` and block weaker evidence for `sub` or `sandwich` nearby. Use specific phrases first, then broader words only when they are still safe with negative scoring around them.

## Route Selection

`routes.json` defines each selectable channel route and thresholds:

- Normal destination routes map to configured Discord channel keys.
- `review` receives ambiguous or useful-to-human-review articles.
- `noise` is a pseudo route for items the bot should classify as not worth posting.
- `required_source_ids`, when present, are hard gates. The NatSec News route uses this to allow only the approved X account into that channel.

The highest eligible primary route wins. A second primary route is allowed when it is within the configured `secondary_within_percent` of the winner. Ties are resolved by configured priority and stable route order. If no primary route clears threshold but review does, the item goes to review with debug details.

Region routes are expected to win current-event conflict stories when conflict, diplomacy, war, or government crisis evidence is present. Domain routes such as Sea, Air, Land, Cyber, Space, Strategic Weapons, and Industrial Base should win when the story is primarily about that domain itself: capability changes, force structure, platform development, doctrine, procurement, accidents, or domain-specific adaptation.

## Source And Feed URL Scoring

`sources.json` scores source identity and configured feed/source URLs. Source rules can match:

- `source_ids`
- `source_classes`
- source name literals or patterns
- configured feed URL hosts
- configured feed URL path terms
- configured feed URL path regexes

Feed URL scoring matches the configured RSS/feed URL, not the article URL. Positive URL-only scores are bias-only by default (`url_bias_only: true`), so they can boost a route that already has content/source evidence but cannot route an unrelated article by URL alone. Negative URL scores always apply.

## Mirrors

`mirrors.json` defines copy/archive behavior after routing. Mirrors do not make an unrouted item post. They are appended only after an item has routed or gone to review, and they should be source-gated when possible.

## Discord Teaching

The preferred edit path is Discord:

- Right-click or long-press a bot article post, then choose **Apps -> Teach routing term**.
- For configured feed URL scoring, choose **Apps -> Teach feed URL** from the same article menu.
- Slash fallbacks are `/rss teach`, `/rss teach-feed-url`, `/rss preview-rule`, `/rss undo-rule`, `/rss rule-history`, and `/rss rule-help`.

Teaching validates route names and regexes before saving, updates `config/routing_v2/evidence.json` or `config/routing_v2/sources.json`, creates a latest rollback backup under `config/routing_v2/.discord_backups/`, records an event in SQLite, reloads routing in-process, and posts a short embed to `settings.routing.teachChangelogChannelId` when configured.

If a duplicate term or feed URL rule already exists, the bot shows the current JSON snippet and a merge preview. **Merge** updates submitted route scores while preserving other existing scores. **Replace** overwrites the criteria/scores/notes while keeping the rule ID. **Cancel** writes nothing.

## Decision Order

1. Build a `RoutingArticle` from title, summary/routing summary, article URL, source name, source ID, source class, and configured feed URL.
2. Match weighted evidence rules across configured text fields.
3. Apply longest-match precedence to overlapping evidence.
4. Apply source identity and feed URL scoring.
5. Enforce route source gates such as NatSec News source exclusivity.
6. Compare primary routes, review, and noise against thresholds.
7. Select the top primary route, optional high-scoring second primary route, review, or no post.
8. Append source mirrors only after a routed/review destination exists.
9. Apply duplicate and cluster limits.
10. Score article importance from the final routing decision and article source metadata, then persist the score and reasons with the decision.

Review posts always include routing explanation/debug information, even when normal debug embeds are off. Quick review buttons are intentionally deferred; future work should add persistent approve/suppress/skip/manual actions around the stored routing decision.

## Importance Scoring

Routing decisions include a local `0` to `10` importance score. It is separate from destination scoring: destination scoring decides where an article belongs, while importance scoring helps Discord readers spot higher-impact items after the destination is selected.

The score is based on high-impact concepts, emitted or expanded tags, source class, headline terms, and review status. Active-conflict, attack, missile, drone, disaster, cyber, strategic-weapons, nuclear, and key regional-crisis signals carry more weight than routine government or diplomacy tags. Low-signal no-match decisions are capped so routine items do not appear urgent.

The score and reason list are stored in `article_routing_decisions`. Posted embeds show new/update state and compact `Imp N` importance in the footer, with warmer colors for higher scores. The posted or updated time is attached to the embed timestamp field so Discord displays it in each viewer's local time.

## Discord Media Presentation

All source types should feed media into the same article metadata shape. RSS/Atom entries, email HTML, Bluesky posts, X/social link enrichment, and custom source fetchers can populate `image_url`, `video_url`, and `rich_metadata.media_items`.

When publishing, direct image media and direct playable video files are temporarily downloaded and uploaded to Discord as message attachments above the embed. Multiple suitable images can be uploaded together when the source exposes them. Direct playable video uploads are limited to file URLs such as `.mp4`, `.m4v`, `.mov`, and `.webm`; video page URLs are left to Discord's link preview behavior instead of being treated as downloadable files.

Embeds no longer set the primary image URL directly when attachment upload succeeds. This keeps RSS, email, Bluesky, X/social, and other enriched posts visually consistent with media above the text embed.

## Source Identity

Each feed should define:

- `sourceId`: stable machine identifier, such as `reuters`, `associated-press`, `defense-gov`, `breaking-defense`, or `csis`.
- `sourceClass`: broad class, such as `wire_service`, `official_us_defense`, `official_us_gov`, `official_foreign_defense`, `official_foreign_gov`, `defense_media`, `think_tank`, `major_media`, `individual_reporter`, `osint`, or `unknown`.
- `initialBackfillHours`: first-success posting window for RSS feeds when `postOldArticlesOnFirstRun` is false. New feeds should usually use `24`.
- `routingTags`: optional source-level tags added to every item from a tightly scoped feed. Use this only when the whole source has a stable topic, such as maritime, cyber, air, Indo-Pacific, or industrial-base coverage.
- `mirrorChannelKeys`: optional destination keys that receive a copy after an item is routed or sent for review.

If omitted, the loader derives `sourceId` from the feed id/name and derives only safe known source classes; otherwise it uses `unknown`.

Routing summaries may differ from Discord display summaries. The fetcher stores richer context in `rich_metadata.routing_summary` when a feed exposes full RSS content, email article bodies, or supported document/PDF text. Channel scoring should use this richer field, while embeds can stay short and readable.

Source onboarding should validate the endpoint before editing config: fetch with the production client shape, confirm a successful status, confirm parseable entries with recent timestamps, and check that the URL/source/name is not already represented. Reject 403s, certificate failures, empty feeds, stale-only feeds, or near-duplicates.

## Dedupe Policy

Duplicate suppression is source scoped:

- Same channel + same source + same normalized title is suppressed.
- Same channel + same source + same title signature is suppressed.
- Same channel + same story cluster + same source is suppressed.
- Same story cluster from different sources can post until 5 unique sources have posted to that channel.
- The 6th unique source for a story cluster is recorded as `cluster_cap`.

The first-pass `story_cluster_key` is based on local title signature data. It is deliberately not semantic clustering.

## Modes

Configured in `config/config.json`:

```json
"routing": {
  "enabled": true,
  "mode": "enforced",
  "engine": "weighted_v2",
  "weightedConfigDir": "config/routing_v2"
}
```

- `enforced`: post only to final router destinations. This is the normal production mode.
- `observe_only`: classify and store decisions, but use configured feed channel targets. Use only for deliberate testing.
- `route_preview`: keep normal polling unchanged while route commands preview decisions.

If routing config is invalid, enforcement is disabled and the bot falls back to existing behavior.

## Validation

```powershell
python -m app.main --validate-config
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --route-backtest 50
python -m app.routing_editor lint
```

Useful one-off checks:

```powershell
python -m app.main --route-test-title "Reuters: Iran sanctions expand after missile attack" --route-test-source "Reuters" --route-test-source-id reuters --route-test-source-class wire_service
python -m app.main --route-test-title "Carrier Global shares rise after earnings" --route-test-source "Reuters" --route-test-source-id reuters --route-test-source-class wire_service
python -m app.main --route-test-title "Patriot contract driven by Ukraine demand expands production" --route-test-source "Defense News" --route-test-source-id defense-news --route-test-source-class defense_media
```

## Discord Commands

All routing command responses are ephemeral:

- `/rss route-test`
- `/rss route-article`
- `/rss route-backtest`
- `/rss routing-status`
- `/rss explain`
- `/rss teach`
- `/rss teach-feed-url`
- `/rss preview-rule`
- `/rss undo-rule`
- `/rss rule-history`
- `/rss rule-help`

Use `/rss explain` with an article ID to inspect the latest persisted routing decision.

For daily routing maintenance, prefer the message context menus **Teach routing term** and **Teach feed URL** because they can infer the Discord message/article context directly.
