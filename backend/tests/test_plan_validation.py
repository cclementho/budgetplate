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
    plan = {"basket": [line("Pork", 9)],
            "meals": [meal("Pork with rice", ["Pork", "Ghost pepper"], ["rice", "turmeric", "salt"])]}
    fixed, report = validate_plan(plan, PROFILE)
    assert fixed["meals"][0]["uses"] == ["Pork"]
    assert fixed["meals"][0]["from_home"] == ["rice", "salt"]
    assert len(report["fixes"]) == 2 and report["problems"] == []
    assert plan["meals"][0]["uses"] == ["Pork", "Ghost pepper"]          # the input is not modified


def test_items_no_meal_uses_are_always_dropped():
    plan = {"basket": [line("Pork", 30), line("Jam", 6), line("Eggs", 4), line("Bread", 5)],
            "meals": [meal("Pork sandwiches", ["Pork", "Bread"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 100})      # $45 of $100: well under budget
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Bread"]
    assert fixed["estimated_total"] == 35 and report["problems"] == []
    assert report["fixes"] == ["dropped 'Jam' ($6.00): no meal uses it", "dropped 'Eggs' ($4.00): no meal uses it"]

    # A reply with a basket and no meals at all isn't emptied.
    fixed, _ = validate_plan({"basket": [line("Pork", 30)], "meals": []}, PROFILE)
    assert [l["name"] for l in fixed["basket"]] == ["Pork"]


def test_over_budget_then_trims_what_the_fewest_meals_rely_on():
    plan = {"basket": [line("Pork", 30), line("Eggs", 8, quantity=2), line("Bread", 5)],
            "meals": [meal("Pork sandwiches", ["Pork", "Bread"]), meal("Pork fried rice with egg on toast", ["Pork", "Eggs", "Bread"], ["rice"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 40})        # $43; only one meal needs the eggs
    eggs = next(l for l in fixed["basket"] if l["name"] == "Eggs")
    assert (eggs["quantity"], eggs["subtotal"]) == (1, 4.0)               # one fewer, not none
    assert fixed["estimated_total"] == 39

    # Needing more than that would mean removing the bread or eggs the meals
    # are named for, so the basket is left alone and reported instead.
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 34})
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Eggs", "Bread"] and fixed["estimated_total"] == 39
    assert [p["type"] for p in report["problems"]] == ["over_budget"]


def test_a_name_that_promises_a_missing_ingredient_is_a_problem_not_a_fix():
    plan = {"basket": [line("Bread", 3)], "meals": [meal("Scrambled Eggs & Toast", ["Bread"], ["eggs"])]}
    fixed, report = validate_plan(plan, PROFILE)                          # eggs are not in the pantry
    assert fixed["meals"][0]["from_home"] == []
    assert [p["type"] for p in report["problems"]] == ["name_promises_missing_ingredient"]
    assert "named for egg" in report["problems"][0]["detail"]


# --- corrections: a small rename call, or one full retry ---------------------------

BREAKFAST = {**PROFILE, "include_breakfast": True}

# Named for eggs it doesn't have: a new name settles it.
MISNAMED = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}],
            "meals": [{"name": "Scrambled Eggs & Toast", "servings": 7, "uses": [1], "from_home": ["eggs"]},
                      {"name": "Toast", "servings": 7, "uses": [1]}]}
# A breakfast with onion: the plan itself has to change.
ONION_BREAKFAST = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}, {"id": 4, "name": "Jam", "quantity": 1}],
                   "meals": [{"name": "Onion jam on toast", "meal": "breakfast", "servings": 7, "uses": [1, 4]},
                             {"name": "Toast", "servings": 14, "uses": [1]}]}
GOOD = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}, {"id": 3, "name": "Eggs", "quantity": 1}],
        "meals": [{"name": "Scrambled Eggs & Toast", "meal": "breakfast", "servings": 7, "uses": [1, 3]},
                  {"name": "Toast", "servings": 14, "uses": [1]}]}
WORSE = {"basket": [{"id": 1, "name": "Bread", "quantity": 1}],
         "meals": [{"name": "Potato hash", "meal": "breakfast", "servings": 7, "uses": [1]},
                   {"name": "Onion toast", "meal": "breakfast", "servings": 7, "uses": [1]}]}


def reply(plan, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(plan))], stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=4000, output_tokens=1000))


def names(*pairs):
    """A rename reply: names((1, "Toast"))."""
    small = reply({"names": [{"meal": n, "name": name} for n, name in pairs]})
    small.usage = SimpleNamespace(input_tokens=150, output_tokens=30)
    return small


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
    assert (stats["calls"], stats["retried"], stats["renames"], stats["problems"]) == (1, False, 0, 0)
    assert stats["input_tokens"] == 4000


