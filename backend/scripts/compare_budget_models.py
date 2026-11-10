"""Compare two Claude models on the budget-plan feature, using frozen real flyer data.

Test harness only. Every request is built by the production prompt code
(ai.budget.build_budget_prompt, SYSTEM_PROMPT) and sent the way production's
call_claude_json sends it; arms differ only in model, max_tokens and effort.

Usage (from backend/):
    # Compare a candidate model with whatever production uses today
    # (config.CLAUDE_MODEL + ai.budget.MAX_TOKENS). Each run gets its own folder
    # under scripts/budget_eval/runs/ with summary.md, results.csv, violations.csv.
    python scripts/compare_budget_models.py compare --candidate claude-sonnet-5-5 \
        --candidate-price 2.00 10.00 --price-source <url> --dry-run --offline
    python scripts/compare_budget_models.py compare --candidate ... --candidate-price IN OUT \
        [--candidate-max-tokens N] [--candidate-effort low] [--cases PATH] [--max-cost 1.00]
    python scripts/compare_budget_models.py compare --rescore scripts/budget_eval/runs/<run>

    # Refresh the test cases from the latest flyer batch (never overwrites the
    # frozen set unless --force; default output is cases_<flyer week>.json)
    python scripts/compare_budget_models.py build-cases [--out PATH] [--force]

    # The original fixed arms (the Haiku 4.5 vs 5.5 study)
    python scripts/compare_budget_models.py estimate|run [--arms ...]
    python scripts/compare_budget_models.py report        # re-score saved responses, no API calls
"""

import argparse
import csv
import datetime as dt
import difflib
import glob
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic  # noqa: E402

from ai.budget import MAX_TOKENS as PRODUCTION_MAX_TOKENS  # noqa: E402
from ai.budget import SYSTEM_PROMPT, build_budget_prompt  # noqa: E402
from ai.client import _extract_json  # noqa: E402
from config import settings  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(__file__), "budget_eval")
CASES_FILE = os.path.join(OUT_DIR, "cases.json")
RESPONSES_FILE = os.path.join(OUT_DIR, "responses.jsonl")
RESULTS_CSV = os.path.join(OUT_DIR, "results.csv")
VIOLATIONS_CSV = os.path.join(OUT_DIR, "violations.csv")
RUNS_DIR = os.path.join(OUT_DIR, "runs")

# Offline estimates (no API call) assume this many characters per token. Measured
# on the frozen prompts: Haiku 4.5 2.29-2.70, Haiku 5.5 1.77-2.04. 1.5 is
# deliberately pessimistic so an unknown tokenizer can't make the estimate too low.
OFFLINE_CHARS_PER_TOKEN = 1.5

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


def build_cases(out_path: str | None = None, force: bool = False) -> str:
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
    if out_path is None:
        out_path = os.path.join(OUT_DIR, f"cases_{batch['max_flyer_week']}.json")
    if os.path.exists(out_path) and not force:
        raise SystemExit(f"{out_path} already exists; pass --force to overwrite it "
                         "(the frozen set keeps results comparable across runs).")

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

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
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
    print(f"Wrote {out_path} (flyer week {batch['max_flyer_week']}, batch {batch['batch_id']})")
    return out_path


def load_cases(path: str = CASES_FILE) -> tuple[dict, list[dict]]:
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found. The frozen cases are kept out of git (they're a flyer "
            "snapshot), so restore your local copy or build new ones with build-cases."
        )
    with open(path) as f:
        data = json.load(f)
    by_id = {m: {d["id"]: d for d in deals} for m, deals in data["stores"].items()}
    for c in data["cases"]:
        store = by_id[c["profile"]["merchant"]]
        c["deals"] = [store[i] for i in c["deal_ids_in_prompt"]]
        c["prompt"] = build_budget_prompt(c["profile"], c["deals"])
        c["prompt_sha"] = hashlib.sha256((SYSTEM_PROMPT + c["prompt"]).encode()).hexdigest()[:16]
    return data, data["cases"]


# --- 2. Cost estimate --------------------------------------------------------

