"""Post-call validation: Python repairs what it can, and asks Claude once to
fix what it can't. No real API calls."""

import json
import logging
from types import SimpleNamespace

import pytest

import ai.client
from ai.budget import generate_budget_plan, stream_budget_plan
from ai.plan_checks import in_pantry, missing_named_ingredients, validate_plan

DEALS = [
    {"clean_name": "Villaggio Bread", "price": 3.00, "price_per_kg": None, "category": "Grains & Bread"},
    {"clean_name": "Fresh Pork Shoulder", "price": 9.00, "price_per_kg": None, "category": "Meat & Seafood"},
    {"clean_name": "Large White Eggs", "price": 4.00, "price_per_kg": None, "category": "Dairy & Eggs"},
    {"clean_name": "Strawberry Jam", "price": 4.00, "price_per_kg": None, "category": "Other"},
]
PROFILE = {"budget": 40, "people": 1, "cuisines": [], "pantry": ["Rice", "Cooking oil"], "merchant": "FreshCo",
           "include_breakfast": False}


def line(name, subtotal, deal_name=None, quantity=1):
    return {"name": name, "deal_name": deal_name or name, "price": subtotal / quantity, "quantity": quantity,
            "subtotal": subtotal, "sold_by_weight": False}


def meal(name, uses, from_home=(), servings=7, kind="main"):
    return {"name": name, "meal": kind, "servings": servings, "uses": list(uses), "from_home": list(from_home)}


# --- the checks -----------------------------------------------------------------

def test_from_home_must_be_something_the_shopper_has():
    pantry = ["Rice", "Cooking oil", "Eggs", "Soy sauce"]
    for item in ["rice", "cooked rice", "Eggs", "egg", "oil", "vegetable oil", "soy sauce", "salt", "Pepper", "water"]:
        assert in_pantry(item, pantry), item
    for item in ["turmeric", "cumin", "butter", "fish sauce", "garlic", ""]:
        assert not in_pantry(item, pantry), item


def test_a_meal_name_must_match_what_the_meal_uses():
    basket = [line("Bread", 3, "Villaggio Bread"), line("Eye of round", 12, "Beef Eye of Round Roast")]
    assert missing_named_ingredients(meal("Scrambled Eggs & Toast", ["Bread"]), basket) == ["egg"]
    assert missing_named_ingredients(meal("Scrambled Eggs & Toast", ["Bread"], ["eggs"]), basket) == []
    assert missing_named_ingredients(meal("Beef stew", ["Eye of round"]), basket) == []     # the flyer name counts
    assert missing_named_ingredients(meal("Noodle soup with greens", ["Bread"]), basket) == ["greens", "noodles"]
    assert missing_named_ingredients(meal("Eggplant stir fry", ["Bread"]), basket) == ["eggplant"]  # not "egg"
    assert missing_named_ingredients(meal("Pork adobo", ["Bread"], ["rice"]), basket) == ["pork"]


def test_validation_drops_ingredients_that_are_neither_bought_nor_at_home():
    plan = {"basket": [line("Bread", 3), line("Pork", 9)],
            "meals": [meal("Pork with rice", ["Pork", "Ghost pepper"], ["rice", "turmeric", "salt"])]}
    fixed, report = validate_plan(plan, PROFILE)
    assert fixed["meals"][0]["uses"] == ["Pork"]
    assert fixed["meals"][0]["from_home"] == ["rice", "salt"]
    assert len(report["fixes"]) == 2 and report["problems"] == []
    assert plan["meals"][0]["uses"] == ["Pork", "Ghost pepper"]          # the input is not modified


def test_over_budget_drops_unused_items_first_cheapest_first():
    plan = {"basket": [line("Pork", 30), line("Jam", 6), line("Candles", 4), line("Bread", 5)],
            "meals": [meal("Pork sandwiches", ["Pork", "Bread"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 36})        # $45, so $9 over
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Bread"]      # both unused items go
    assert fixed["estimated_total"] == 35 and report["problems"] == []
    assert "Candles" in report["fixes"][0] and "Jam" in report["fixes"][1]


def test_over_budget_then_trims_what_the_fewest_meals_rely_on():
    plan = {"basket": [line("Pork", 30), line("Eggs", 8, quantity=2), line("Bread", 5)],
            "meals": [meal("Pork sandwiches", ["Pork", "Bread"]), meal("Pork fried rice with egg on toast", ["Pork", "Eggs", "Bread"], ["rice"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 40})        # $43; only one meal needs the eggs
    eggs = next(l for l in fixed["basket"] if l["name"] == "Eggs")
    assert (eggs["quantity"], eggs["subtotal"]) == (1, 4.0)               # one fewer, not none
    assert fixed["estimated_total"] == 39

    fixed, report = validate_plan(plan, {**PROFILE, "budget": 34})        # needs more than that
    assert [l["name"] for l in fixed["basket"]] == ["Pork"]               # eggs and bread dropped
    assert fixed["meals"][0]["uses"] == ["Pork"] and fixed["estimated_total"] == 30


def test_a_name_that_promises_a_missing_ingredient_is_a_problem_not_a_fix():
    plan = {"basket": [line("Bread", 3)], "meals": [meal("Scrambled Eggs & Toast", ["Bread"], ["eggs"])]}
    fixed, report = validate_plan(plan, PROFILE)                          # eggs are not in the pantry
    assert fixed["meals"][0]["from_home"] == []
    assert [p["type"] for p in report["problems"]] == ["name_promises_missing_ingredient"]
    assert "named for egg" in report["problems"][0]["detail"]


# --- one retry ------------------------------------------------------------------

BAD = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}],
       "meals": [{"name": "Scrambled Eggs & Toast", "servings": 7, "uses": [1], "from_home": ["eggs"]},
                 {"name": "Toast", "servings": 7, "uses": [1]}]}
GOOD = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}, {"id": 3, "name": "Eggs", "quantity": 1}],
        "meals": [{"name": "Scrambled Eggs & Toast", "servings": 7, "uses": [1, 3]},
                  {"name": "Toast", "servings": 7, "uses": [1]}]}
