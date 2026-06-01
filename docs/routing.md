# Structured Routing

The routing system is a first-pass, local-only classifier for RSS articles. It does not use paid APIs, external AI services, or full-text scraping. It matches transparent dictionary terms, emits structured tags, expands those tags through a taxonomy, scores configured channel rules, and records the decision.

The existing `config/config.json` channel and feed setup remains the source of truth for Discord channel IDs. Routing rules refer to channel keys only.

## Files

- `config/routing/taxonomy.json` defines allowed tags and parent tag relationships.
- `config/routing/knowledge_base.json` defines terms, aliases, and the tags they emit.
- `config/routing/channels.json` defines channel scoring rules by `channel_key`.

## Terms And Tags

A term is text found in an article, such as `Chinese carrier`.

A tag is structured meaning emitted by the matched term, such as `china`, `aircraft_carrier`, `naval`, and `indo_pacific`.

Terms and tags are deliberately separate. Tune language in `knowledge_base.json`; tune routing behavior in `channels.json`.

## Taxonomy

Each tag can define `parent_tags`. For example:

```json
"carrier_strike_group": {"parent_tags": ["aircraft_carrier", "naval", "military"]}
```

If a term emits `carrier_strike_group`, the final expanded tag set also includes `aircraft_carrier`, `naval`, and `military`.

## Knowledge Base

Each knowledge entry has:

- `id`: stable identifier used by term boosts/penalties.
- `aliases`: phrases to match case-insensitively.
- `tags`: emitted structured tags.
- `priority`: tie-breaker for overlapping matches.
- `score`: reserved for entry strength and explanations.

The matcher uses longest-match overlap blocking. `Chinese aircraft carrier` wins over the shorter overlapping `Chinese aircraft`.

## Channel Scoring

Each channel rule has:

- `channel_key`: must exist in `config/config.json`.
- `enabled`
- `minimum_score`
- `priority`
- `term_boosts`
- `tag_boosts`
- `term_penalties`
- `tag_penalties`
- `required_any`
- `source_biases`
- `content_mode_adjustments`

Common rule fields can live once in top-level `rule_defaults` inside `channels.json`. A channel only needs to override values that differ from those defaults.

`title_only` articles are scored more conservatively by default. `title_and_stub` is used when a summary/stub is available.

`skip_candidate` and `review_required` tags override normal routing selections. This prevents low-value roundups and review-marked items from auto-posting in enforced mode.

## Modes

Configured in `config/config.json`:

```json
"routing": {
  "enabled": false,
  "mode": "observe_only",
  "configDir": "config/routing"
}
```

Modes:

- `observe_only`: classify and store/log decisions, but keep the existing feed/channel posting behavior.
- `route_preview`: reserved for manual command previews; normal polling remains unchanged.
- `enforced`: use selected routing channels instead of blindly posting to every feed-attached channel.

If routing config is invalid in observe mode, the bot logs the error and continues existing behavior. If enforced mode cannot load valid routing config, enforcement is refused and the bot falls back to existing behavior.

## CLI

Validate routing:

```powershell
python -m app.main --validate-routing
```

Test one title:

```powershell
python -m app.main --route-test-title "Chinese carrier Liaoning enters Philippine Sea"
```

Test title plus summary/source/url:

```powershell
python -m app.main --route-test-title "Ghana parliament passes anti-LGBTQ+ bill" --route-test-source "BBC Africa"
```

Backtest recent database articles:

```powershell
python -m app.main --route-backtest 25
```

Bootstrap missing routing files:

```powershell
python -m app.main --bootstrap-routing-config
```

Existing files are not overwritten unless you pass:

```powershell
python -m app.main --bootstrap-routing-config --force-bootstrap-routing-config
```

## Discord Commands

All responses are ephemeral:

- `/rss route-test`
- `/rss route-article`
- `/rss route-backtest`
- `/rss routing-status`

Use `/rss route-test` before enabling `enforced`.

## Example

Title:

```text
Chinese carrier Liaoning enters Philippine Sea
```

Knowledge matches:

- `Chinese carrier`
- `Liaoning`
- `Philippine Sea`

Tags:

- `china`
- `aircraft_carrier`
- `naval`
- `indo_pacific`
- `philippines`

Expanded:

- `military`
- `world`

Routing:

- `sea` scores highly from `naval` and `aircraft_carrier`.
- `indo-pacific` scores highly from `indo_pacific`, `china`, and `philippines`.

## Tuning

Add a new term:

1. Add an entry to `knowledge_base.json`.
2. Use aliases that are specific enough to avoid noise.
3. Run `python -m app.main --validate-routing`.
4. Run one or more `--route-test-title` checks.

Add a new tag:

1. Add it to `taxonomy.json`.
2. Add parent tags if useful.
3. Use it in knowledge entries or channel rules.
4. Run `python -m app.main --validate-routing`.

Tune a channel:

1. Open `channels.json`.
2. Adjust `tag_boosts`, `term_boosts`, penalties, or `minimum_score`.
3. Run `python -m app.main --route-backtest 25`.
4. Check `logs/rssbot-audit.log` when audit logging is enabled.

## First-Pass Limits

- Matching is phrase/alias based.
- Existing DB rows do not store full article bodies.
- Backtests mostly use title, source name, normalized title, and URL path.
- The starter channel rules are intentionally conservative and need live tuning.