def test_a_misnamed_meal_gets_a_small_rename_call_not_a_full_retry(claude, caplog):
    claude.replies = [reply(MISNAMED), names((1, "Plain toast"))]
    stats = {}
    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        plan = generate_budget_plan(PROFILE, DEALS, stats=stats)

    assert len(claude.requests) == 2
    rename = claude.requests[1]
    text = rename["messages"][0]["content"]
    assert len(rename["messages"]) == 1 and rename["max_tokens"] == 400       # small and on its own
    assert '1. "Scrambled Eggs & Toast" is made from: Villaggio Bread.' in text and "mentions egg" in text
    assert '"Toast" is made from' not in text                                  # only the flagged meal
    assert "DEALS ON SALE" not in text and "Fresh Pork Shoulder" not in text            # not the plan or the flyer

    assert [m["name"] for m in plan["meals"]] == ["Plain toast", "Toast"]
    assert [l["name"] for l in plan["basket"]] == ["Villaggio Bread"]          # the basket is untouched
    assert (stats["calls"], stats["retried"], stats["renames"]) == (2, False, 1)
    assert (stats["problems_first"], stats["problems"]) == (1, 0)
    assert (stats["input_tokens"], stats["output_tokens"]) == (4150, 1030)     # both calls counted
    assert "retried=False renames=1" in caplog.text and "full retries and" in caplog.text


def test_a_new_name_is_kept_only_if_it_is_more_accurate(claude):
    claude.replies = [reply(MISNAMED), names((1, "Egg and cheese toast"))]     # still promises egg, adds cheese
    stats = {}
    plan = generate_budget_plan(PROFILE, DEALS, stats=stats)
    assert plan["meals"][0]["name"] == "Scrambled Eggs & Toast"
    assert (stats["renames"], stats["problems"]) == (0, 1)

    claude.replies = [reply(MISNAMED), reply({"names": "nonsense"}, stop_reason="max_tokens")]
    assert generate_budget_plan(PROFILE, DEALS)["meals"][0]["name"] == "Scrambled Eggs & Toast"


def test_a_problem_python_cannot_fix_triggers_one_full_retry(claude, caplog):
    claude.replies = [reply(ONION_BREAKFAST), reply(GOOD)]
    stats = {}
    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        plan = generate_budget_plan(BREAKFAST, DEALS, stats=stats)

    assert len(claude.requests) == 2
    first, retry = claude.requests
    assert [m["role"] for m in retry["messages"]] == ["user", "assistant", "user"]
    assert retry["messages"][0] == first["messages"][0]                        # same prompt
    assert json.loads(retry["messages"][1]["content"]) == ONION_BREAKFAST      # its own earlier answer
    note = retry["messages"][2]["content"]
    assert 'the breakfast "Onion jam on toast" contains onion' in note
    assert "named for" not in note                                              # name problems aren't sent here

    assert [l["name"] for l in plan["basket"]] == ["Villaggio Bread", "Large White Eggs"]            # the corrected plan
    assert (stats["calls"], stats["retried"], stats["renames"], stats["problems"]) == (2, True, 0, 0)
    assert "retried=True renames=0" in caplog.text and "renamed plans in" in caplog.text


def test_when_the_retry_is_no_better_the_first_plan_is_kept_and_then_renamed(claude):
    claude.replies = [reply(ONION_BREAKFAST), reply(WORSE), names((1, "Jam on toast"))]
    stats = {}
    plan = generate_budget_plan(BREAKFAST, DEALS, stats=stats)
    assert len(claude.requests) == 3                                            # never a second full retry
    assert [m["name"] for m in plan["meals"]] == ["Jam on toast", "Toast"]      # first plan, with an honest name
    assert (stats["calls"], stats["retried"], stats["renames"], stats["problems"]) == (3, True, 1, 0)


def test_a_retry_that_fails_keeps_the_first_plan(claude):
    claude.replies = [reply(ONION_BREAKFAST), reply(GOOD, stop_reason="max_tokens"), names()]
    stats = {}
    plan = generate_budget_plan(BREAKFAST, DEALS, stats=stats)
    assert [l["name"] for l in plan["basket"]] == ["Villaggio Bread", "Strawberry Jam"]
    assert stats["retried"] is False and stats["problems"] == 2                 # onion at breakfast, and its name


