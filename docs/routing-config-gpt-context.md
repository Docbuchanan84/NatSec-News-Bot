# RSS Bot Routing Config Context

This file is intended to be uploaded into a ChatGPT Project/GPT alongside the latest routing config files. It gives the assistant enough context to help draft safe JSON edits without using Codex.

## Project Goal

The RSS bot reads many RSS/feed sources and posts articles into Discord channels. Routing is controlled by JSON config. The assistant's job is to help the user quickly improve routing by drafting tags, knowledge entries, aliases, boosts, penalties, and route-test examples.

The assistant should produce paste-ready snippets and explain exactly where they belong.

## Required Latest Files

Always prefer the latest versions from GitHub or a fresh manual upload:

- `config/routing/taxonomy.json`
- `config/routing/knowledge_base.json`
- `config/routing/suppressions.json`
- `config/routing/channels.json`
- `config/config.example.json`
- `docs/routing.md`
- `docs/routing-config-gpt-context.md`

Useful optional files:

- `app/routing_editor.py`
- `app/routing/engine.py`
- `app/routing/scorer.py`
- `app/routing/matcher.py`
- `app/routing/config.py`
- `app/routing/models.py`
- `ops/apply-routing-changes.ps1`
- `README.md`

Do not upload:

- `.env`
- private tokens
- local logs unless intentionally sharing examples
- SQLite database files
- `config/routing/.editor_backups/`

## How Routing Works

Routing has three main layers.

### 1. Taxonomy Tags

`taxonomy.json` defines every valid tag. Tags can have `parent_tags`, so a specific tag can inherit broader categories.

Example:

```json
"switzerland": {
  "parent_tags": ["europe"],
  "description": "Swiss national politics, security, economy, society, and public affairs."
}
```

If a knowledge entry emits `switzerland`, the router also expands to `europe` and then to any broader parents.

### 2. Knowledge Concepts

`knowledge_base.json` defines concept IDs and aliases. Aliases are matched against article title/summary text. When an alias matches, its knowledge entry emits tags.

Example:

```json
{
  "id": "switzerland",
  "aliases": [
    "Switzerland",
    "Swiss",
    "Swiss voters",
    "Swiss referendum"
  ],
  "tags": [
    "switzerland",
    "europe"
  ],
  "priority": 60,
  "score": 4,
  "description": "Swiss country and public affairs routing."
}
```

Aliases belong here, not in channel rules.

### 3. Suppressions

`suppressions.json` defines known false-positive text that should skip routing unless protected tags are present.

```json
{
  "id": "false_positive_sports",
  "aliases": ["Premier League", "World Cup", "soccer"],
  "action": "skip",
  "unless_tags_any": ["military", "government", "disaster"],
  "priority": 50
}
```

Use suppressions for sports, entertainment, travel, commercial airline/shipping, market roundup, and similar noise.

Important details:

- Aliases match case-insensitively.
- Whitespace and hyphen differences are handled.
- Longer overlapping alias matches win over shorter ones.
- `priority` helps break overlap ties.
- `score` is match metadata. It is useful context, but channel destination score is mainly controlled in `channels.json`.
- The `id` should be stable. Rename IDs only with care because channel `concept_boosts`, `concept_penalties`, `required_concepts`, or `excluded_concepts` may reference them.

### 3. Channel Scores

`channels.json` defines which Discord channel should receive a story.

Core fields:

- `channel_key`: channel identifier from bot config.
- `destination_class`: `primary`, `mirror`, or `review`.
- `minimum_score`: score needed to select the channel.
- `priority`: tie-breaker after score.
- `tag_boosts`: add score when emitted/expanded tags match.
- `tag_penalties`: subtract score when tags match.
- `concept_boosts`: add score when a knowledge ID matches.
- `concept_penalties`: subtract score when a knowledge ID matches.
- `required_tags` / `excluded_tags`: gate by taxonomy tags.
- `required_concepts` / `excluded_concepts`: gate by knowledge IDs.
- `required_any`, `excluded_any`, `term_boosts`, and `term_penalties`: legacy compatibility only.

The channel score is not simply the sum of `knowledge_base.score`. To change where things route, edit channel boosts, penalties, and gates.

## Destination Selection

The bot is intentionally narrow:

- It normally chooses one primary topical channel.
- It may also include one mirror/archive channel when relevant.
- If no mirror/archive channel is relevant, a second primary channel can be selected only when scoring is strong enough.
- Review tags and suppressions can override normal routing.

Behavior tags:

- `review_required` and `ambiguous` send posts to review.
- `skip_candidate` still skips normal posting for compatibility, but false positives should live in `suppressions.json`.

## Practical Editing Recipes

### Add Aliases To Existing Topic

Use when the tag/ID already exists but the bot misses common headline wording.

Edit `knowledge_base.json`:

```json
"aliases": [
  "Existing alias",
  "New specific alias",
  "Another headline phrase"
]
```

Then test likely headlines.

### Add New Country Or Region Topic

1. Add a specific tag in `taxonomy.json` if missing.
2. Add a knowledge entry in `knowledge_base.json`.
3. Add channel score changes in `channels.json` if the regional channel does not already score the tag.

Example channel scoring:

```json
"tag_boosts": {
  "switzerland": 5
},
"required_tags": [
  "europe",
  "switzerland"
]
```

### Fix Wrong Channel Routing

When something routes to the wrong channel, consider all three:

1. Is the correct concept being matched?
2. Is the correct tag emitted?
3. Does the intended channel score that tag more strongly than the wrong channel?

Good fixes usually combine:

- More specific aliases.
- Correct channel boost.
- Wrong-channel penalty.
- A tighter `required_tags` or `required_concepts` gate.
- A suppression entry when the item is pure noise.

### Use Negative Scoring

Negative scoring is valuable for recurring false positives.

Example: if civilian aviation stories are leaking into military air:

```json
"tag_penalties": {
  "civilian_aviation": 6,
  "consumer_travel": 5
}
```

Use penalties to push a wrong channel down without weakening the right channel.

### Ripple Rename

If renaming a tag, update:

- `taxonomy.json` tag key.
- every `parent_tags` reference.
- every knowledge entry `tags` list.
- `channels.json` tag boosts/penalties.
- `required_tags`, `excluded_tags`, and `suppress_when_tags_any`.
- suppression `unless_tags_any`.
- `review_tags` and `skip_tags` if relevant.

If renaming a knowledge ID, update:

- `knowledge_base.json` `id`.
- `channels.json` `concept_boosts`.
- `channels.json` `concept_penalties`.
- `required_concepts` and `excluded_concepts` if they reference the ID.

## Preferred Assistant Output Format

For an edit request, answer like this:

- Start with a one- or two-sentence recommendation.
- Name the exact file and existing entry/rule to update.
- Provide the JSON snippet in a fenced `json` block.
- Provide route-test and validation commands in a fenced `powershell` block.
- Mention whether a channel score or penalty should also be checked.

Keep snippets small unless a full replacement object is safer.

## Local Editor Available To User

The repo has a local editor:

```powershell
python -m app.routing_editor wizard
python -m app.routing_editor find switzerland
python -m app.routing_editor show-entry switzerland
python -m app.routing_editor show-channel europe
python -m app.routing_editor lint
```

Double-click launchers:

- `config/Open Routing Editor.cmd`
- `config/Test and Redeploy Routing Changes.cmd`

The editor validates before saving and creates backups.

## Validation And Deployment

After applying edits:

```powershell
python -m app.main --validate-routing
python -m app.routing_editor lint
python -m app.routing_editor route-test "Example headline"
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
