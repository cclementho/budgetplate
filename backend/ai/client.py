"""Shared Anthropic client + JSON helpers for Claude Haiku calls.

Centralising the client means the API key is read once and never leaves the
backend. All callers use ``call_claude_json`` so JSON parsing/error handling is
consistent.
"""

import json
import logging
import re

import anthropic

from config import settings

logger = logging.getLogger("budgetplate.ai")

_client: anthropic.Anthropic | None = None


class ClaudeResponseError(Exception):
    """Claude replied, but the reply can't be used.

    ``reason`` is one of ``max_tokens`` (cut off), ``refusal``, ``empty`` (no
    text) or ``invalid_json``. Details are logged here; callers turn this into
    a friendly message instead of a 500.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    model: str | None = None,
):
    """Call Claude Haiku and return parsed JSON from the response text.

    ``model`` overrides ``settings.CLAUDE_MODEL`` for one call (used by the
    model-comparison script); production callers leave it unset.
    """
    client = get_client()
    kwargs = {
        "model": model or settings.CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    # Read text blocks only: newer models (e.g. Haiku 5.5) put thinking blocks first.
    text = "".join(block.text for block in response.content if block.type == "text")

    usage = getattr(response, "usage", None)
    context = (
        f"model={kwargs['model']} stop_reason={response.stop_reason} max_tokens={max_tokens} "
        f"input_tokens={getattr(usage, 'input_tokens', None)} "
        f"output_tokens={getattr(usage, 'output_tokens', None)} text_chars={len(text)}"
    )
    if response.stop_reason == "max_tokens":
        logger.error("Claude response was cut off at max_tokens (thinking counts toward it): %s", context)
        raise ClaudeResponseError("max_tokens")
    if response.stop_reason == "refusal":
        logger.error("Claude declined the request: %s", context)
        raise ClaudeResponseError("refusal")
    if not text.strip():
        logger.error("Claude returned no text: %s", context)
        raise ClaudeResponseError("empty")
    try:
        return _extract_json(text)
    except ValueError:  # includes json.JSONDecodeError
        logger.error("Claude returned text that is not valid JSON: %s", context)
        raise ClaudeResponseError("invalid_json") from None