def test_the_stream_announces_a_full_retry_but_not_a_rename(claude):
    claude.replies = [reply(ONION_BREAKFAST), reply(GOOD)]
    events = list(stream_budget_plan(BREAKFAST, DEALS))
    assert [kind for kind, _ in events] == ["item", "item", "meal", "meal", "retry", "plan"]
    assert "contains onion" in events[4][1]["problems"][0]
    plan, stats = events[-1][1]
    assert [l["name"] for l in plan["basket"]] == ["Villaggio Bread", "Large White Eggs"] and stats["retried"] is True
    assert "stream" not in claude.requests[1]                                   # the correction is a plain call

    claude.replies = [reply(MISNAMED), names((1, "Plain toast"))]
    events = list(stream_budget_plan(PROFILE, DEALS))
    assert [kind for kind, _ in events] == ["item", "meal", "meal", "plan"]     # no retry event
    plan, stats = events[-1][1]
    assert plan["meals"][0]["name"] == "Plain toast" and (stats["retried"], stats["renames"]) == (False, 1)


# --- breakfast --------------------------------------------------------------------

def test_breakfasts_with_potato_or_onion_are_flagged():
    from ai.budget import build_budget_prompt
    from ai.plan_checks import breakfast_misfits

    basket = [line("Potatoes", 3, "Russet Potatoes 5 Lb"), line("Eggs", 4), line("Onions", 2, "Yellow Onions 3 Lb"),
              line("Scallions", 1, "Green Onions")]
    assert breakfast_misfits(meal("Breakfast hash", ["Potatoes", "Eggs"], kind="breakfast"), basket) == ["potatoes"]
    assert breakfast_misfits(meal("Omelette", ["Eggs", "Onions"], kind="breakfast"), basket) == ["onion"]
    assert breakfast_misfits(meal("Sweet potato hash", ["Eggs"], kind="breakfast"), basket) == ["potatoes"]  # by name
    assert breakfast_misfits(meal("Congee with egg", ["Eggs", "Scallions"], ["rice"], kind="breakfast"), basket) == []
    assert breakfast_misfits(meal("Potato and onion curry", ["Potatoes", "Onions"]), basket) == []   # dinner is fine

    plan = {"basket": basket, "meals": [meal("Egg and potato scramble", ["Potatoes", "Eggs"], kind="breakfast")]}
    _, report = validate_plan(plan, PROFILE)
    assert [p["type"] for p in report["problems"]] == ["breakfast_with_potato_or_onion"]
    assert "breakfasts must not use potatoes or onion" in report["problems"][0]["detail"]

    prompt = build_budget_prompt({**PROFILE, "include_breakfast": True}, DEALS)
    assert "things people eat in the morning: eggs, oats, toast, congee, yogurt, fruit" in prompt
    assert "NEVER put potatoes or onion in a\n  breakfast" in prompt


def test_one_drop_that_settles_the_budget_beats_several_small_ones():
    plan = {"basket": [line("Pork", 30), line("Grapes", 1.5), line("Yams", 1), line("Chicken", 4), line("Beef", 9)],
            "meals": [meal("Sunday roast", ["Pork"]), meal("Fruit bowl", ["Grapes"]),
                      meal("Baked side", ["Yams"]), meal("Soup", ["Chicken"]), meal("Stew", ["Beef"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 42.5})     # $45.50, so $3 over
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Grapes", "Yams", "Beef"]   # only the $4 chicken goes
    assert len(report["fixes"]) == 1


def test_an_item_a_meal_is_named_for_is_never_removed():
    plan = {"basket": [line("Pork", 30), line("Chicken", 4), line("Carrots", 5), line("Beef", 9)],
            "meals": [meal("Pork roast", ["Pork"]), meal("Chicken soup", ["Chicken", "Carrots"]),
                      meal("Beef stew", ["Beef"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 45})        # $48, so $3 over
    # The $4 chicken would settle it, but "Chicken soup" is named for it: the carrots go instead.
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Chicken", "Beef"]
    assert fixed["meals"][1]["uses"] == ["Chicken"] and report["problems"] == []


def test_when_only_named_items_are_left_over_budget_is_reported_not_forced():
    plan = {"basket": [line("Pork", 30), line("Chicken", 14)],
            "meals": [meal("Pork roast", ["Pork"]), meal("Chicken soup", ["Chicken"])]}
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 40})
    assert [l["name"] for l in fixed["basket"]] == ["Pork", "Chicken"]    # nothing was removed
    assert [p["type"] for p in report["problems"]] == ["over_budget"]

    plan["basket"][1] = line("Chicken", 14, quantity=2)                  # but one fewer is allowed
    fixed, report = validate_plan(plan, {**PROFILE, "budget": 40})
    assert fixed["estimated_total"] == 37 and report["problems"] == []