def _recorded_input_tokens(cases: list[dict], legacy_ok: bool) -> dict:
    """Input tokens already measured by earlier runs, keyed by (case_id, model).

    Only reuses a count when the prompt is byte-identical (same prompt_sha).
    Responses saved before prompt_sha existed are trusted only for the frozen set.
    """
    shas = {c["case_id"]: c["prompt_sha"] for c in cases}
    found = {}
    for path in [RESPONSES_FILE, *glob.glob(os.path.join(RUNS_DIR, "*", "responses.jsonl"))]:
        if not os.path.exists(path):
            continue
        for line in open(path):
            r = json.loads(line)
            same = r.get("prompt_sha") == shas.get(r["case_id"]) if "prompt_sha" in r else legacy_ok
            if same and r.get("input_tokens"):
                found[r["case_id"], r["model"]] = r["input_tokens"]
    return found


def estimate(client, cases: list[dict], arms: list[str], *, offline: bool = False,
             legacy_ok: bool = True) -> float:
    """Price the run before spending anything; output is priced at each arm's cap.

    Online: exact input tokens from count_tokens (free, but it is an API call).
    Offline: no network at all - reuse token counts measured by earlier runs on the
    same prompts, otherwise assume OFFLINE_CHARS_PER_TOKEN.
    """
    models = sorted({ARMS[a]["model"] for a in arms})
    counts, source = {}, {}
    recorded = _recorded_input_tokens(cases, legacy_ok) if offline else {}
    for c in cases:
        for m in models:
            if not offline:
                counts[c["case_id"], m] = client.messages.count_tokens(
                    model=m, system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": c["prompt"]}],
                ).input_tokens
                source[m] = "count_tokens (exact)"
            elif (c["case_id"], m) in recorded:
                counts[c["case_id"], m] = recorded[c["case_id"], m]
                source.setdefault(m, "measured in earlier runs")
            else:
                chars = len(SYSTEM_PROMPT) + len(c["prompt"])
                counts[c["case_id"], m] = math.ceil(chars / OFFLINE_CHARS_PER_TOKEN)
                source[m] = f"approximate ({OFFLINE_CHARS_PER_TOKEN} chars/token, pessimistic)"
    worst = 0.0
    for a in arms:
        cfg = ARMS[a]
        n_in = sum(counts[c["case_id"], cfg["model"]] for c in cases)
        arm_worst = sum(price_for(cfg["model"], counts[c["case_id"], cfg["model"]], cfg["max_tokens"])
                        for c in cases)
        worst += arm_worst
        print(f"{a:<26} {cfg['model']:<20} input {n_in:>7,} tokens [{source[cfg['model']]}]; "
              f"output at most {cfg['max_tokens'] * len(cases):>7,} -> worst case ${arm_worst:.4f}")
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


def run(client: anthropic.Anthropic, cases: list[dict], arms: list[str],
        responses_file: str = RESPONSES_FILE) -> None:
    """Run the given arms; responses from other arms already on disk are kept."""
    kept = []
    if os.path.exists(responses_file):
        kept = [line for line in open(responses_file) if arm_of(json.loads(line)) not in arms]
    with open(responses_file, "w") as out:
        out.writelines(kept)
        for i, c in enumerate(cases):
            # Alternate the order so no arm always gets the warm connection.
            order = arms if i % 2 == 0 else list(reversed(arms))
            for a in order:
                r = call(client, a, c["prompt"])
                r.update(case_id=c["case_id"], model=ARMS[a]["model"], arm=a, prompt_sha=c["prompt_sha"])
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


