"""Compare two Claude models on the budget-plan feature, using frozen real flyer data.

Test harness only. Every request is built by the production prompt code
(ai.budget.build_budget_prompt, SYSTEM_PROMPT) and sent the way production's
call_claude_json sends it; arms differ only in model, max_tokens and effort.

Usage (from backend/):
    python scripts/compare_budget_models.py build-cases   # freeze 10 cases from the local warehouse
    python scripts/compare_budget_models.py estimate      # count tokens (free) and price the run
    python scripts/compare_budget_models.py run           # estimate, then call every arm (stops if > $1)
    python scripts/compare_budget_models.py run --arms haiku-5.5-low-effort haiku-5.5-16k
    python scripts/compare_budget_models.py report        # re-score saved responses, no API calls

Outputs land in scripts/budget_eval/.
"""

import argparse
import csv
import difflib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic  # noqa: E402

from ai.budget import SYSTEM_PROMPT, build_budget_prompt  # noqa: E402
from ai.client import _extract_json  # noqa: E402
from config import settings  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "budget_eval")
CASES_FILE = os.path.join(OUT_DIR, "cases.json")
RESPONSES_FILE = os.path.join(OUT_DIR, "responses.jsonl")
RESULTS_CSV = os.path.join(OUT_DIR, "results.csv")
VIOLATIONS_CSV = os.path.join(OUT_DIR, "violations.csv")

# Each arm is one request setup, pinned to the values that were tested.
# "haiku-4.5" is what production sent before the switch (4,096-token cap);
# "haiku-5.5-16k" matches production since then (config.CLAUDE_MODEL, ai.budget.MAX_TOKENS).
ARMS = {
    "haiku-4.5": {"model": "claude-haiku-4-5", "max_tokens": 4096, "effort": None},
    "haiku-5.5": {"model": "claude-haiku-5-5", "max_tokens": 4096, "effort": None},
    "haiku-5.5-low-effort": {"model": "claude-haiku-5-5", "max_tokens": 4096, "effort": "low"},
    "haiku-5.5-16k": {"model": "claude-haiku-5-5", "max_tokens": 16_000, "effort": None},
}
# Responses saved before arms existed only recorded the model.
_LEGACY_ARM = {"claude-haiku-4-5": "haiku-4.5", "claude-haiku-5-5": "haiku-5.5"}


def arm_of(resp: dict) -> str:
    return resp.get("arm") or _LEGACY_ARM[resp["model"]]

# Verified 2026-10-19 from https://platform.claude.com/docs/en/about-claude/pricing.md
# ($ per million tokens, standard first-party API). Haiku 5.5 has a second rate card
# for prompts over 100,000 tokens.
PRICES = {
    "claude-haiku-4-5": [(None, 1.00, 5.00)],
    "claude-haiku-5-5": [(100_000, 0.10, 0.50), (None, 0.50, 2.50)],
}
COST_CEILING_USD = 1.00

POSTAL_CODE = "M5S1Z9"


def price_for(model: str, input_tokens: int, output_tokens: int) -> float:
    for limit, in_rate, out_rate in PRICES[model]:
        if limit is None or input_tokens <= limit:
            return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    raise ValueError(model)


# --- 1. Test cases -----------------------------------------------------------

# Chains x budgets x household sizes x restrictions, 4 with pantry items. Pantry
# items come from the app's own "already at home" staples (Home.jsx) and were
# chosen because each store has matching deals, so the pantry rule is exercised.
CASE_SPECS = [
    ("No Frills", 40, 1, None, []),
    ("No Frills", 75, 2, "Vegetarian", ["Rice", "Bread", "Cooking oil"]),
    ("No Frills", 120, 4, "No dairy", []),
    ("FreshCo", 40, 2, "Vegetarian", []),
    ("FreshCo", 120, 4, None, ["Rice", "Eggs", "Onions", "Garlic"]),
    ("Walmart", 75, 1, "No dairy", ["Pasta", "Cooking oil"]),
    ("Walmart", 120, 4, "Vegetarian", []),
    ("Walmart", 40, 2, None, []),
    ("Loblaws", 75, 4, None, ["Eggs", "Milk", "Bread", "Butter"]),
    ("Loblaws", 40, 1, "No dairy", []),
]


