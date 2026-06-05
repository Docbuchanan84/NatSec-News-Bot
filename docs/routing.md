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

## Routing Files

- `config/routing/taxonomy.json` defines allowed tags and parent tag expansion.
- `config/routing/knowledge_base.json` defines phrase aliases and emitted tags.
- `config/routing/channels.json` defines destination rules and source gates.

Channel rules support:

- `destination_class`: `primary`, `mirror`, or `review`; default is `primary`.
- `required_source_ids`, `excluded_source_ids`
- `required_source_classes`, `excluded_source_classes`
- legacy `required_source_any`, `excluded_source_any`, and `source_biases`

Primary destinations are scored normally and limited by `max_primary_destinations`. Mirror destinations are source-gated archives added after primary selection. Review destinations are only used for review-required or ambiguous items.

## Decision Order

1. Match knowledge entries.
2. Emit tags and expand taxonomy parents.
3. If skip tags are present, record `skipped` and post nowhere, including mirrors.
4. If review tags are present, record `review` and post only to `review`.
5. If there are no matches, record `no_match` and post nowhere.
6. Otherwise score primary destinations, add source mirrors, apply duplicate and cluster limits, record `routed` when final destinations exist.

Review posts always include routing explanation/debug information, even when normal debug embeds are off. Quick review buttons are intentionally deferred; future work should add persistent approve/suppress/skip/manual actions around the stored routing decision.

## Source Identity

Each feed should define:

- `sourceId`: stable machine identifier, such as `reuters`, `associated-press`, `defense-gov`, `breaking-defense`, or `csis`.
- `sourceClass`: broad class, such as `wire_service`, `official_us_defense`, `official_us_gov`, `official_foreign_defense`, `official_foreign_gov`, `defense_media`, `think_tank`, `major_media`, `individual_reporter`, `osint`, or `unknown`.

If omitted, the loader derives `sourceId` from the feed id/name and derives only safe known source classes; otherwise it uses `unknown`.

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
  "mode": "observe_only",
  "configDir": "config/routing"
}
```

- `observe_only`: classify and store decisions, but use legacy feed channel targets.
- `route_preview`: keep normal polling unchanged while route commands preview decisions.
- `enforced`: post only to final router destinations.

If routing config is invalid, enforcement is disabled and the bot falls back to existing behavior.

## Validation

```powershell
python -m app.main --validate-config
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --route-backtest 50
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

Use `/rss explain` with an article ID to inspect the latest persisted routing decision.
