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
from ai.budget import build_budget_prompt, finalize_plan, generate_budget_plan
from ai.client import ClaudeResponseError, call_claude_json
from config import settings

PLAN_JSON = '{"basket": [], "swap_suggestion": null, "meals": []}'
EMPTY_PLAN = {"basket": [], "meals": [], "estimated_total": 0, "servings_total": 0,
              "servings_target": 21, "include_breakfast": True, "swap_suggestion": None}

DEALS = [
    {"clean_name": "Fresh Pork Shoulder Blade Roast", "price": 8.80, "price_per_kg": 8.80, "category": "Meat & Seafood"},
    {"clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "category": "Produce"},
    {"clean_name": "Sunrise Tofu", "price": 2.50, "price_per_kg": 7.14, "category": "Other"},
]
PROFILE = {"budget": 120, "people": 1, "cuisines": ["Chinese"], "restriction": None,
           "pantry": ["Rice", "Soy sauce"], "merchant": "FreshCo"}


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


def test_meal_plans_use_haiku_4_5_at_4096_tokens_with_no_effort_setting(claude):
    assert generate_budget_plan({"budget": 40, "people": 1, "merchant": "No Frills"}, []) == EMPTY_PLAN
    (sent,) = claude.requests
    assert settings.CLAUDE_MODEL == "claude-haiku-4-5"
    assert sent["model"] == "claude-haiku-4-5"
    assert sent["max_tokens"] == 4096
    assert "output_config" not in sent and "extra_body" not in sent and "thinking" not in sent


def test_model_and_token_cap_can_be_overridden_from_the_environment(monkeypatch):
    import importlib

    import config

    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("CLAUDE_MAX_TOKENS", "16000")
    try:
        fresh = importlib.reload(config).settings
        assert (fresh.CLAUDE_MODEL, fresh.CLAUDE_MAX_TOKENS) == ("claude-haiku-5-5", 16000)
        monkeypatch.setenv("CLAUDE_MODEL", "")  # blank means "use the default"
        monkeypatch.delenv("CLAUDE_MAX_TOKENS")
        fresh = importlib.reload(config).settings
        assert (fresh.CLAUDE_MODEL, fresh.CLAUDE_MAX_TOKENS) == ("claude-haiku-4-5", 4096)
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_thinking_blocks_before_the_answer_are_ignored(claude):
    claude.response = fake_response(text=PLAN_JSON, thinking=True)
    assert call_claude_json("prompt")["meals"] == []


# --- the prompt ---------------------------------------------------------------

def test_prompt_asks_for_a_full_week_with_breakfast_by_default():
    prompt = build_budget_prompt({**PROFILE, "people": 2}, DEALS)
    assert "Plan ONE week of breakfasts, lunches and dinners" in prompt
    assert "21 servings per person, so about 42 servings in total" in prompt
    assert "7 of each person's servings are breakfasts (14 in total)" in prompt
    assert "eggs, oats, toast, congee" in prompt           # breakfasts can be simple
    assert "1 or 2 breakfasts and 5 or 6 lunch/dinner meals" in prompt
    assert '"meal": "<"breakfast" or "main">"' in prompt


def test_prompt_without_breakfast_plans_lunches_and_dinners_only():
    prompt = build_budget_prompt({**PROFILE, "people": 2, "include_breakfast": False}, DEALS)
    assert "Plan ONE week of lunches and dinners" in prompt
    assert "14 servings per person, so about 28 servings in total" in prompt
    assert "5 to 7 different meals" in prompt
    assert "breakfast" not in prompt.lower()
    assert "(never go over it)" in prompt
    assert "spending up to it" not in prompt and "Use the budget" not in prompt   # no push to spend
    assert "Underspending is a WIN" not in prompt           # the old rule that starved baskets
    assert "NO FILLER" in prompt                            # still never pad
    assert '"why"' not in prompt                            # the model no longer explains each item
    assert "FOOD ONLY" not in prompt and "SENSIBLE PACK SIZES" not in prompt   # Python enforces these now
    assert "estimated_total" not in prompt and '"subtotal"' not in prompt  # Python does the maths


def test_prompt_lists_deals_with_ids_the_model_must_use():
    prompt = build_budget_prompt(PROFILE, DEALS)
    assert "1 | Fresh Pork Shoulder Blade Roast | $8.80 | $8.80/kg | Meat & Seafood" in prompt
    assert "2 | Bok Choy | $1.99 | $n/a/kg | Produce" in prompt
    assert "ALREADY HAS AT HOME (using them is optional): Rice, Soy sauce" in prompt


# --- pricing the model's choices in Python ----------------------------------------

def test_prices_and_subtotals_come_from_the_flyer_not_the_model():
    raw = {
        "basket": [
            {"id": 1, "name": "Pork shoulder", "quantity": 2, "why": "main protein",
             "price": 0.01, "subtotal": 0.02},                       # model-supplied money is ignored
            {"id": 2, "name": "Bok choy", "quantity": 3, "why": "greens for every stir fry"},
        ],
        "estimated_total": 1.00,
        "swap_suggestion": None,
        "meals": [
            {"name": "Red-braised pork", "servings": 6, "uses": [1], "from_home": ["Rice", "Soy sauce"],
             "prep_time": "90 minutes", "instructions": "Braise the pork and serve over rice."},
            {"name": "Pork and bok choy stir fry", "servings": 8, "uses": [1, 2], "from_home": ["Rice"],
             "prep_time": "20 minutes", "instructions": "Stir fry sliced pork with bok choy."},
        ],
    }
    plan = finalize_plan(raw, DEALS, PROFILE)

    pork, bok = plan["basket"]
    assert (pork["name"], pork["price"], pork["quantity"], pork["subtotal"]) == ("Fresh Pork Shoulder Blade Roast", 8.80, 2, 17.60)
    assert (bok["price"], bok["subtotal"], bok["group"]) == (1.99, 5.97, "Produce")
    assert pork["group"] == "Proteins" and pork["deal_name"] == "Fresh Pork Shoulder Blade Roast"
    assert plan["estimated_total"] == 23.57
    assert plan["servings_total"] == 14 and plan["servings_target"] == 21
    assert finalize_plan(raw, DEALS, {**PROFILE, "include_breakfast": False})["servings_target"] == 14
    assert plan["meals"][1]["uses"] == ["Fresh Pork Shoulder Blade Roast", "Bok Choy"]   # IDs become readable names
    assert plan["meals"][0]["from_home"] == ["Rice", "Soy sauce"]


def test_invented_or_broken_basket_lines_are_dropped_and_duplicates_merged(caplog):
    raw = {"basket": [
        {"id": 99, "name": "Wagyu", "quantity": 1},          # not on the flyer
        {"id": "x", "name": "???", "quantity": 1},
        {"id": 3, "name": "Tofu", "quantity": 0},            # nothing to buy
        {"id": 2, "name": "Bok choy", "quantity": 1},
        {"id": 2, "name": "Bok choy", "quantity": 2},
    ], "meals": [{"name": "Greens", "servings": "4", "uses": [2, 99]}]}
    with caplog.at_level(logging.WARNING, logger="budgetplate.budget"):
        plan = finalize_plan(raw, DEALS, PROFILE)
    assert [(i["name"], i["quantity"], i["subtotal"]) for i in plan["basket"]] == [("Bok Choy", 3, 5.97)]
    assert plan["meals"][0] == {"name": "Greens", "meal": "main", "servings": 4, "uses": ["Bok Choy"], "from_home": [],
                                "appliance": None, "prep_time": "", "instructions": ""}
    assert "dropped 3 basket line(s)" in caplog.text


def test_items_no_meal_uses_are_reported_in_the_log(caplog):
    raw = {"basket": [{"id": 2, "name": "Bok choy", "quantity": 1}, {"id": 3, "name": "Tofu", "quantity": 1}],
           "meals": [{"name": "Greens", "servings": 2, "uses": [2]}]}
    with caplog.at_level(logging.WARNING, logger="budgetplate.budget"):
        finalize_plan(raw, DEALS, PROFILE)
    assert "1 bought item(s) not used by any meal: ['Sunrise Tofu']" in caplog.text


def test_garbage_from_the_model_becomes_an_empty_plan():
    assert finalize_plan(["not", "a", "plan"], DEALS, {"people": 3})["servings_target"] == 63
    assert finalize_plan({"basket": "nope", "meals": None}, DEALS, PROFILE)["basket"] == []


def test_route_returns_the_python_priced_plan(claude, monkeypatch):
    import json

    import main

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [dict(d) for d in DEALS])
    monkeypatch.setattr(routes.budget, "select_candidates", lambda deals, cuisines, **kw: deals)
    claude.response = fake_response(text=json.dumps({
        "basket": [{"id": 1, "name": "Pork shoulder", "quantity": 15, "why": "protein"}],
        "meals": [{"name": "Braise", "servings": 14, "uses": [1]}]}))

    body = TestClient(main.app).post("/api/budget-plan", json={
        "postal_code": "N6G 2J8", "merchant": "FreshCo", "budget": 120, "people": 1}).json()

    # 15 x $8.80 = $132 is over the $120 budget, so Python buys fewer: 13 x $8.80.
    assert body["estimated_total"] == 114.4 and body["under_budget"] is True
    assert body["plan"]["basket"][0]["subtotal"] == 114.4 and body["plan"]["basket"][0]["quantity"] == 13
    assert body["plan"]["servings_total"] == 14


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


