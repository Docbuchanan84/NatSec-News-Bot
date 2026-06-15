# Typed Routing Design Note

The routing config is now typed around one principle:

Text matches concepts. Concepts emit tags/signals. Suppressions remove known noise. Policies resolve conflicts. Channel profiles score destinations. Sources add mirrors.

## Typed References

Channel rules should use explicit fields:

- `required_tags`, `excluded_tags`
- `required_concepts`, `excluded_concepts`
- `tag_boosts`, `tag_penalties`
- `concept_boosts`, `concept_penalties`
- `suppress_when_tags_any`

Legacy `required_any`, `excluded_any`, `term_boosts`, and `term_penalties` still load, but they are compatibility fields. They can mix tags, concept IDs, and aliases, which makes routing harder to audit.

## Suppressions

Skip-only false-positive entries moved out of `knowledge_base.json` into `suppressions.json`. Suppressions are not concepts; they are operational noise filters. They run after concept/tag matching so `unless_tags_any` can protect real military, government, or disaster stories.

## Migration Rule

Use `python -m app.routing_editor migrate-typed-routing` for future cleanup. It previews changes, writes backups, validates the candidate config, and preserves legacy references only when they are ambiguous.