WORSE = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}],
         "meals": [{"name": "Scrambled Eggs & Toast", "servings": 7, "uses": [1]},
                   {"name": "Pork and Mushroom Stir Fry", "servings": 7, "uses": [1]}]}


def reply(plan, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(plan))], stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=4000, output_tokens=1000))


@pytest.fixture
def claude(monkeypatch):
    """Fake client: .replies are returned in order by messages.create."""
    fake = SimpleNamespace(requests=[], replies=[])

    def create(**kwargs):
        fake.requests.append(kwargs)
        return fake.replies.pop(0)

    class Stream:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            text = self.response.content[0].text
            return SimpleNamespace(text_stream=iter([text[i:i + 9] for i in range(0, len(text), 9)]),
                                   get_final_message=lambda: self.response)

        def __exit__(self, *exc):
            pass

    def stream(**kwargs):
        fake.requests.append(kwargs)
        return Stream(fake.replies.pop(0))

    fake.messages = SimpleNamespace(create=create, stream=stream)
    monkeypatch.setattr(ai.client, "get_client", lambda: fake)
    return fake


def test_a_clean_plan_is_one_call(claude):
    claude.replies = [reply(GOOD)]
    stats = {}
    plan = generate_budget_plan(PROFILE, DEALS, stats=stats)
    assert len(claude.requests) == 1 and plan["estimated_total"] == 7.0
    assert (stats["calls"], stats["retried"], stats["problems"], stats["input_tokens"]) == (1, False, 0, 4000)


def test_a_flagged_meal_triggers_one_retry_that_lists_the_problem(claude, caplog):
    claude.replies = [reply(BAD), reply(GOOD)]
    stats = {}
    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        plan = generate_budget_plan(PROFILE, DEALS, stats=stats)

    assert len(claude.requests) == 2
    first, retry = claude.requests
    assert len(first["messages"]) == 1 and [m["role"] for m in retry["messages"]] == ["user", "assistant", "user"]
    assert retry["messages"][0] == first["messages"][0]                    # same prompt
    assert json.loads(retry["messages"][1]["content"]) == BAD              # its own earlier answer
    note = retry["messages"][2]["content"]
    assert '"Scrambled Eggs & Toast" is named for egg but uses none' in note and note.count("\n- ") == 1

    assert [l["name"] for l in plan["basket"]] == ["Bread", "Eggs"]         # the corrected plan
    assert (stats["calls"], stats["retried"], stats["problems_first"], stats["problems"]) == (2, True, 1, 0)
    assert stats["input_tokens"] == 8000 and stats["output_tokens"] == 2000  # both calls counted
    assert "retried=True" in caplog.text and "retries so far:" in caplog.text


def test_when_the_retry_is_no_better_the_better_plan_is_kept(claude):
    claude.replies = [reply(BAD), reply(WORSE)]
    stats = {}
    plan = generate_budget_plan(PROFILE, DEALS, stats=stats)
    assert len(claude.requests) == 2                                        # never a third call
    assert [m["name"] for m in plan["meals"]] == ["Scrambled Eggs & Toast", "Toast"]   # first plan: 1 problem vs 3
    assert (stats["retried"], stats["problems_first"], stats["problems"]) == (True, 1, 1)


def test_a_retry_that_fails_keeps_the_first_plan(claude):
    claude.replies = [reply(BAD), reply(GOOD, stop_reason="max_tokens")]
    stats = {}
    plan = generate_budget_plan(PROFILE, DEALS, stats=stats)
    assert [l["name"] for l in plan["basket"]] == ["Bread"]
    assert (stats["calls"], stats["retried"], stats["problems"]) == (2, False, 1)


def test_the_stream_announces_the_retry_and_ends_with_the_corrected_plan(claude):
    claude.replies = [reply(BAD), reply(GOOD)]
    events = list(stream_budget_plan(PROFILE, DEALS))
    kinds = [kind for kind, _ in events]
    assert kinds == ["item", "meal", "meal", "retry", "plan"]
    assert "named for egg" in events[3][1]["problems"][0]
    plan, stats = events[-1][1]
    assert [l["name"] for l in plan["basket"]] == ["Bread", "Eggs"] and stats["retried"] is True
    assert "stream" not in claude.requests[1]                              # the correction is a plain call
