"""Weekly meal plan and basket generation using Claude Haiku.

Given a shortlist of the deals on sale at the shopper's store this week plus
their preferences (budget, household size, cuisines, dietary restriction, what
is already at home), Haiku plans a week of meals (lunch and dinner, plus
breakfast unless the shopper turns it off) and the basket that makes them.

The model only chooses: which deals (by ID), how many, and which meals. Names
aside, everything that involves money is done here in Python from the flyer
data - prices, subtotals and the total - so the model can't invent a price or
get the arithmetic wrong.
"""

import json
import logging
import re

from ai.candidates import _words, bucket_for
from ai.client import call_claude_json, stream_claude_json
from config import settings

logger = logging.getLogger("budgetplate.budget")

# Basket groups used by the shopping list, in display order.
GROUPS = ["Proteins", "Produce", "Grains & Pantry", "Dairy & Eggs", "Other"]
_BUCKET_GROUP = {
    "proteins": "Proteins",
    "produce": "Produce",
    "grains_pantry": "Grains & Pantry",
    "dairy_eggs": "Dairy & Eggs",
}
_CATEGORY_GROUP = {
    "Meat & Seafood": "Proteins",
    "Produce": "Produce",
    "Grains & Bread": "Grains & Pantry",
    "Dairy & Eggs": "Dairy & Eggs",
}
# A jar, tin or packet made from an ingredient is a pantry item whatever the
# ingredient is: ginger-garlic paste is not produce and fish sauce is not fish.
_PANTRY_FORM = _words(
    "paste", "sauce", "powder", "starch", "seasoning", "spice", "masala", "bouillon", "broth",
    "stock", "soup", "vinegar", "oil", "dressing", "mayonnaise", "ketchup", "mustard", "salsa",
    "canned", "pickle", "pickled", "marinated", "dried", "mix", "kit",
)
# Fresh produce the parser doesn't recognise and files under "Other".
_FRESH_PRODUCE = _words("mushroom")


def group_for(deal: dict) -> str:
    """The shopping-list group for a deal, decided in Python (never by the model).

    The parser's category is the starting point, with two corrections: jars
    and packets go to Grains & Pantry, and fresh produce the parser misses
    (mushrooms) goes to Produce. Deals the parser left in "Other" fall back to
    the shortlist's keyword buckets.
    """
    name = deal.get("clean_name") or ""
    if _PANTRY_FORM.search(name):
        return "Grains & Pantry"
    if _FRESH_PRODUCE.search(name):
        return "Produce"
    category = deal.get("category")
    if category in _CATEGORY_GROUP:
        return _CATEGORY_GROUP[category]
    return _BUCKET_GROUP.get(bucket_for(deal), "Other")

# Kitchen appliances a shopper can say they have (the frontend offers exactly
# these). Most kitchens have the first two, so they are the default.
APPLIANCES = ["Stovetop", "Oven", "Microwave", "Air fryer", "Rice cooker", "Instant Pot"]
DEFAULT_APPLIANCES = ["Stovetop", "Oven"]

# Lunch and dinner for seven days, plus seven breakfasts when they're wanted.
SERVINGS_PER_PERSON = 14
BREAKFASTS_PER_PERSON = 7

SYSTEM_PROMPT = (
    "You are a friendly, practical grocery-budgeting assistant for Canadian "
    "shoppers on a tight budget, especially students without cars. You plan a "
    "week of meals from what's actually on sale this week, matched to the "
    "cuisines the shopper cooks. You talk like a helpful friend, not a "
    "nutritionist. You ONLY ever respond with valid JSON in the exact schema "
    "requested."
)