def build_cases() -> None:
    """Freeze each store's deals from the latest successful batch, as of its scrape time.

    The live query (queries.get_items) only returns deals valid *now*; this batch's
    flyer has ended, so it would return nothing. Instead we take the same columns
    from the flyer_items view, filtered to deals that were valid when the batch
    was scraped - exactly what the app showed that day.
    """
    import psycopg2
    import psycopg2.extras

    from routes.budget import _filter_deals

    conn = psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://localhost/budgetplate"))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT batch_id, finished_at, max_flyer_week FROM etl_batch
        WHERE postal_code = %s AND status IN ('success', 'success_with_warnings')
          AND loaded_row_count > 0
        ORDER BY max_flyer_week DESC, finished_at DESC, batch_id DESC LIMIT 1
        """,
        (POSTAL_CODE,),
    )
    batch = cur.fetchone()

    stores = {}
    for merchant in sorted({spec[0] for spec in CASE_SPECS}):
        cur.execute(
            """
            SELECT id, clean_name, original_name, category, price, price_per_kg,
                   price_per_unit, weight_kg
            FROM flyer_items
            WHERE postal_code = %s AND merchant = %s
              AND (valid_to IS NULL OR valid_to >= scraped_at)
            ORDER BY clean_name
            """,
            (POSTAL_CODE, merchant),
        )
        stores[merchant] = [
            {k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in row.items()}
            for row in cur.fetchall()
        ]

    cases = []
    for i, (merchant, budget, people, restriction, pantry) in enumerate(CASE_SPECS, 1):
        kept, skipped = _filter_deals(stores[merchant], restriction, pantry)
        cases.append({
            "case_id": f"case{i:02d}",
            "profile": {"budget": budget, "people": people, "cuisines": [],
                        "restriction": restriction, "pantry": pantry, "merchant": merchant},
            "deal_ids_in_prompt": [d["id"] for d in kept],
            "skipped_pantry_items": skipped,
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CASES_FILE, "w") as f:
        json.dump({
            "postal_code": POSTAL_CODE,
            "source_batch_id": batch["batch_id"],
            "flyer_week": str(batch["max_flyer_week"]),
            "deals_as_of": str(batch["finished_at"]),
            "note": "stores = full deal list per store (valid at scrape time); "
                    "deal_ids_in_prompt = what the /api/budget-plan route sends after "
                    "removing Household, restriction-blocked categories and pantry matches.",
            "stores": stores,
            "cases": cases,
        }, f, indent=2, default=str)
    for c in cases:
        p = c["profile"]
        print(f"{c['case_id']}: {p['merchant']:<9} ${p['budget']:<3} x{p['people']} "
              f"{p['restriction'] or 'none':<10} pantry={p['pantry']} "
              f"-> {len(c['deal_ids_in_prompt'])} deals in prompt")
    print(f"Wrote {CASES_FILE}")


def load_cases() -> tuple[dict, list[dict]]:
    with open(CASES_FILE) as f:
        data = json.load(f)
    by_id = {m: {d["id"]: d for d in deals} for m, deals in data["stores"].items()}
    for c in data["cases"]:
        store = by_id[c["profile"]["merchant"]]
        c["deals"] = [store[i] for i in c["deal_ids_in_prompt"]]
        c["prompt"] = build_budget_prompt(c["profile"], c["deals"])
    return data, data["cases"]


# --- 2. Cost estimate --------------------------------------------------------

def estimate(client: anthropic.Anthropic, cases: list[dict], arms: list[str]) -> float:
    """Exact input tokens per model (count_tokens is free); output priced at each arm's cap."""
    models = sorted({ARMS[a]["model"] for a in arms})
    counts = {}
    for c in cases:
        for m in models:
            counts[c["case_id"], m] = client.messages.count_tokens(
                model=m, system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": c["prompt"]}],
            ).input_tokens
    worst = 0.0
    for a in arms:
        cfg = ARMS[a]
        n_in = sum(counts[c["case_id"], cfg["model"]] for c in cases)
        arm_worst = sum(price_for(cfg["model"], counts[c["case_id"], cfg["model"]], cfg["max_tokens"]) for c in cases)
        worst += arm_worst
        print(f"{a:<22} input {n_in:>7,} tokens; output at most {cfg['max_tokens'] * len(cases):>7,} "
              f"-> worst case ${arm_worst:.4f}")
    print(f"Worst case for this run (every response hits its max_tokens): ${worst:.4f}")
    return worst


# --- 3. Running the arms ------------------------------------------------------

