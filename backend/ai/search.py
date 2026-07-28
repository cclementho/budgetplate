"""Fuzzy item search + substitute suggestions — pure Python, no AI.

Given a free-text query ("chicken breast") and the catalogue of cleaned items
for a postal code, rank direct matches by token overlap + fuzzy similarity and
propose same-category items as substitutes. Instant, deterministic, and free —
Claude is reserved for /api/budget-plan only.

The public interface matches the old Claude-backed version:
``search_items(query, items) -> {"matches": [{id, similarity}],
                                 "similar": [{id, reason}]}``
"""

import re
from difflib import SequenceMatcher

MAX_MATCHES = 10
MAX_SIMILAR = 6
MATCH_THRESHOLD = 0.4

_WORD_RE = re.compile(r"[a-z]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _score(query_tokens: list[str], name: str) -> float:
    """Score 0..1: blend of query-token coverage and fuzzy string similarity."""
    name_lower = (name or "").lower()
    name_tokens = set(_tokens(name_lower))
    if not query_tokens or not name_tokens:
        return 0.0

    # Token coverage: how much of the query appears in the name (prefix-tolerant
    # so "tomatoes" matches "tomato").
    hits = 0
    for qt in query_tokens:
        if qt in name_tokens or any(
            nt.startswith(qt) or qt.startswith(nt) for nt in name_tokens
        ):
            hits += 1
    coverage = hits / len(query_tokens)

    # Fuzzy whole-string similarity as a tiebreaker.
    fuzz = SequenceMatcher(None, " ".join(query_tokens), name_lower).ratio()

    return round(0.75 * coverage + 0.25 * fuzz, 3)


def search_items(query: str, items: list[dict]) -> dict:
    """Return {"matches": [...], "similar": [...]} of catalogue ids.

    ``items`` are catalogue rows with at least ``id``, ``clean_name`` and
    ``category``. Matches are direct hits ranked by score; similar items are
    same-category products that could substitute (e.g. chicken thighs for a
    chicken-breast search).
    """
    query_tokens = _tokens(query)
    if not query_tokens or not items:
        return {"matches": [], "similar": []}

    scored = [
        (it, _score(query_tokens, it.get("clean_name", ""))) for it in items
    ]

    matches = sorted(
        (pair for pair in scored if pair[1] >= MATCH_THRESHOLD),
        key=lambda p: p[1],
        reverse=True,
    )[:MAX_MATCHES]
    match_ids = {it["id"] for it, _ in matches}

    # Substitutes: same category as the top matches, sharing at least one query
    # token OR moderately similar — but not already a direct match.
    match_categories = {it.get("category") for it, _ in matches}
    similar: list[dict] = []
    if matches:
        candidates = sorted(
            (
                (it, score)
                for it, score in scored
                if it["id"] not in match_ids
                and it.get("category") in match_categories
                and score >= 0.15
            ),
            key=lambda p: p[1],
            reverse=True,
        )
        for it, _ in candidates[:MAX_SIMILAR]:
            similar.append(
                {
                    "id": it["id"],
                    "reason": f"Similar option in {it.get('category', 'the same aisle')}",
                }
            )

    return {
        "matches": [
            {"id": it["id"], "similarity": score} for it, score in matches
        ],
        "similar": similar,
    }
