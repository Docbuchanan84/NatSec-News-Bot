from __future__ import annotations

from app.routing.models import TaxonomyTag


def expand_tags(tags: set[str], taxonomy: dict[str, TaxonomyTag]) -> set[str]:
    expanded = set(tags)
    stack = list(tags)
    while stack:
        tag = stack.pop()
        item = taxonomy.get(tag)
        if item is None:
            continue
        for parent in item.parent_tags:
            if parent not in expanded:
                expanded.add(parent)
                stack.append(parent)
    return expanded


def find_taxonomy_cycles(taxonomy: dict[str, TaxonomyTag]) -> list[str]:
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(tag: str, path: list[str]) -> None:
        if tag in visited:
            return
        if tag in visiting:
            cycle = " -> ".join(path + [tag])
            errors.append(f"taxonomy cycle detected: {cycle}")
            return
        visiting.add(tag)
        for parent in taxonomy[tag].parent_tags:
            if parent in taxonomy:
                visit(parent, path + [tag])
        visiting.remove(tag)
        visited.add(tag)

    for tag in taxonomy:
        visit(tag, [])
    return errors