PROMPT_TEMPLATE = """Plan ONE week of {meals_phrase} for this shopper, and the grocery basket that makes them.

SHOPPER
- Weekly budget: ${budget:.2f} (never go over it; spending up to it is fine)
- People to feed: {people}
- Cuisine preferences: {cuisines}
- Dietary restriction: {restriction}
- ALREADY HAS AT HOME (using them is optional): {pantry}
- Kitchen appliances (the ONLY ways they can cook): {appliances}
- Store: {merchant}

THE GOAL
- Cover {cover_phrase} for 7 days: {per_person} servings per person, so about {servings_target} servings in total.
- Give {meal_count}. Each meal has "servings": how many portions it makes with what you bought for it.
  The servings of all meals must add up to roughly {servings_target}. Cooking a big batch and eating it again for
  lunch is expected, so one meal can count for 2 to 4 servings per person.
{breakfast_goal}- Use the budget to make the week better and more filling: enough protein, vegetables and a carb in every meal,
  and some variety. A basket that only feeds the shopper for two days is a failure, even if it is cheap.
- Buy quantities that really make the servings. One bell pepper cannot stretch across two dishes for a week;
  if a meal makes 4 servings, buy enough protein and vegetables for 4 portions.

DEALS ON SALE THIS WEEK AT {merchant} (each line is "ID | NAME | $PRICE | $PER_KG | CATEGORY"):
Lines marked "sold by weight" are priced per lb or kg, not per item: you choose how much to buy.
{deals}

Return ONLY valid JSON in exactly this schema:
{{
  "basket": [
    {{
      "id": <the deal's ID number from the list>,
      "name": "<short everyday name for it, e.g. 'Chicken thighs'>",
      "quantity": <how many of this listing to buy, a number. For "sold by weight" deals: the weight to buy in kg, e.g. 1.5>
    }}
  ],
  "swap_suggestion": <null, OR one sentence on what to swap if the basket had to go over budget>,
  "meals": [
    {{
      "name": "<meal name>",
      "meal": "<{meal_kinds}>",
      "servings": <portions this makes, a whole number>,
      "uses": [<IDs of the basket items this meal uses>],
      "from_home": ["<ingredient the shopper already has, if used>"],
      "appliance": "<the ONE appliance from the shopper's list that cooks this meal>",
      "prep_time": "<realistic, e.g. 20 minutes>",
      "instructions": "<ONE simple sentence>"
    }}
  ]
}}

HARD RULES (never break these)
1. NO FILLER. Buy only what the meals need. Every basket item must be used
   by at least one meal: its ID must appear in some meal's "uses". A
   condiment is bought only when a specific meal needs it.
2. Only choose deals from the list above, by ID. One line buys one product.
3. The ALREADY HAS AT HOME list is what is AVAILABLE, not what must be used:
   use one of those ingredients only when the dish genuinely calls for it
   (soy sauce in a stir fry, never in a dal), and it is fine to use none of
   them. List the ones a meal really uses in its "from_home". "from_home" may
   only contain things from that list (salt, pepper and water are always
   fine). Everything else a meal needs must be in the basket.
4. Every meal must be fully cookable with ONLY the shopper's kitchen
   appliances: {appliances}. No step may need anything else. Microwave only
   means no stovetop and no oven steps at all; no oven means no roasting or
   baking. Shop accordingly: don't buy a roast without an oven, or dried
   beans that need hours of simmering without a stovetop or Instant Pot.
   "appliance" must be exactly one name from the shopper's list.
5. REAL RECIPES ONLY. Every meal must be a recognizable dish a normal person
   would cook and could look up by its name, made with the ingredients that
   dish actually needs. The name must match what is in it: no "noodle soup
   with greens" without greens, no curry without curry spices, no "pork
   belly" made from a different cut. If a dish needs an ingredient that is
   not in the basket, not at home and not on the deal list, choose a
   different dish. If a deal doesn't fit a real dish, leave it out of the
   basket instead of forcing it into a meal.

OTHER RULES
- PRIORITISE ingredients common to the shopper's cuisines when several deals
  compete. A Chinese preference should favour pork, tofu, bok choy, rice and
  soy-friendly veg over ground beef and cheddar; adapt to whatever cuisines are
  listed. If cuisines are empty or include "Whatever is cheapest", build the
  best-value sensible week.
- Meals must genuinely fit the shopper's cuisine (a Korean preference gets
  bibimbap or doenjang jjigae, NOT pasta).
- Respect the dietary restriction strictly (e.g. Halal = no pork or alcohol,
  Gluten-free = avoid wheat/barley, Vegetarian/Vegan = no meat/seafood, Vegan
  also no dairy or eggs).
- For "sold by weight" deals, buy a realistic weight for the servings (roughly
  150 to 200 g of meat per serving); the cost is its $/kg price times the kg.
- The basket total is each item's price times its quantity. Keep it at or
  under the budget. Only if a sensible week truly cannot fit, return the
  closest basket and set "swap_suggestion"; otherwise set it to null.
"""


def _fmt(value) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


# Output cap for the plan (4,096 unless CLAUDE_MAX_TOKENS overrides it). Shared
# with scripts/compare_budget_models.py so the comparison sends exactly what
# production sends.
MAX_TOKENS = settings.CLAUDE_MAX_TOKENS