def call(client: anthropic.Anthropic, arm: str, prompt: str) -> dict:
    """Send what production's call_claude_json sends, with the arm's model, cap and effort."""
    cfg = ARMS[arm]
    kwargs = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    if cfg["effort"]:
        # anthropic==0.42 predates the output_config argument; extra_body sends
        # the same field in the request body.
        kwargs["extra_body"] = {"output_config": {"effort": cfg["effort"]}}
    started = time.perf_counter()
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.APIError as e:
        return {"error": f"{type(e).__name__}: {e}", "latency_s": time.perf_counter() - started}
    latency = time.perf_counter() - started
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {
        "latency_s": latency,
        "stop_reason": resp.stop_reason,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "thinking_blocks": sum(1 for b in resp.content if b.type == "thinking"),
        "text": text,
        "resolved_model": resp.model,
    }


def run(client: anthropic.Anthropic, cases: list[dict], arms: list[str]) -> None:
    """Run the given arms; responses from other arms already on disk are kept."""
    kept = []
    if os.path.exists(RESPONSES_FILE):
        kept = [line for line in open(RESPONSES_FILE) if arm_of(json.loads(line)) not in arms]
    with open(RESPONSES_FILE, "w") as out:
        out.writelines(kept)
        for i, c in enumerate(cases):
            # Alternate the order so no arm always gets the warm connection.
            order = arms if i % 2 == 0 else list(reversed(arms))
            for a in order:
                r = call(client, a, c["prompt"])
                r.update(case_id=c["case_id"], model=ARMS[a]["model"], arm=a)
                out.write(json.dumps(r) + "\n")
                out.flush()
                print(f"{c['case_id']} {a}: {r.get('stop_reason') or r.get('error')} "
                      f"in {r['latency_s']:.1f}s, out={r.get('output_tokens')}")


# --- 4. Rule checks -----------------------------------------------------------

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(s).lower())).strip()


_STOPWORDS = {"or", "and", "the", "with", "of", "product", "pk", "pack", "bag", "lb", "kg", "ml"}


def tokens(s: str) -> set[str]:
    """Meaningful words of a normalized name (drops sizes, units and filler)."""
    return {w for w in norm(s).split() if len(w) > 2 and not w.isdigit() and w not in _STOPWORDS}


def wordre(terms) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.I)


NON_FOOD = wordre([
    r"soap", r"detergent", r"cleaner", r"bleach", r"tissues?", r"toilet", r"paper towels?",
    r"napkins?", r"foil", r"plastic wrap", r"parchment", r"garbage", r"trash bags?",
    r"diapers?", r"wipes", r"shampoo", r"conditioner", r"toothpaste", r"deodorant",
    r"lotion", r"razors?", r"batter(?:y|ies)", r"light bulbs?", r"candles?",
    r"dog", r"cat food", r"pet", r"litter", r"plants?", r"flowers?", r"bouquet",
    r"gift cards?", r"vitamins?", r"supplements?", r"plush", r"toys?", r"costume",
])
NON_FOOD_UNSURE = wordre([r"halloween", r"decor\w*", r"pumpkin carving", r"gum"])

MEAT = wordre([
    r"chicken", r"beef", r"pork", r"bacon", r"ham", r"sausages?", r"turkey", r"lamb",
    r"veal", r"steaks?", r"ribs?", r"brisket", r"wings?", r"drumsticks?", r"thighs?",
    r"meatballs?", r"pepperoni", r"salami", r"prosciutto", r"chorizo", r"hot dogs?",
    r"wieners?", r"deli meat", r"fish", r"salmon", r"tuna", r"cod", r"tilapia",
    r"haddock", r"shrimp", r"prawns?", r"crab", r"lobster", r"scallops?", r"mussels?",
    r"clams?", r"anchov(?:y|ies)", r"sardines?", r"pollock", r"basa", r"trout",
    r"ground (?:beef|pork|turkey|chicken)", r"lard", r"gelatin",
])
MEAT_UNSURE = wordre([
    r"broth", r"stock", r"soup", r"dumplings?", r"gyoza", r"wontons?", r"pizza",
    r"lasagna", r"ramen", r"instant noodles?", r"marshmallows?", r"worcestershire",
    r"caesar", r"oyster sauce", r"perogies|pierogi(?:es)?", r"frozen (?:meal|dinner)s?",
    r"jerky", r"gravy", r"burgers?", r"patties", r"nuggets?",
])
DAIRY = wordre([
    r"milk", r"cheese", r"cheddar", r"mozzarella", r"parmesan", r"feta", r"ricotta",
    r"brie", r"gouda", r"havarti", r"paneer", r"butter", r"cream", r"yogh?urt",
    r"ghee", r"whey", r"casein", r"kefir", r"ice cream", r"sour cream", r"cottage",
    r"cream cheese", r"custard", r"gelato", r"milkshake", r"creamer",
])
# Plant-based look-alikes that are not dairy.
DAIRY_OK = wordre([
    r"(?:coconut|almond|oat|soy|rice|cashew) (?:milk|beverage|cream|yogh?urt|cheese)",
    r"peanut butter", r"almond butter", r"apple butter", r"cocoa butter", r"butternut",
    r"butter (?:beans?|lettuce|chicken)", r"buttermilk biscuits? mix", r"vegan",
    r"dairy[ -]free", r"plant[- ]based",
])
DAIRY_UNSURE = wordre([
    r"bread", r"naan", r"buns?", r"cookies?", r"cakes?", r"muffins?", r"croissants?",
    r"pancakes?", r"waffles?", r"chocolate", r"margarine", r"pizza", r"granola",
    r"crackers?", r"pastr(?:y|ies)", r"pies?", r"pasta sauce", r"alfredo", r"ranch",
    r"perogies|pierogi(?:es)?", r"mac(?:aroni)? (?:and|&) cheese", r"lasagna", r"pudding",
])