def test_route_logs_one_timing_line_with_stages_and_tokens(claude, monkeypatch, caplog):
    import main

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [
        {"clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "category": "Produce"}])

    with caplog.at_level(logging.INFO, logger="budgetplate.budget"):
        res = TestClient(main.app).post("/api/budget-plan", json={
            "postal_code": "M5S 1Z9", "merchant": "No Frills", "budget": 40, "people": 1})

    assert res.status_code == 200
    (line,) = [r.getMessage() for r in caplog.records if "budget_plan timing" in r.getMessage()]
    for part in ("fresh_check_ms=", "get_items_ms=", "filter_ms=", "claude_ms=", "total_ms=",
                 "deals_sent=1", "input_tokens=3754", "output_tokens=900", "outcome=ok"):
        assert part in line
    assert "pipeline_ms=" not in line  # data was fresh, so no scrape ran


# --- kitchen appliances -----------------------------------------------------------

def test_prompt_limits_meals_to_the_shoppers_appliances():
    prompt = build_budget_prompt({**PROFILE, "appliances": ["Microwave", "Air fryer"]}, DEALS)
    assert "Kitchen appliances (the ONLY ways they can cook): Microwave, Air fryer" in prompt
    assert "ONLY the shopper's kitchen\n   appliances: Microwave, Air fryer." in prompt
    assert '"appliance": "<the ONE appliance' in prompt
    # No selection given (e.g. an older client): stovetop and oven.
    assert "the ONLY ways they can cook): Stovetop, Oven" in build_budget_prompt(PROFILE, DEALS)


def test_each_meal_reports_its_appliance_and_off_list_ones_are_blanked(caplog):
    raw = {"basket": [{"id": 2, "name": "Bok choy", "quantity": 1}],
           "meals": [{"name": "Steamed greens", "servings": 2, "uses": [2], "appliance": "microwave"},
                     {"name": "Roast greens", "servings": 2, "uses": [2], "appliance": "Oven"}]}
    with caplog.at_level(logging.WARNING, logger="budgetplate.budget"):
        plan = finalize_plan(raw, DEALS, {**PROFILE, "appliances": ["Microwave"]})
    assert [m["appliance"] for m in plan["meals"]] == ["Microwave", None]
    assert "Roast greens: Oven" in caplog.text


def test_route_accepts_only_known_appliances_and_needs_at_least_one(claude, monkeypatch):
    import main

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [dict(d) for d in DEALS])
    client = TestClient(main.app)
    base = {"postal_code": "N6G 2J8", "merchant": "FreshCo", "budget": 60, "people": 1}

    assert client.post("/api/budget-plan", json={**base, "appliances": []}).status_code == 422
    assert client.post("/api/budget-plan", json={**base, "appliances": ["Campfire"]}).status_code == 422
    assert client.post("/api/budget-plan", json={**base, "appliances": ["Air fryer"]}).status_code == 200
    assert "the ONLY ways they can cook): Air fryer" in claude.requests[-1]["messages"][0]["content"]
    assert client.post("/api/budget-plan", json=base).status_code == 200   # default applies
    assert "the ONLY ways they can cook): Stovetop, Oven" in claude.requests[-1]["messages"][0]["content"]


def test_breakfasts_are_labelled_and_the_route_takes_the_toggle(claude, monkeypatch):
    import json

    import main

    raw = {"basket": [{"id": 2, "name": "Bok choy", "quantity": 1}],
           "meals": [{"name": "Congee", "meal": "Breakfast", "servings": 7, "uses": [2]},
                     {"name": "Stir fry", "meal": "main", "servings": 14, "uses": [2]}]}
    assert [m["meal"] for m in finalize_plan(raw, DEALS, PROFILE)["meals"]] == ["breakfast", "main"]
    off = finalize_plan(raw, DEALS, {**PROFILE, "include_breakfast": False})
    assert [m["meal"] for m in off["meals"]] == ["main", "main"] and off["include_breakfast"] is False

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [dict(d) for d in DEALS])
    claude.response = fake_response(text=json.dumps(raw))
    client = TestClient(main.app)
    base = {"postal_code": "N6G 2J8", "merchant": "FreshCo", "budget": 60}
    assert client.post("/api/budget-plan", json=base).json()["plan"]["servings_target"] == 21   # on by default
    body = client.post("/api/budget-plan", json={**base, "include_breakfast": False}).json()
    assert body["plan"]["servings_target"] == 14
    assert any("Plan ONE week of lunches and dinners for this shopper" in r["messages"][0]["content"]
               for r in claude.requests)