def _wants_breakfast(profile: dict) -> bool:
    """Breakfast is planned unless the shopper turned it off."""
    return profile.get("include_breakfast", True) is not False


def servings_target(people, include_breakfast: bool = True) -> int:
    """Servings for a week for this many people: 14 each, 21 with breakfast."""
    per_person = SERVINGS_PER_PERSON + (BREAKFASTS_PER_PERSON if include_breakfast else 0)
    try:
        return per_person * max(int(people), 1)
    except (TypeError, ValueError):
        return per_person


_BREAKFAST_GOAL = """- Breakfast: 7 of each person's servings are breakfasts ({breakfast_servings} in total). Keep them simple and
  cheap, the kind of thing people really eat on a weekday: eggs, oats, toast, congee, yogurt and fruit. One or
  two breakfasts repeated through the week is normal. Set "meal" to "breakfast" for these and "main" for
  lunches and dinners.
"""


def _appliances(profile: dict) -> list[str]:
    """The shopper's appliances, in a fixed order (default: stovetop and oven)."""
    chosen = set(profile.get("appliances") or DEFAULT_APPLIANCES)
    return [a for a in APPLIANCES if a in chosen] or list(DEFAULT_APPLIANCES)


def _sold_by_weight(deal: dict) -> bool:
    return bool(deal.get("priced_per")) and _number(deal.get("price_per_kg")) is not None


def deal_line(deal_id: int, deal: dict) -> str:
    """One deal as the model sees it: 'ID | NAME | $PRICE | $PER_KG | CATEGORY'.

    A per-weight price is spelled out ("$2.00/lb, sold by weight") so it isn't
    mistaken for the cost of one item.
    """
    if _sold_by_weight(deal):
        return (
            f"{deal_id} | {deal.get('clean_name', '')} | "
            f"${_fmt(deal.get('price'))}/{deal['priced_per']}, sold by weight | "
            f"${_fmt(deal.get('price_per_kg'))}/kg | {deal.get('category', '')}"
        )
    return (
        f"{deal_id} | {deal.get('clean_name', '')} | ${_fmt(deal.get('price'))} | "
        f"${_fmt(deal.get('price_per_kg'))}/kg | {deal.get('category', '')}"
    )


def build_budget_prompt(profile: dict, deals: list[dict]) -> str:
    """Render the user prompt for a profile and the shortlisted deals.

    ``profile`` keys: budget, people, cuisines (list), restriction (str|None),
    pantry (list), appliances (list), merchant. ``deals`` are the candidate rows (already
    pantry-filtered and shortlisted by the caller); a deal's ID is its
    position in this list, starting at 1.
    """
    cuisines = profile.get("cuisines") or []
    cuisines_text = ", ".join(cuisines) if cuisines else "None specified"
    restriction = profile.get("restriction") or "No restriction"
    pantry = profile.get("pantry") or []
    pantry_text = ", ".join(pantry) if pantry else "Nothing. Assume an empty kitchen"
    breakfast = _wants_breakfast(profile)
    people = profile.get("people", 1)

    return PROMPT_TEMPLATE.format(
        budget=float(profile.get("budget", 60)),
        people=people,
        servings_target=servings_target(people, breakfast),
        per_person=servings_target(1, breakfast),
        meals_phrase="breakfasts, lunches and dinners" if breakfast else "lunches and dinners",
        cover_phrase="breakfast, lunch AND dinner" if breakfast else "lunch AND dinner",
        meal_count=(
            "6 to 8 different meals: 1 or 2 breakfasts and 5 or 6 lunch/dinner meals"
            if breakfast else "5 to 7 different meals"
        ),
        breakfast_goal=(
            _BREAKFAST_GOAL.format(breakfast_servings=servings_target(people, True) - servings_target(people, False))
            if breakfast else ""
        ),
        meal_kinds='"breakfast" or "main"' if breakfast else 'always "main"',
        cuisines=cuisines_text,
        restriction=restriction,
        pantry=pantry_text,
        appliances=", ".join(_appliances(profile)),
        merchant=profile.get("merchant", "the store"),
        deals="\n".join(deal_line(i, d) for i, d in enumerate(deals, 1)),
    )


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default  # NaN -> default


