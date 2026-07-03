# RSS Bot Weighted Routing Config Context

This file is intended to be uploaded into a ChatGPT Project/GPT alongside the latest routing config files. It gives the assistant enough context to draft safe weighted routing JSON edits without using Codex.

## Project Goal

The RSS bot reads RSS/feed, email, social, and custom sources and posts articles into Discord channels. Routing is controlled by JSON config plus Discord teaching commands. The assistant's job is to help the user improve weighted routing by drafting evidence rules, source/feed URL rules, route scores, and route-test examples.

The assistant should produce paste-ready snippets and explain exactly where they belong.

## Required Latest Files

Always prefer the latest versions from GitHub or a fresh manual upload:

- `config/routing_v2/routes.json`
- `config/routing_v2/evidence.json`
- `config/routing_v2/sources.json`
- `config/routing_v2/mirrors.json`
- `config/config.example.json`
- `docs/routing.md`
- `docs/routing-config-gpt-context.md`
- `docs/routing-config-gpt-prompt.md`

Useful optional files:

- `README.md`
- `app/routing_v2/config.py`
- `app/routing_v2/engine.py`
- `app/routing_v2/matcher.py`
- `app/routing_v2/teaching.py`
- `app/discord_bot.py`
- `ops/apply-routing-changes.ps1`

Legacy-only reference files:

- `config/routing/taxonomy.json`
- `config/routing/knowledge_base.json`
- `config/routing/suppressions.json`
- `config/routing/channels.json`
- `app/routing_editor.py`

Do not upload:

- `.env`
- private tokens
- local logs unless intentionally sharing examples
- SQLite database files
- `config/routing_v2/.discord_backups/`
- `config/routing/.editor_backups/`

## How Weighted Routing Works

Weighted V2 is the primary router. It scores each incoming article against every route. Rules in `config/routing_v2/evidence.json` add or subtract route points from matched text. Rules in `config/routing_v2/sources.json` add or subtract points from source IDs, source classes, source names, or configured feed URLs. The highest eligible primary route wins, with a second primary route allowed when it is within the configured score percentage.

`review` and `noise` are pseudo/review outcomes:

- `review` means the article is useful but ambiguous or needs human attention.
- `noise` means the item should generally not post to a topical channel.

Legacy routing files are retained for compatibility and historical reference, but new routing work should target `config/routing_v2/` unless the user explicitly asks for legacy router edits.

## Weighted V2 Files

### `routes.json`

Defines route keys, display names, aliases, thresholds, priorities, pseudo routes, and route-level gates.

Important fields:

- `key`: canonical route key used in scores.
- `aliases`: friendly names accepted by slash command score parsing.
- `channel_key`: destination channel key from `config/config.json` for real routes.
- `route_type`: `primary`, `review`, `noise`, or similar route class.
- `minimum_score`: score needed to select the route.
- `priority`: tie-breaker after score.
- `required_source_ids`: hard source gate. Use for source-exclusive channels.
- `pseudo`: true for non-channel scoring routes such as `noise`.

### `evidence.json`

Defines words, phrases, and regexes that score article text. Evidence can add or subtract points for any route.

Literal example:

```json
{
  "id": "naval_operations",
  "type": "literal",
  "terms": ["naval operations", "carrier strike group"],
  "scores": {
    "sea": 55,
    "land": -15
  },
  "notes": "Specific maritime/naval routing evidence."
}
```

Regex example:

```json
{
  "id": "world_cup_variants",
  "type": "pattern",
  "patterns": ["\\b(?:fifa\\s+)?world cup\\b"],
  "scores": {
    "sports": 55,
    "review": -10
  },
  "notes": "Sports routing for World Cup coverage."
}
```

Important details:

- Literal terms compile to whole-word, case-insensitive regex.
- Whitespace and hyphen differences should be tolerated.
- Regex is best for robust variants, plurals, abbreviations, and optional words.
- Longer overlapping matches win.
- Phrase rules can block weaker nearby word rules.
- Short terms must not match inside larger words.

### `sources.json`

Defines source identity scoring and configured feed URL scoring.

Example:

```json
{
  "id": "fifa_feed_sports_bias",
  "source_url_hosts": ["fifa.com"],
  "source_url_path_terms": ["world cup"],
  "scores": {
    "sports": 35,
    "review": -5
  },
  "url_bias_only": true,
  "notes": "Boost World Cup feed URL matches without routing by URL alone."
}
```

Feed URL scoring matches the configured feed/source URL, not the article URL. Positive URL-only scores should usually keep `url_bias_only: true`; they boost content/source evidence but cannot route an unrelated article by themselves. Negative URL scores always apply.

### `mirrors.json`

Defines archive/copy destinations after routing. Mirrors do not make a no-match item post. Use source gates on mirrors when possible.

## Destination Selection

The bot is intentionally narrow:

- It normally chooses one primary topical channel.
- It may add a second primary when scoring is within the configured percentage of the winner.
- It may add source mirrors after a route/review destination exists.
- It sends ambiguous useful items to review with debug details.
- It sends low-value off-topic items to noise/no post.

Decision order:

1. Build article routing context from title, summary, URL slug, source name, source ID, source class, and configured feed URL.
2. Match evidence rules.
3. Apply longest-match precedence.
4. Apply source identity and feed URL scoring.
5. Enforce route source gates.
6. Compare primary, review, and noise thresholds.
7. Select winner, optional high-scoring secondary, review, or no post.
8. Add mirrors after routing.
9. Persist routing/debug information and importance score.

## Region Versus Domain

Region routes should win current events about wars, diplomacy, sanctions, elections, civil unrest, government crisis, and conflict developments. It is more important for region channels not to miss important current events.

Domain routes should win when the news is specifically about a domain changing or operating: naval/maritime affairs, airpower, land forces, cyber, space, strategic weapons, procurement, doctrine, capability adaptation, accidents, force structure, or industrial base.

Good routing fixes usually combine:

- a positive score for the intended route
- a negative score for common wrong routes
- a `review` or `noise` score when the item should not go straight to a normal channel

Example reasoning:

- `submarine` can score Sea strongly.
- `sub sandwich` can score Noise and block weaker `sub`.
- `Navy` can score Sea and negatively score Land.
- `movie` or `film` can score Noise and negatively score routes where entertainment content has leaked.

## Score Scale

Use granular scores. A near-perfect article for a route can total around 100 points.

Suggested scale:

- `+50` to `+80`: strong direct evidence.
- `+25` to `+45`: solid evidence.
- `+5` to `+20`: weak/contextual evidence.
- `-10` to `-40`: wrong-route deterrent.
- `+35` to `+70` for `noise` or `review` when those outcomes are intentional.

Avoid making every broad word decisive. Use phrase specificity and negative scoring.

## Discord Teaching Workflow

Preferred UI:

- Right-click or long-press a bot article post.
- Choose **Apps -> Teach routing term** for text evidence.
- Choose **Apps -> Teach feed URL** for configured feed URL evidence.

Slash fallback commands:

```text
/rss teach message_id:<discord_message_id> term:"nuclear deterrence" scores:"strategic-weapons:+55, air:+6"
/rss teach-feed-url message_id:<discord_message_id> path_term:"sports" scores:"sports:+35"
/rss preview-rule article_id:<article_id> term:"sub sandwich" scores:"noise:+45, sea:-25"
/rss undo-rule
/rss rule-history
/rss rule-help
```

Teaching behavior:

- Validates route names and regexes.
- Accepts canonical route keys, friendly names, and configured aliases.
- Creates one latest rollback backup under `config/routing_v2/.discord_backups/`.
- Reloads routing in-process after a successful write.
- Records an event in SQLite.
- Posts a short embed to `settings.routing.teachChangelogChannelId` when configured.

Duplicate behavior:

- Duplicate term/feed URL detected: show current JSON snippet.
- Show a merge preview.
- User can choose **Merge**, **Replace**, or **Cancel**.
- Merge updates submitted route scores and preserves other scores.
- Replace overwrites criteria/scores/notes while keeping the rule ID.

## Practical Editing Recipes

### Add A Phrase Or Variant Family

Use `evidence.json`. Prefer literal terms for a small set of phrases and regex for many variants.

```json
{
  "id": "fed_rate_decision",
  "type": "pattern",
  "patterns": ["\\b(?:fed|federal reserve)\\s+(?:rate|interest rate)s?\\b"],
  "scores": {
    "economy": 50,
    "review": -5
  },
  "notes": "Economy routing for Federal Reserve rate coverage."
}
```

### Fix Wrong Channel Routing

Check whether:

1. The intended route has positive evidence.
2. The wrong route has a repeatable false-positive pattern.
3. A longer phrase should block a shorter word.
4. Source or feed URL scoring is biasing the wrong route.
5. The item should be review or noise instead of a normal route.

Good fixes usually add positive and negative route scores in the same rule.

### Add Feed URL Bias

Use `sources.json`, not `evidence.json`.

```json
{
  "id": "sports_feed_path_bias",
  "source_url_path_terms": ["sports"],
  "scores": {
    "sports": 35
  },
  "url_bias_only": true,
  "notes": "Configured feed URL sports path bias."
}
```

### Add A New Channel/Route

1. Add the channel to `config/config.json`.
2. Add the route to `config/routing_v2/routes.json`.
3. Add evidence in `evidence.json`.
4. Add source/feed URL bias in `sources.json` if useful.
5. Add mirror behavior in `mirrors.json` only if needed.
6. Validate and route-test.

## Preferred Assistant Output Format

For an edit request:

- Start with a one- or two-sentence recommendation.
- Name the exact file and existing rule to update.
- Provide the JSON snippet in a fenced `json` block.
- Provide route-test and validation commands in a fenced `powershell` block.
- Mention whether the Discord teaching UI can apply it directly.

Keep snippets small unless a full replacement object is safer.

## Validation And Deployment

After applying edits:

```powershell
python -m app.main --validate-routing
python -m app.main --routing-diagnostics
python -m app.main --route-test-title "Example headline"
python -m app.main --route-backtest 25
```

Legacy-only lint:

```powershell
python -m app.routing_editor lint
python -m app.main --validate-routing --routing-engine legacy
```

Deploy config-only changes:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\apply-routing-changes.ps1
```

Emergency bot-up command:

```powershell
docker compose up -d --force-recreate rssbot
docker compose ps
docker compose logs --since 5m rssbot
```