def test_prompt_demands_real_recipes():
    prompt = build_budget_prompt(PROFILE, DEALS)
    assert "5. REAL RECIPES ONLY" in prompt
    assert "recognizable dish" in prompt and "ingredients that\n   dish actually needs" in prompt
    assert "leave it out of the\n   basket instead of forcing it into a meal" in prompt


def test_prompt_says_pantry_items_are_available_not_required():
    prompt = build_budget_prompt(PROFILE, DEALS)
    assert "ALREADY HAS AT HOME (using them is optional)" in prompt
    assert "AVAILABLE, not what must be used" in prompt
    assert "ingredients only when the dish genuinely calls for it" in prompt


def test_route_returns_a_cuisine_note_when_few_deals_fit(claude, monkeypatch):
    import main

    monkeypatch.setattr(routes.budget, "has_fresh_data", lambda pc: True)
    monkeypatch.setattr(routes.budget, "get_items", lambda pc, merchant=None: [dict(d) for d in DEALS])
    body = TestClient(main.app).post("/api/budget-plan", json={
        "postal_code": "N6G 2J8", "merchant": "FreshCo", "budget": 60, "cuisines": ["Mexican"]}).json()
    assert body["cuisine_note"].startswith("Not many Mexican staples on sale at FreshCo")


def test_every_basket_item_gets_a_size_when_the_flyer_has_one():
    from ai.budget import pack_size

    assert pack_size({"clean_name": "Baby Carrots", "weight_kg": 0.34}) == "340 g"
    assert pack_size({"clean_name": "Aman's Lentils", "weight_kg": 1.81}) == "1.81 kg"
    assert pack_size({"clean_name": "Large Eggs 12 Pk"}) == "12 pack"
    assert pack_size({"clean_name": "Natrel Fine Filtered Milk 2 L Skim, 1 or 2%", "weight_kg": 2.0}) == "2 L"
    assert pack_size({"clean_name": "Kikkoman Soy Sauce 591 Ml", "weight_kg": 0.591}) == "591 mL"
    assert pack_size({"clean_name": "Bok Choy"}) is None                       # UI shows "1 pack"
    assert pack_size({"clean_name": "Pork Roast", "priced_per": "lb", "price_per_kg": 4.41,
                      "weight_kg": 2.0}) is None                               # bought by the kg
    plan = finalize_plan({"basket": [{"id": 2, "quantity": 1}]}, DEALS, PROFILE)
    assert "size" in plan["basket"][0]


