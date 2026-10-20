"""The meal-plan Claude call: request shape, and clean failures instead of 500s.

No real API calls: get_client() is replaced with a fake that returns canned
responses shaped like the Anthropic SDK's.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ai.client
import routes.budget
from ai.budget import generate_budget_plan
from ai.client import ClaudeResponseError, call_claude_json
from config import settings

PLAN_JSON = '{"basket": [], "estimated_total": 0, "swap_suggestion": null, "meals": []}'


def fake_response(*, text=None, stop_reason="end_turn", thinking=True):
    content = [SimpleNamespace(type="thinking", thinking="", signature="sig")] if thinking else []
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=3754, output_tokens=4096 if stop_reason == "max_tokens" else 900),
    )


@pytest.fixture
def claude(monkeypatch):
    """Fake Anthropic client: set .response, read back .requests."""
    fake = SimpleNamespace(requests=[], response=fake_response(text=PLAN_JSON))

    def create(**kwargs):
        fake.requests.append(kwargs)
        return fake.response

    fake.messages = SimpleNamespace(create=create)
    monkeypatch.setattr(ai.client, "get_client", lambda: fake)
    return fake


def test_meal_plans_use_haiku_5_5_with_room_to_think_and_no_effort_setting(claude):
    assert generate_budget_plan({"budget": 40, "people": 1, "merchant": "No Frills"}, []) == {
        "basket": [], "estimated_total": 0, "swap_suggestion": None, "meals": []}
    (sent,) = claude.requests
    assert settings.CLAUDE_MODEL == "claude-haiku-5-5"
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["max_tokens"] == 16_000
    assert "output_config" not in sent and "extra_body" not in sent and "thinking" not in sent


def test_thinking_blocks_before_the_answer_are_ignored(claude):
    claude.response = fake_response(text=PLAN_JSON, thinking=True)
    assert call_claude_json("prompt")["meals"] == []


@pytest.mark.parametrize("response, reason", [
    (fake_response(text='{"basket": [{"name": "Bok', stop_reason="max_tokens"), "max_tokens"),
    (fake_response(text=None, stop_reason="max_tokens"), "max_tokens"),   # all output spent thinking
    (fake_response(text=None, stop_reason="end_turn"), "empty"),
    (fake_response(text="   ", stop_reason="end_turn"), "empty"),
    (fake_response(text=None, stop_reason="refusal"), "refusal"),
    (fake_response(text="Sorry, here is a plan: basket...", stop_reason="end_turn"), "invalid_json"),
])
def test_unusable_replies_raise_a_clear_error_and_are_logged(claude, caplog, response, reason):
    claude.response = response
    with caplog.at_level(logging.ERROR, logger="budgetplate.ai"):
        with pytest.raises(ClaudeResponseError) as err:
            call_claude_json("prompt", max_tokens=16_000)
    assert err.value.reason == reason
    (record,) = caplog.records
    assert f"stop_reason={response.stop_reason}" in record.getMessage()
    assert "max_tokens=16000" in record.getMessage()
    assert "output_tokens=" in record.getMessage()


def test_cut_off_plan_returns_a_friendly_502_not_a_server_error(claude, monkeypatch):
    import main

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [
        {"clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "category": "Produce"}])
    claude.response = fake_response(text='{"basket": [', stop_reason="max_tokens")

    res = TestClient(main.app).post("/api/budget-plan", json={
        "postal_code": "M5S 1Z9", "merchant": "No Frills", "budget": 40, "people": 1})

    assert res.status_code == 502
    assert res.json() == {"detail": routes.budget.PLAN_FAILED_MESSAGE}