def match_deal(item: dict, store_deals: list[dict]) -> tuple[dict | None, str]:
    """Find the flyer deal a basket line refers to.

    Returns (deal, how): how is 'exact' (same name and price), 'reworded' (name
    differs slightly, price matches - flagged for review, not a violation),
    'price_mismatch' (name found, price isn't one the flyer lists) or 'missing'.
    """
    name = norm(item.get("name", ""))
    try:
        price = round(float(item.get("price")), 2)
    except (TypeError, ValueError):
        price = None
    same_name = [d for d in store_deals if norm(d["clean_name"]) == name]
    for d in same_name:
        if price is not None and abs(round(d["price"], 2) - price) < 0.005:
            return d, "exact"
    if same_name:
        return same_name[0], "price_mismatch"

    # Flyer names often bundle products ("Iögo Creamy 16x100 G or Siggi's Skyr
    # Yogurt") and models shorten them to the one they picked ("Iögo Creamy
    # Yogurt"), so compare words: most of the basket name's words must appear
    # in the deal's name.
    words = tokens(name)

    def close(d):
        dn = norm(d["clean_name"])
        if not dn or not name:
            return False
        if dn in name or name in dn or difflib.SequenceMatcher(None, dn, name).ratio() >= 0.85:
            return True
        return bool(words) and len(words & tokens(dn)) / len(words) >= 0.6

    # Closest listing first: several deals can share a price ($2.49 bread vs
    # $2.49 English muffins), so the first one that matches isn't necessarily the right one.
    near = sorted(
        (d for d in store_deals if close(d)),
        key=lambda d: (len(words & tokens(d["clean_name"])) / max(len(words), 1),
                       difflib.SequenceMatcher(None, norm(d["clean_name"]), name).ratio()),
        reverse=True,
    )
    for d in near:
        if price is not None and abs(round(d["price"], 2) - price) < 0.005:
            return d, "reworded"
    if near:
        return near[0], "price_mismatch"
    return None, "missing"


def schema_ok(plan) -> bool:
    if not isinstance(plan, dict):
        return False
    basket, meals = plan.get("basket"), plan.get("meals")
    if not isinstance(basket, list) or not basket or not isinstance(meals, list):
        return False
    for it in basket:
        if not isinstance(it, dict) or not it.get("name"):
            return False
        if not isinstance(it.get("price"), (int, float)) or not isinstance(it.get("subtotal"), (int, float)):
            return False
    return all(isinstance(m, dict) and isinstance(m.get("uses"), list) for m in meals)