_VOLUME_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(ml|l)\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(\d+)\s*(?:pk|pack|ct|count|'s)\b", re.IGNORECASE)


def pack_size(deal: dict) -> str | None:
    """What one unit of a deal is: "340 g", "1.81 kg", "12 pack", or None.

    None means the flyer gives no size (the UI then says "1 pack"), and also
    covers deals sold by weight, where the shopper chooses the amount.
    """
    if _sold_by_weight(deal):
        return None
    name = deal.get("clean_name") or ""
    # Liquids are stored by weight too (2 L of milk as 2 kg); show the volume.
    volume = _VOLUME_RE.search(name)
    if volume:
        return f"{volume.group(1)} {'L' if volume.group(2).lower() == 'l' else 'mL'}"
    weight = _number(deal.get("weight_kg"))
    if weight and weight > 0:
        if weight < 1:
            return f"{round(weight * 1000):g} g"
        return f"{round(weight, 2):g} kg"
    count = _COUNT_RE.search(name)
    return f"{int(count.group(1))} pack" if count and int(count.group(1)) > 1 else None


def finalize_plan(raw, deals: list[dict], profile: dict, quiet: bool = False) -> dict:
    """Turn the model's choices into the plan the API returns.

    Prices come from the flyer (never from the model), and every subtotal and
    the total are computed here. Basket lines whose ID isn't in the deal list
    are dropped, since they can't be priced or bought.

    ``quiet`` skips the cleanup warnings; it is used for the partial plans
    built while a reply is still streaming.

    Returns ``{"basket", "meals", "estimated_total", "servings_total",
    "servings_target", "include_breakfast", "swap_suggestion"}``.
    """
    raw = raw if isinstance(raw, dict) else {}
    lines: dict[int, dict] = {}
    invalid = 0
    for entry in raw.get("basket") or []:
        if not isinstance(entry, dict):
            invalid += 1
            continue
        deal_id = _number(entry.get("id"))
        if deal_id is None or deal_id != int(deal_id) or not 1 <= int(deal_id) <= len(deals):
            invalid += 1
            continue
        deal_id = int(deal_id)
        deal = deals[deal_id - 1]
        by_weight = _sold_by_weight(deal)
        # Sold by weight: the quantity is kilograms, so the price is per kg.
        price = _number(deal.get("price_per_kg") if by_weight else deal.get("price"))
        quantity = _number(entry.get("quantity"), 1.0)
        if price is None or quantity is None or quantity <= 0:
            invalid += 1
            continue
        if deal_id in lines:  # the same deal listed twice: one line, summed
            lines[deal_id]["quantity"] += quantity
            continue
        lines[deal_id] = {
            "deal_id": deal_id,
            "name": str(entry.get("name") or deal.get("clean_name") or "").strip(),
            "deal_name": deal.get("clean_name"),
            # The flyer's own name for the listing this product came from.
            "source_name": deal.get("source_name") or deal.get("clean_name"),
            "price": round(price, 2),
            "quantity": quantity,
            "size": pack_size(deal),  # what one unit is; None if the flyer doesn't say
            "group": group_for(deal),
            # For by-weight items "price" is per kg and "quantity" is kg;
            # "listed_price" is what the flyer shows (e.g. 2.00 per lb).
            "sold_by_weight": by_weight,
            "priced_per": deal.get("priced_per") if by_weight else None,
            "listed_price": _number(deal.get("price")) if by_weight else None,
        }

    basket = []
    for line in lines.values():
        line["quantity"] = round(line["quantity"], 2)
        line["subtotal"] = round(line["price"] * line["quantity"], 2)
        basket.append(line)

    breakfast = _wants_breakfast(profile)
    allowed = {a.lower(): a for a in _appliances(profile)}
    off_list: list[str] = []
    meals = []
    used_ids: set[int] = set()
    for entry in raw.get("meals") or []:
        if not isinstance(entry, dict):
            continue
        uses = []
        for ref in entry.get("uses") or []:
            ref_id = _number(ref)
            if ref_id is not None and int(ref_id) in lines:
                used_ids.add(int(ref_id))
                uses.append(lines[int(ref_id)]["name"])
        from_home = [str(x).strip() for x in entry.get("from_home") or [] if str(x).strip()]
        servings = _number(entry.get("servings"), 0)
        said = str(entry.get("appliance") or "").strip()
        appliance = allowed.get(said.lower())
        if said and appliance is None:
            off_list.append(f"{entry.get('name')}: {said}")
        meals.append({
            "name": str(entry.get("name") or "").strip(),
            "meal": "breakfast" if breakfast and str(entry.get("meal") or "").strip().lower() == "breakfast" else "main",
            "servings": max(int(round(servings)), 0),
            "uses": uses,
            "from_home": from_home,
            "appliance": appliance,  # None if the model named one the shopper lacks
            "prep_time": str(entry.get("prep_time") or "").strip(),
            "instructions": str(entry.get("instructions") or "").strip(),
        })

    unused = [line["name"] for line in basket if line["deal_id"] not in used_ids]
    if (invalid or unused) and not quiet:
        logger.warning(
            "budget plan cleanup: dropped %d basket line(s) with no valid deal ID; "
            "%d bought item(s) not used by any meal: %s", invalid, len(unused), unused,
        )

    if off_list and not quiet:
        logger.warning("budget plan: meals naming an appliance the shopper doesn't have: %s", off_list)

    swap = raw.get("swap_suggestion")
    return {
        "basket": basket,
        "meals": meals,
        "estimated_total": round(sum(line["subtotal"] for line in basket), 2),
        "servings_total": sum(meal["servings"] for meal in meals),
        "servings_target": servings_target(profile.get("people", 1), breakfast),
        "include_breakfast": breakfast,
        "swap_suggestion": swap.strip() if isinstance(swap, str) and swap.strip() else None,
    }


def generate_budget_plan(profile: dict, deals: list[dict], model: str | None = None) -> dict:
    """Ask Claude for the week's plan and return it priced from the flyer.

    ``deals`` is the shortlist the model may choose from. ``model`` overrides
    the configured Claude model (``settings.CLAUDE_MODEL``) for this call only;
    leave it unset in production.
    """
    prompt = build_budget_prompt(profile, deals)
    raw = call_claude_json(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, model=model)
    return finalize_plan(raw, deals, profile)


class _ObjectScanner:
    """Finds finished basket and meal objects in a JSON reply as it streams in.

    The reply is ``{"basket": [{...}, ...], ..., "meals": [{...}, ...]}``, so
    every object that closes directly inside one of the top-level arrays is a
    complete basket line or meal, even though the whole reply isn't valid JSON
    yet. Braces inside strings are ignored.
    """

    def __init__(self):
        self._stack: list[str] = []
        self._in_string = False
        self._escaped = False
        self._current: list[str] | None = None  # text of the object being read

    def feed(self, text: str) -> list[dict]:
        found = []
        for ch in text:
            if self._current is not None:
                self._current.append(ch)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
                continue
            if ch == '"':
                self._in_string = True
            elif ch in "{[":
                if ch == "{" and self._stack == ["{", "["]:
                    self._current = ["{"]
                self._stack.append(ch)
            elif ch in "}]" and self._stack:
                self._stack.pop()
                if ch == "}" and self._stack == ["{", "["] and self._current is not None:
                    try:
                        obj = json.loads("".join(self._current))
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict):
                        found.append(obj)
                    self._current = None
        return found


