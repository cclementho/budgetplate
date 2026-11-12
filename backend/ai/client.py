"""Shared Anthropic client + JSON helpers for Claude Haiku calls.

Centralising the client means the API key is read once and never leaves the
backend. All callers use ``call_claude_json`` so JSON parsing/error handling is
consistent.
"""

import json
import logging
import re
import threading
import time

import anthropic

from config import settings

logger = logging.getLogger("budgetplate.ai")

_client: anthropic.Anthropic | None = None

# Stats of the most recent Claude call made on this thread (model, tokens,
# milliseconds), so a route can put them in its own timing log line.
_last_call = threading.local()


def last_call_stats() -> dict:
    """Stats for the latest ``call_claude_json`` on this thread ({} if none)."""
    return dict(getattr(_last_call, "stats", {}))


class ClaudeResponseError(Exception):
    """Claude replied, but the reply can't be used.

    ``reason`` is one of ``max_tokens`` (cut off), ``refusal``, ``empty`` (no
    text) or ``invalid_json``. Details are logged here; callers turn this into
    a friendly message instead of a 500.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.stats: dict = {}  # model, tokens and timing of the failed call


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


def _request(prompt: str, system: str | None, max_tokens: int, model: str | None) -> dict:
    kwargs = {
        "model": model or settings.CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    return kwargs


def _parse_reply(response, text: str, kwargs: dict, elapsed_ms: int) -> tuple[object, dict]:
    """Check a finished reply and return ``(parsed JSON, stats)``.

    Raises ``ClaudeResponseError`` (with ``.stats`` attached) when the reply
    was cut off, refused, empty or not JSON.
    """
    usage = getattr(response, "usage", None)
    stats = {
        "model": kwargs["model"],
        "stop_reason": response.stop_reason,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "claude_ms": elapsed_ms,
    }
    context = (
        f"model={kwargs['model']} stop_reason={response.stop_reason} "
        f"max_tokens={kwargs['max_tokens']} input_tokens={stats['input_tokens']} "
        f"output_tokens={stats['output_tokens']} text_chars={len(text)} claude_ms={elapsed_ms}"
    )

    def fail(reason: str, message: str):
        logger.error("%s: %s", message, context)
        error = ClaudeResponseError(reason)
        error.stats = stats
        return error

    if response.stop_reason == "max_tokens":
        raise fail("max_tokens", "Claude response was cut off at max_tokens (thinking counts toward it)")
    if response.stop_reason == "refusal":
        raise fail("refusal", "Claude declined the request")
    if not text.strip():
        raise fail("empty", "Claude returned no text")
    try:
        parsed = _extract_json(text)
    except ValueError:  # includes json.JSONDecodeError
        raise fail("invalid_json", "Claude returned text that is not valid JSON") from None
    logger.info("Claude call ok: %s", context)
    return parsed, stats


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
    kwargs = _request(prompt, system, max_tokens, model)

    _last_call.stats = {}
    started = time.perf_counter()
    response = client.messages.create(**kwargs)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    # Read text blocks only: newer models (e.g. Haiku 5.5) put thinking blocks first.
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        parsed, stats = _parse_reply(response, text, kwargs, elapsed_ms)
    except ClaudeResponseError as exc:
        _last_call.stats = exc.stats
        raise
    _last_call.stats = stats
    return parsed


def stream_claude_json(
    prompt: str,
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    model: str | None = None,
):
    """Stream a Claude reply: yields ``("text", chunk)`` as it is written, then
    ``("done", (parsed JSON, stats))`` once the whole reply has been checked.

    The same checks as ``call_claude_json`` run at the end and raise
    ``ClaudeResponseError`` (stats on ``.stats``). ``stats`` also has
    ``first_text_ms``, the wait before the first text arrived. Closing the
    generator early ends the API request.
    """
    client = get_client()
    kwargs = _request(prompt, system, max_tokens, model)
    started = time.perf_counter()
    first_text_ms = None
    parts: list[str] = []
    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:  # text only; thinking is not included
            if first_text_ms is None:
                first_text_ms = round((time.perf_counter() - started) * 1000)
            parts.append(chunk)
            yield "text", chunk
        response = stream.get_final_message()
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    try:
        parsed, stats = _parse_reply(response, "".join(parts), kwargs, elapsed_ms)
    except ClaudeResponseError as exc:
        exc.stats["first_text_ms"] = first_text_ms
        raise
    stats["first_text_ms"] = first_text_ms
    yield "done", (parsed, stats)