def check(case: dict, store_deals: list[dict], plan: dict) -> tuple[list[dict], list[dict]]:
    """Return (violations, review_flags) for one plan. Nothing is fixed."""
    profile = case["profile"]
    pantry = profile["pantry"]
    restriction = profile["restriction"]
    violations, review = [], []

    def v(kind, detail):
        violations.append({"type": kind, "detail": detail})

    def r(kind, detail):
        review.append({"type": kind, "detail": detail})

    pantry_re = wordre([re.escape(t) for t in pantry]) if pantry else None
    basket = plan.get("basket") or []
    basket_names = [norm(it.get("name", "")) for it in basket]

    # Shopping-list items are real flyer deals at their real prices.
    for it in basket:
        deal, how = match_deal(it, store_deals)
        label = f"{it.get('name')!r} @ ${it.get('price')}"
        if how == "missing":
            v("item_not_on_flyer", f"{label}: no deal with this name at {profile['merchant']}")
        elif how == "price_mismatch":
            v("price_not_on_flyer", f"{label}: flyer lists {deal['clean_name']!r} at ${deal['price']:.2f}")
        elif how == "reworded":
            r("item_name_reworded", f"{label} matched flyer deal {deal['clean_name']!r}")
        it["_deal"] = deal

        # Food only.
        name = it.get("name", "")
        if (deal and deal.get("category") == "Household") or NON_FOOD.search(name):
            v("non_food_item", f"{label} (flyer category: {deal.get('category') if deal else 'n/a'})")
        elif NON_FOOD_UNSURE.search(name):
            r("maybe_non_food", label)

        # Already at home.
        if pantry_re and pantry_re.search(name):
            v("pantry_item_in_basket", f"{label} matches 'already at home' {pantry}")

    # Recipe ingredients come from the list (or from home, which the app's own rule 5 allows).
    for meal in plan.get("meals") or []:
        for use in meal.get("uses") or []:
            u = norm(use)
            in_basket = any(u and (u == b or u in b or b in u) for b in basket_names if b)
            if in_basket:
                continue
            if pantry_re and pantry_re.search(use):
                r("meal_uses_pantry_item", f"{meal.get('name')!r} uses {use!r} from home (allowed by the prompt's rule 5)")
                continue
            v("meal_ingredient_not_in_basket", f"{meal.get('name')!r} uses {use!r}, which is not on the shopping list")

    # Within budget, using the route's own total (sum of line subtotals).
    total = round(sum(it["subtotal"] for it in basket if isinstance(it.get("subtotal"), (int, float))), 2)
    if total > profile["budget"] + 0.005:
        v("over_budget", f"total ${total:.2f} > budget ${profile['budget']} "
                         f"(swap_suggestion: {plan.get('swap_suggestion')!r})")
    stated = plan.get("estimated_total")
    if isinstance(stated, (int, float)) and abs(stated - total) > 0.01:
        r("stated_total_differs", f"model said ${stated:.2f}, line subtotals sum to ${total:.2f}")
    for it in basket:
        if isinstance(it.get("subtotal"), (int, float)) and isinstance(it.get("price"), (int, float)) \
                and it["subtotal"] + 0.005 < it["price"]:
            r("subtotal_below_unit_price", f"{it.get('name')!r}: subtotal ${it['subtotal']} < price ${it['price']}")

    # Dietary restriction: basket lines and every meal ingredient/name.
    if restriction in ("Vegetarian", "No dairy"):
        texts = [("basket", it.get("name", ""), (it.get("_deal") or {}).get("category")) for it in basket]
        for meal in plan.get("meals") or []:
            texts.append(("meal", meal.get("name", ""), None))
            texts += [("meal ingredient", u, None) for u in meal.get("uses") or []]
        for where, text, category in texts:
            label = f"{where} {text!r}"
            if restriction == "Vegetarian":
                if MEAT.search(text) or category == "Meat & Seafood":
                    v("not_vegetarian", label)
                elif MEAT_UNSURE.search(text):
                    r("maybe_not_vegetarian", label)
            else:
                if DAIRY_OK.search(text):
                    continue
                if DAIRY.search(text) or (category == "Dairy & Eggs" and not re.search(r"\beggs?\b", text, re.I)):
                    v("contains_dairy", label)
                elif DAIRY_UNSURE.search(text):
                    r("maybe_contains_dairy", label)

    for it in basket:
        it.pop("_deal", None)
    return violations, review


# --- 5. Scoring and report ------------------------------------------------------

VIOLATION_TYPES = [
    "item_not_on_flyer", "price_not_on_flyer", "meal_ingredient_not_in_basket",
    "pantry_item_in_basket", "over_budget", "non_food_item", "not_vegetarian", "contains_dairy",
]