def test_groups_come_from_python_not_the_model():
    from ai.budget import group_for

    def group(name, category="Other"):
        return group_for({"clean_name": name, "category": category})

    assert group("White Whole Mushrooms 227 G") == "Produce"            # parser says Other
    assert group("Handi Ginger & Garlic Paste", "Produce") == "Grains & Pantry"
    assert group("Three Crabs Fish Sauce", "Meat & Seafood") == "Grains & Pantry"
    assert group("Fresh Pork Shoulder", "Meat & Seafood") == "Proteins"
    assert group("Bok Choy", "Produce") == "Produce"
    assert group("Tilda Basmati Rice", "Grains & Bread") == "Grains & Pantry"
    assert group("Large White Eggs", "Dairy & Eggs") == "Dairy & Eggs"
    assert group("Sunrise Tofu") == "Proteins"                          # Other: keyword fallback
    assert group("Frozen Perogies", "Frozen") == "Other"

    # The model isn't asked for a group, and one it sends anyway is ignored.
    assert '"group"' not in build_budget_prompt(PROFILE, DEALS)
    plan = finalize_plan({"basket": [{"id": 2, "quantity": 1, "group": "Proteins"}]}, DEALS, PROFILE)
    assert plan["basket"][0]["group"] == "Produce"


def test_the_model_is_not_asked_for_a_reason_and_one_it_sends_is_dropped():
    plan = finalize_plan({"basket": [{"id": 1, "quantity": 1, "why": "classic Chinese cut"}]}, DEALS, PROFILE)
    assert "why" not in plan["basket"][0]
    assert plan["basket"][0]["source_name"] == "Fresh Pork Shoulder Blade Roast"


