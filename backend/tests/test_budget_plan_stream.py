"""Streaming meal plans: items and meals arrive as they are written, and the
validated final plan replaces them. No real API calls."""

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai.client
import routes.budget
from ai.budget import _ObjectScanner, stream_budget_plan
from ai.client import ClaudeResponseError

DEALS = [
    {"clean_name": "Fresh Pork Shoulder Blade Roast", "price": 8.80, "price_per_kg": 4.40,
     "category": "Meat & Seafood", "weight_kg": 2.0},
    {"clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "category": "Produce"},
    {"clean_name": "Large White Eggs 12 Pk", "price": 3.49, "price_per_kg": None, "category": "Dairy & Eggs"},
]
PROFILE = {"budget": 120, "people": 1, "cuisines": ["Chinese"], "pantry": ["Rice"], "merchant": "FreshCo"}

REPLY = json.dumps({
    "basket": [
        {"id": 1, "name": "Pork shoulder", "quantity": 1, "note": "cheap braise {not a brace}"},
        {"id": 2, "name": "Bok choy", "quantity": 2, "note": 'greens "on sale"'},
        {"id": 99, "name": "Invented", "quantity": 1},
        {"id": 3, "name": "Eggs", "quantity": 1},
    ],
    "swap_suggestion": None,
    "meals": [
        {"name": "Fried eggs on rice", "meal": "breakfast", "servings": 7, "uses": [3],
         "from_home": ["Rice"], "appliance": "Stovetop", "prep_time": "10 minutes", "instructions": "Fry."},
        {"name": "Red-braised pork", "meal": "main", "servings": 14, "uses": [1, 2],
         "from_home": ["Rice"], "appliance": "Stovetop", "prep_time": "90 minutes", "instructions": "Braise."},
    ],
}, indent=2)


def chunks(text, size=7):
    return [text[i:i + size] for i in range(0, len(text), size)]


@pytest.fixture
def claude(monkeypatch):
    """Fake Anthropic client whose messages.stream() replays .text in small chunks."""
    fake = SimpleNamespace(requests=[], text="```json\n" + REPLY + "\n```", stop_reason="end_turn", closed=0)

    class Stream:
        def __enter__(self):
            return SimpleNamespace(
                text_stream=iter(chunks(fake.text)),
                get_final_message=lambda: SimpleNamespace(
                    stop_reason=fake.stop_reason,
                    usage=SimpleNamespace(input_tokens=4000, output_tokens=1200),
                ),
            )

        def __exit__(self, *exc):
            fake.closed += 1

    def stream(**kwargs):
        fake.requests.append(kwargs)
        return Stream()

    fake.messages = SimpleNamespace(stream=stream)
    monkeypatch.setattr(ai.client, "get_client", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    """The budget routes on their own app, so the rate limiter isn't involved."""
    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [dict(d) for d in DEALS])
    monkeypatch.setattr(routes.budget, "select_candidates", lambda deals, cuisines, **kw: deals)
    app = FastAPI()
    app.include_router(routes.budget.router, prefix="/api")
    return TestClient(app)


def read_events(response):
    events = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


BODY = {"postal_code": "N6G 2J8", "merchant": "FreshCo", "budget": 120, "people": 1,
        "cuisines": ["Chinese"], "pantry": ["Rice"]}


# --- finding finished objects in a partial reply --------------------------------

def test_scanner_returns_each_object_once_it_closes_whatever_the_chunking():
    for size in (1, 3, 7, 50, len(REPLY)):
        scanner = _ObjectScanner()
        found = [obj for chunk in chunks(REPLY, size) for obj in scanner.feed(chunk)]
        assert [o.get("id") or o["name"] for o in found] == [1, 2, 99, 3, "Fried eggs on rice", "Red-braised pork"]
    assert found[0]["note"] == "cheap braise {not a brace}"      # braces and quotes inside strings
    assert found[1]["note"] == 'greens "on sale"'


def test_scanner_ignores_a_half_written_object():
    scanner = _ObjectScanner()
    assert scanner.feed('{"basket": [{"id": 1, "name": "Pork"}, {"id": 2, "na') == [{"id": 1, "name": "Pork"}]
    assert scanner.feed('me": "Bok choy"}') == [{"id": 2, "name": "Bok choy"}]


# --- the plan stream ------------------------------------------------------------

def test_items_and_meals_are_priced_in_python_before_they_are_sent(claude):
    events = list(stream_budget_plan(PROFILE, DEALS))
    assert [kind for kind, _ in events] == ["item", "item", "item", "meal", "meal", "plan"]   # ID 99 never sent
    pork = events[0][1]
    assert (pork["name"], pork["price"], pork["subtotal"], pork["group"], pork["size"]) == (
        "Pork shoulder", 8.80, 8.80, "Proteins", "2 kg")
    assert events[1][1]["subtotal"] == 3.98                       # 2 x $1.99 from the flyer
    assert events[4][1]["uses"] == ["Pork shoulder", "Bok choy"]  # IDs resolved against items so far
    plan, stats = events[-1][1]
    assert plan["estimated_total"] == 16.27 and plan["servings_total"] == 21
    assert [i["name"] for i in plan["basket"]] == [e[1]["name"] for e in events[:3]]
    assert stats["input_tokens"] == 4000 and stats["output_tokens"] == 1200
    assert claude.requests[0]["model"] == "claude-haiku-4-5" and claude.requests[0]["max_tokens"] == 4096


def test_a_cut_off_reply_raises_after_the_partial_items(claude):
    claude.text, claude.stop_reason = REPLY[:300], "max_tokens"
    stream = stream_budget_plan(PROFILE, DEALS)
    seen = []
    with pytest.raises(ClaudeResponseError) as error:
        for event in stream:
            seen.append(event[0])
    assert error.value.reason == "max_tokens" and error.value.stats["output_tokens"] == 1200
    assert seen and set(seen) == {"item"}


def test_leaving_mid_stream_ends_the_api_request(claude):
    stream = stream_budget_plan(PROFILE, DEALS)
    next(stream)
    stream.close()
    assert claude.closed == 1


# --- the route ------------------------------------------------------------------

def test_route_streams_start_items_meals_then_the_validated_plan(claude, client, caplog):
    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        response = client.post("/api/budget-plan/stream", json=BODY)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = read_events(response)
    assert [name for name, _ in events] == ["start", "item", "item", "item", "meal", "meal", "done"]

    start = events[0][1]
    assert start["servings_target"] == 21 and start["include_breakfast"] is True
    assert start["cuisine_note"].startswith("Not many Chinese staples on sale at FreshCo")

    # "done" is exactly what /budget-plan returns for the same reply.
    done = events[-1][1]
    assert done["estimated_total"] == 16.27 and done["under_budget"] is True
    assert set(done) == {"postal_code", "merchant", "budget", "estimated_total", "under_budget",
                         "skipped_pantry_items", "cuisine_note", "plan"}
    assert [m["meal"] for m in done["plan"]["meals"]] == ["breakfast", "main"]

    line = next(r.getMessage() for r in caplog.records if "budget_plan timing" in r.getMessage())
    assert "outcome=ok" in line and "first_item_ms=" in line and "claude_ms=" in line
    assert "deals_sent=3" in line and "input_tokens=4000 output_tokens=1200" in line


def test_route_sends_an_error_event_when_the_reply_is_unusable(claude, client, caplog):
    claude.text, claude.stop_reason = REPLY[:300], "max_tokens"
    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        events = read_events(client.post("/api/budget-plan/stream", json=BODY))
    assert events[-1] == ("error", {"detail": routes.budget.PLAN_FAILED_MESSAGE})
    assert "done" not in [name for name, _ in events]
    assert "outcome=claude_max_tokens" in caplog.text


def test_route_answers_with_plain_json_when_the_store_has_no_deals(claude, client, monkeypatch):
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [])
    response = client.post("/api/budget-plan/stream", json=BODY)
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["plan"] is None and not claude.requests