def report(data: dict, cases: list[dict]) -> None:
    by_case = {c["case_id"]: c for c in cases}
    responses = [json.loads(line) for line in open(RESPONSES_FILE)]
    rows, vrows = [], []
    for resp in responses:
        c = by_case[resp["case_id"]]
        p = c["profile"]
        row = {
            "case_id": c["case_id"], "arm": arm_of(resp), "model": resp["model"],
            "max_tokens": ARMS[arm_of(resp)]["max_tokens"], "effort": ARMS[arm_of(resp)]["effort"] or "default",
            "merchant": p["merchant"],
            "budget": p["budget"], "people": p["people"], "restriction": p["restriction"] or "none",
            "pantry": "; ".join(p["pantry"]), "deals_in_prompt": len(c["deals"]),
            "input_tokens": resp.get("input_tokens"), "output_tokens": resp.get("output_tokens"),
            "cost_usd": None, "latency_s": round(resp["latency_s"], 2),
            "stop_reason": resp.get("stop_reason"), "thinking_blocks": resp.get("thinking_blocks"),
            "parsed_json": False, "valid_output": False, "basket_items": None, "meals": None,
            "basket_total": None, "violations": None, "review_flags": None, "error": resp.get("error"),
        }
        if resp.get("input_tokens") is not None:
            row["cost_usd"] = round(price_for(resp["model"], resp["input_tokens"], resp["output_tokens"]), 6)
        plan = None
        if not resp.get("error"):
            try:
                plan = _extract_json(resp["text"])
                row["parsed_json"] = True
            except (ValueError, json.JSONDecodeError):
                pass
        if row["parsed_json"] and schema_ok(plan) and resp.get("stop_reason") == "end_turn":
            row["valid_output"] = True
            viol, review = check(c, data["stores"][p["merchant"]], plan)
            row.update(
                basket_items=len(plan["basket"]), meals=len(plan["meals"]),
                basket_total=round(sum(i["subtotal"] for i in plan["basket"]), 2),
                violations=len(viol), review_flags=len(review),
            )
            for t in VIOLATION_TYPES:
                row[f"v_{t}"] = sum(1 for x in viol if x["type"] == t)
            for x in viol:
                vrows.append({"case_id": c["case_id"], "arm": arm_of(resp), "kind": "violation", **x})
            for x in review:
                vrows.append({"case_id": c["case_id"], "arm": arm_of(resp), "kind": "review", **x})
        rows.append(row)

    fields = list(rows[0].keys()) + [f"v_{t}" for t in VIOLATION_TYPES if f"v_{t}" not in rows[0]]
    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(VIOLATIONS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "arm", "kind", "type", "detail"])
        w.writeheader()
        w.writerows(vrows)

    print("\n== Summary ==")
    for m in [a for a in ARMS if any(r["arm"] == a for r in rows)]:
        mr = [r for r in rows if r["arm"] == m]
        ok = [r for r in mr if r["valid_output"]]
        costs = [r["cost_usd"] for r in mr if r["cost_usd"] is not None]
        lat = [r["latency_s"] for r in mr if not r["error"]]
        counts = Counter(x["type"] for x in vrows if x["arm"] == m and x["kind"] == "violation")
        flags = Counter(x["type"] for x in vrows if x["arm"] == m and x["kind"] == "review")
        print(f"\n{m}: {len(ok)}/{len(mr)} valid "
              f"| avg cost ${sum(costs) / len(costs):.5f} (total ${sum(costs):.4f}) "
              f"| avg latency {sum(lat) / len(lat):.1f}s (min {min(lat):.1f}, max {max(lat):.1f}) "
              f"| avg tokens in {sum(r['input_tokens'] or 0 for r in mr) / len(mr):,.0f} "
              f"out {sum(r['output_tokens'] or 0 for r in mr) / len(mr):,.0f}")
        print("  stop reasons:", dict(Counter(r["stop_reason"] or r["error"] for r in mr)))
        print("  violations:", dict(counts) or "none", "| plans with >=1 violation:",
              sum(1 for r in ok if r["violations"]))
        print("  review flags:", dict(flags) or "none")
    print(f"\nWrote {RESULTS_CSV} and {VIOLATIONS_CSV}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build-cases", "estimate", "run", "report"])
    ap.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS),
                    help="which setups to estimate/run (default: all)")
    args = ap.parse_args()

    if args.command == "build-cases":
        build_cases()
        return 0

    data, cases = load_cases()
    if args.command == "report":
        report(data, cases)
        return 0

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=2)
    worst = estimate(client, cases, args.arms)
    if args.command == "estimate":
        return 0
    if worst > COST_CEILING_USD:
        print(f"Worst-case cost ${worst:.2f} is over ${COST_CEILING_USD:.2f}; not running.")
        return 2
    run(client, cases, args.arms)
    report(data, cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