# --- recovering JSON from a messy reply -----------------------------------------

def test_the_last_complete_json_object_is_used_when_the_model_answers_twice():
    from ai.client import _extract_json

    first = '{"basket": [{"id": 1, "quantity": 9}], "meals": []}'
    second = '{"basket": [{"id": 1, "quantity": 2}], "meals": [{"name": "Braise", "uses": [1]}]}'
    reply = f"```json\n{first}\n```\n\nWait, I need to recalculate. The total is over budget.\n\n```json\n{second}\n```"
    assert _extract_json(reply)["basket"][0]["quantity"] == 2

    # A second answer that was cut off: fall back to the last complete one.
    assert _extract_json(first + "\n\nLet me fix that:\n" + second[:40])["basket"][0]["quantity"] == 9
    # Prose around a single answer, braces inside strings, and plain JSON still work.
    assert _extract_json('Here you go: {"basket": [], "note": "use {curly} braces"} Enjoy!')["note"] == "use {curly} braces"
    assert _extract_json(first) == {"basket": [{"id": 1, "quantity": 9}], "meals": []}
    assert _extract_json("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        _extract_json("no json here {oops")


def test_a_reply_with_two_answers_becomes_a_plan_not_a_502(claude):
    first = '{"basket": [{"id": 2, "quantity": 9}], "meals": []}'
    second = '{"basket": [{"id": 2, "quantity": 1}], "meals": [{"name": "Greens", "servings": 2, "uses": [2]}]}'
    claude.response = fake_response(text=f"{first}\n\nWait, I need to recalculate.\n\n{second}")
    plan = generate_budget_plan(PROFILE, DEALS)
    assert plan["basket"][0]["quantity"] == 1 and plan["meals"][0]["name"] == "Greens"


def test_basket_names_come_from_the_deal_never_from_the_model():
    from ai.budget import display_name

    deals = DEALS + [
        {"clean_name": "Gray Ridge Premium Large Eggs 18 Pk", "price": 5.47, "price_per_kg": None,
         "category": "Dairy & Eggs"},
        {"clean_name": "Indian Eggplant Product of Usa or Mexico 3.28/kg - Freshco Diwali", "price": 1.49,
         "price_per_kg": 3.28, "category": "Produce", "priced_per": "lb"},
    ]
    raw = {"basket": [{"id": len(DEALS) + 1, "name": "Eggs (best estimate: using bread for breakfast)", "quantity": 1},
                      {"id": len(DEALS) + 2, "name": "Chinese eggplant", "quantity": 0.5},
                      {"id": 2, "quantity": 1}],
           "meals": [{"name": "Egg and eggplant stir fry", "servings": 4, "uses": [len(DEALS) + 1, len(DEALS) + 2, 2]}]}
    plan = finalize_plan(raw, deals, PROFILE)

    assert [i["name"] for i in plan["basket"]] == ["Gray Ridge Premium Large Eggs 18 Pk", "Indian Eggplant", "Bok Choy"]
    assert plan["meals"][0]["uses"] == ["Gray Ridge Premium Large Eggs 18 Pk", "Indian Eggplant", "Bok Choy"]
    assert "best estimate" not in str(plan) and "Chinese eggplant" not in str(plan)
    assert '"name": "<short everyday name' not in build_budget_prompt(PROFILE, deals)   # no longer asked for

    # The display name is the clean_name minus the origin and per-kg text flyers append.
    assert display_name({"clean_name": "Pork Picnic Roast Bone-in, Fresh, Cryovac 4.41/kg"}) == "Pork Picnic Roast Bone-in, Fresh, Cryovac"
    assert display_name({"clean_name": "Bananas Product of Ecuador"}) == "Bananas"
    assert display_name({"clean_name": "Bok Choy"}) == "Bok Choy"