def report(data: dict, cases: list[dict], out_dir: str = OUT_DIR,
           responses_file: str = RESPONSES_FILE) -> tuple[list[dict], list[dict]]:
    by_case = {c["case_id"]: c for c in cases}
    responses = [json.loads(line) for line in open(responses_file)]
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
    results_csv = os.path.join(out_dir, "results.csv")
    violations_csv = os.path.join(out_dir, "violations.csv")
    with open(results_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    with open(violations_csv, "w", newline="") as f:
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
    print(f"\nWrote {results_csv} and {violations_csv}")
    return rows, vrows


# --- 6. compare: candidate vs production ------------------------------------

def summarize_arm(rows: list[dict], vrows: list[dict], arm: str) -> dict:
    mr = [r for r in rows if r["arm"] == arm]
    ok = [r for r in mr if r["valid_output"]]
    costs = [r["cost_usd"] for r in mr if r["cost_usd"] is not None]
    lat = [r["latency_s"] for r in mr if not r["error"]]
    lat_ok = [r["latency_s"] for r in ok]
    return {
        "runs": len(mr), "valid": len(ok),
        "avg_cost": sum(costs) / len(costs) if costs else None,
        "total_cost": sum(costs),
        "cost_per_valid": sum(costs) / len(ok) if ok else None,
        "avg_latency": sum(lat) / len(lat) if lat else None,
        "avg_latency_valid": sum(lat_ok) / len(lat_ok) if lat_ok else None,
        "max_latency": max(lat) if lat else None,
        "avg_in": sum(r["input_tokens"] or 0 for r in mr) / len(mr),
        "avg_out": sum(r["output_tokens"] or 0 for r in mr) / len(mr),
        "stop_reasons": dict(Counter(r["stop_reason"] or "error" for r in mr)),
        "violations": Counter(x["type"] for x in vrows if x["arm"] == arm and x["kind"] == "violation"),
        "flags": Counter(x["type"] for x in vrows if x["arm"] == arm and x["kind"] == "review"),
        "spend_share": (sum(r["basket_total"] / r["budget"] for r in ok) / len(ok)) if ok else None,
        "errors": [r["error"] for r in mr if r["error"]],
    }


def _money(x, digits=4):
    return "n/a" if x is None else f"${x:.{digits}f}"


def _secs(x):
    return "n/a" if x is None else f"{x:.1f} s"


def verdict(prod: dict, cand: dict) -> list[str]:
    """Mechanical checks only; the written recommendation is a judgement on top."""
    out = []
    pv, cv = sum(prod["violations"].values()), sum(cand["violations"].values())
    out.append(("PASS" if cand["valid"] >= prod["valid"] else "FAIL")
               + f" reliability: candidate {cand['valid']}/{cand['runs']} valid vs production {prod['valid']}/{prod['runs']}")
    out.append(("PASS" if cv <= pv else "FAIL")
               + f" accuracy: candidate {cv} rule violation(s) vs production {pv}")
    if prod["cost_per_valid"] and cand["cost_per_valid"]:
        ratio = cand["cost_per_valid"] / prod["cost_per_valid"]
        out.append(f"INFO cost per valid plan: candidate {_money(cand['cost_per_valid'])} vs "
                   f"production {_money(prod['cost_per_valid'])} ({ratio:.2f}x)")
    if prod["avg_latency"] is not None and cand["avg_latency"] is not None:
        out.append(f"INFO latency: candidate avg {_secs(cand['avg_latency'])} (max {_secs(cand['max_latency'])}) "
                   f"vs production avg {_secs(prod['avg_latency'])} (max {_secs(prod['max_latency'])})")
    return out


def write_summary(run_dir: str, meta: dict, rows: list[dict], vrows: list[dict]) -> str:
    prod, cand = summarize_arm(rows, vrows, "production"), summarize_arm(rows, vrows, "candidate")
    A = {a: ARMS[a] for a in ("production", "candidate")}
    lines = [
        f"# Budget-plan model comparison: {A['candidate']['model']} vs {A['production']['model']}",
        "",
        f"- Date: {meta['date']}  |  cases: `{meta['cases_file']}` (flyer week {meta['flyer_week']}, "
        f"{meta['n_cases']} cases, postal code {meta['postal_code']})",
        f"- Prices verified from {meta['price_source']} on {meta['price_verified']}",
        f"- Each case ran once per setup, so run-to-run variation is not measured.",
        "",
        "| Setup | Model | Output limit | Effort | Input $/MTok | Output $/MTok |",
        "|---|---|---|---|---|---|",
    ]
    for name, cfg in A.items():
        tiers = PRICES[cfg["model"]]
        lines.append(f"| {name} | `{cfg['model']}` | {cfg['max_tokens']:,} | {cfg['effort'] or 'default'} "
                     f"| ${tiers[0][1]:.2f} | ${tiers[0][2]:.2f} |")
    lines += ["", "## Results", "",
              "| | production | candidate |", "|---|---|---|"]

    def row(label, f):
        lines.append(f"| {label} | {f(prod)} | {f(cand)} |")

    row("Valid plans", lambda x: f"{x['valid']}/{x['runs']}")
    row("Stop reasons", lambda x: ", ".join(f"{k} {v}" for k, v in x["stop_reasons"].items()))
    row("Avg cost per call", lambda x: _money(x["avg_cost"], 5))
    row("Cost per valid plan", lambda x: _money(x["cost_per_valid"], 5))
    row("Total spent", lambda x: _money(x["total_cost"], 4))
    row("Avg latency (all / valid only)", lambda x: f"{_secs(x['avg_latency'])} / {_secs(x['avg_latency_valid'])}")
    row("Slowest call", lambda x: _secs(x["max_latency"]))
    row("Avg tokens in / out (out includes thinking)", lambda x: f"{x['avg_in']:,.0f} / {x['avg_out']:,.0f}")
    row("Rule violations", lambda x: str(sum(x["violations"].values())))
    row("Avg spend vs budget (valid plans)", lambda x: "n/a" if x["spend_share"] is None else f"{x['spend_share']:.0%}")
    lines += ["", "### Violations by type", "", "| Type | production | candidate |", "|---|---|---|"]
    for t in VIOLATION_TYPES:
        lines.append(f"| {t} | {prod['violations'].get(t, 0)} | {cand['violations'].get(t, 0)} |")
    lines += ["", "### Every violation", ""]
    vio = [x for x in vrows if x["kind"] == "violation"]
    lines += [f"- **{x['arm']}**, {x['case_id']}, `{x['type']}`: {x['detail']}" for x in vio] or ["- none"]
    lines += ["", "### Review flags (not violations; check by hand)", ""]
    for arm, summ in (("production", prod), ("candidate", cand)):
        lines.append(f"- {arm}: " + (", ".join(f"{k} {v}" for k, v in summ["flags"].items()) or "none"))
    worth = [x for x in vrows if x["kind"] == "review"
             and x["type"] not in ("item_name_reworded", "meal_uses_pantry_item")]
    lines += [f"  - {x['arm']}, {x['case_id']}, `{x['type']}`: {x['detail']}" for x in worth]
    if prod["errors"] or cand["errors"]:
        lines += ["", "### API errors", ""] + [f"- {e}" for e in prod["errors"] + cand["errors"]]
    lines += ["", "## Mechanical verdict", ""] + [f"- {v}" for v in verdict(prod, cand)]
    lines += ["", "## Recommendation", "", "_To be written after reading the results above._", ""]
    path = os.path.join(run_dir, "summary.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def _slug(model: str, max_tokens: int, effort: str | None) -> str:
    return "_".join(x for x in [model, f"{max_tokens // 1000}k" if max_tokens % 1000 == 0 else str(max_tokens),
                                f"effort-{effort}" if effort else ""] if x)


def compare(args) -> int:
    for model, price, tier in ((args.candidate, args.candidate_price, args.candidate_price_over),
                               (settings.CLAUDE_MODEL, args.production_price, args.production_price_over)):
        if price:
            tiers = [(int(tier[0]), float(tier[1]), float(tier[2]))] if tier else []
            PRICES[model] = ([(tiers[0][0], *map(float, price))] + [(None, tiers[0][1], tiers[0][2])]
                             if tiers else [(None, *map(float, price))])
        elif model not in PRICES:
            raise SystemExit(f"No verified price for {model}: pass its official per-MTok prices "
                             "(--candidate-price / --production-price IN OUT) and --price-source.")
    if (args.candidate_price or args.production_price) and not args.price_source:
        raise SystemExit("--price-source is required when passing prices (the official pricing page URL).")

    ARMS["production"] = {"model": settings.CLAUDE_MODEL, "max_tokens": PRODUCTION_MAX_TOKENS, "effort": None}
    ARMS["candidate"] = {"model": args.candidate,
                         "max_tokens": args.candidate_max_tokens or PRODUCTION_MAX_TOKENS,
                         "effort": args.candidate_effort}
    arms = ["production", "candidate"]
    data, cases = load_cases(args.cases)
    if args.limit:
        cases = cases[:args.limit]
    print(f"cases: {args.cases} (flyer week {data['flyer_week']}, {len(cases)} cases)")
    for a in arms:
        print(f"{a}: {ARMS[a]}  prices {PRICES[ARMS[a]['model']]}")

    client = None if args.offline else anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=2)
    worst = estimate(client, cases, arms, offline=args.offline,
                     legacy_ok=os.path.abspath(args.cases) == os.path.abspath(CASES_FILE))
    if worst > args.max_cost:
        print(f"STOP: worst-case cost ${worst:.2f} is over the ${args.max_cost:.2f} limit. "
              "Lower --candidate-max-tokens, use --limit, or raise --max-cost only with approval.")
        return 2
    if args.dry_run:
        print(f"DRY RUN: within the ${args.max_cost:.2f} limit; no model calls made.")
        return 0

    today = dt.date.today().isoformat()
    run_dir = os.path.join(RUNS_DIR, f"{today}_{_slug(args.candidate, ARMS['candidate']['max_tokens'], args.candidate_effort)}")
    os.makedirs(run_dir, exist_ok=True)
    meta = {"date": today, "cases_file": os.path.relpath(args.cases, os.path.dirname(__file__)),
            "flyer_week": data["flyer_week"], "n_cases": len(cases), "postal_code": data["postal_code"],
            "price_source": args.price_source or "the PRICES table in compare_budget_models.py",
            "price_verified": args.price_verified or today, "arms": {a: ARMS[a] for a in arms},
            "prices": {ARMS[a]["model"]: PRICES[ARMS[a]["model"]] for a in arms},
            "worst_case_usd": worst}
    with open(os.path.join(run_dir, "run.json"), "w") as f:
        json.dump(meta, f, indent=2)
    responses = os.path.join(run_dir, "responses.jsonl")
    run(client, cases, arms, responses_file=responses)
    rows, vrows = report(data, cases, out_dir=run_dir, responses_file=responses)
    print(f"Wrote {write_summary(run_dir, meta, rows, vrows)}")
    return 0


def rescore(run_dir: str) -> int:
    """Re-apply the rule checks to a saved compare run (no API calls)."""
    meta = json.load(open(os.path.join(run_dir, "run.json")))
    ARMS.update(meta["arms"])
    for model, tiers in meta["prices"].items():
        PRICES[model] = [tuple(t) for t in tiers]
    cases_path = os.path.join(os.path.dirname(__file__), meta["cases_file"])
    data, cases = load_cases(cases_path)
    rows, vrows = report(data, cases[:meta["n_cases"]], out_dir=run_dir,
                         responses_file=os.path.join(run_dir, "responses.jsonl"))
    print(f"Wrote {write_summary(run_dir, meta, rows, vrows)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["compare", "build-cases", "estimate", "run", "report"])
    ap.add_argument("--arms", nargs="+", choices=list(ARMS), default=list(ARMS),
                    help="estimate/run/report: which fixed setups (default: all)")
    g = ap.add_argument_group("compare")
    g.add_argument("--candidate", help="candidate model ID, e.g. claude-sonnet-5-5")
    g.add_argument("--candidate-max-tokens", type=int, help=f"default: production's ({PRODUCTION_MAX_TOKENS})")
    g.add_argument("--candidate-effort", choices=["low", "medium", "high", "xhigh", "max"],
                   help="default: none sent (the model's default effort)")
    g.add_argument("--candidate-price", nargs=2, metavar=("IN", "OUT"), help="$ per million tokens, verified")
    g.add_argument("--candidate-price-over", nargs=3, metavar=("TOKENS", "IN", "OUT"),
                   help="higher rate card for prompts over TOKENS input tokens, if the model has one")
    g.add_argument("--production-price", nargs=2, metavar=("IN", "OUT"),
                   help="only if production's model isn't in PRICES or its price changed")
    g.add_argument("--production-price-over", nargs=3, metavar=("TOKENS", "IN", "OUT"))
    g.add_argument("--price-source", help="official pricing page URL the prices came from")
    g.add_argument("--price-verified", help="date the prices were checked (default: today)")
    g.add_argument("--cases", default=CASES_FILE, help="test cases (default: the frozen set)")
    g.add_argument("--limit", type=int, help="only the first N cases")
    g.add_argument("--max-cost", type=float, default=COST_CEILING_USD, help="stop if the worst case is above this")
    g.add_argument("--dry-run", action="store_true", help="estimate only; never call the model")
    g.add_argument("--offline", action="store_true", help="estimate without any API call (implies --dry-run)")
    g.add_argument("--rescore", metavar="RUN_DIR", help="re-apply the rule checks to a saved run; no API calls")
    b = ap.add_argument_group("build-cases")
    b.add_argument("--out", help="where to write new cases (default: budget_eval/cases_<flyer week>.json)")
    b.add_argument("--force", action="store_true", help="allow overwriting an existing cases file")
    args = ap.parse_args()

    if args.command == "compare" and args.rescore:
        return rescore(args.rescore)
    if args.command == "compare":
        if not args.candidate:
            ap.error("compare needs --candidate")
        if args.offline:
            args.dry_run = True
        return compare(args)

    if args.command == "build-cases":
        build_cases(args.out, args.force)
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