def stream_budget_plan(profile: dict, deals: list[dict], model: str | None = None):
    """Ask Claude for the plan and yield it piece by piece.

    Yields ``("item", basket line)`` and ``("meal", meal)`` as each one is
    finished, already priced and cleaned by ``finalize_plan``, and finally
    ``("plan", (plan, stats))``: the fully validated plan, which is the source
    of truth and replaces everything shown so far. Raises
    ``ClaudeResponseError`` if the finished reply can't be used.
    """
    prompt = build_budget_prompt(profile, deals)
    scanner = _ObjectScanner()
    basket_so_far: list[dict] = []
    sent_ids: set[int] = set()
    for kind, payload in stream_claude_json(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS, model=model):
        if kind == "done":
            raw, stats = payload
            yield "plan", (finalize_plan(raw, deals, profile), stats)
            return
        for obj in scanner.feed(payload):
            if "uses" in obj or "servings" in obj:
                meals = finalize_plan({"basket": basket_so_far, "meals": [obj]}, deals, profile, quiet=True)["meals"]
                if meals and meals[0]["name"]:
                    yield "meal", meals[0]
            elif "id" in obj:
                lines = finalize_plan({"basket": [obj]}, deals, profile, quiet=True)["basket"]
                if lines and lines[0]["deal_id"] not in sent_ids:
                    sent_ids.add(lines[0]["deal_id"])
                    basket_so_far.append(obj)
                    yield "item", lines[0]
