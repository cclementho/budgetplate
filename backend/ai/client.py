"""Shared Anthropic client + JSON helpers for Claude Haiku calls.

Centralising the client means the API key is read once and never leaves the
backend. All callers use ``call_claude_json`` so JSON parsing/error handling is
consistent.
"""

import json
import re

import anthropic

from config import settings

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """Return a lazily-initialised Anthropic client."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _extract_json(text: str):
    """Best-effort extraction of a JSON value from a model response.

    Haiku is reliable but can wrap JSON in prose or code fences. We try a strict
    parse first, then fall back to the first balanced ``{...}`` / ``[...]`` span.
    """
    text = text.strip()
    # Strip markdown code fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost array or object.
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("Could not parse JSON from model response")


def call_claude_json(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
):
    """Call Claude Haiku and return parsed JSON from the response text."""
    client = get_client()
    kwargs = {
        "model": settings.CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text = "".join(block.text for block in response.content if block.type == "text")
    return _extract_json(text)
