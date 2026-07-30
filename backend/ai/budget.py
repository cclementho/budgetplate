"""Weekly basket generation using Claude Haiku.

Given the deals on sale at the shopper's chosen store this week plus their
preferences (budget, household size, cuisine preferences, dietary restriction),
Haiku assembles a practical weekly basket that stays within budget and leans
into the cuisines the shopper actually cooks — then suggests a few meals in that
same style.
"""

from ai.client import call_claude_json

# Basket groups used by the printable shopping list (Section 4).
GROUPS = ["Proteins", "Produce", "Grains & Pantry", "Other"]

SYSTEM_PROMPT = (
    "You are a friendly, practical grocery-budgeting assistant for Canadian "
    "shoppers on a tight budget — especially students without cars. You build a "
    "weekly basket from what's actually on sale this week, matched to the "
    "cuisines the shopper cooks. You talk like a helpful friend, not a "
    "nutritionist. You ONLY ever respond with valid JSON in the exact schema "
    "requested."
)

PROMPT_TEMPLATE = """Build ONE weekly grocery basket for this shopper.

SHOPPER
- Weekly budget: ${budget:.2f}  (try hard to stay AT or UNDER this)
- People to feed: {people}
- Cuisine preferences: {cuisines}
- Dietary restriction: {restriction}
- ALREADY HAS AT HOME (do not buy these): {pantry}
- Store: {merchant}

DEALS ON SALE THIS WEEK AT {merchant} (each line is "NAME | $PRICE | $PER_KG | CATEGORY"):
{deals}

Return ONLY valid JSON in exactly this schema:
{{
  "basket": [
    {{
      "name": "<clean product name>",
      "price": <the store sale price for one unit/pack, a number>,
      "suggested_quantity": "<e.g. 1kg, 1 dozen, 2 bags>",
      "subtotal": <price * quantity for this line, a number>,
      "why": "<ONE short friendly sentence, e.g. 'cheapest protein this week' or 'pairs with the pork for a stir fry'>",
      "group": "<one of: Proteins / Produce / Grains & Pantry / Other>"
    }}
  ],
  "estimated_total": <sum of all subtotals, a number>,
  "swap_suggestion": <null, OR a string like "Swap X for Y and save $Z to get under budget.">,
  "meals": [
    {{
      "name": "<meal name>",
      "uses": ["<basket item>", "<basket item>"],
      "prep_time": "<realistic, e.g. 20 minutes>",
      "instructions": "<ONE simple sentence>"
    }}
  ]
}}

HARD RULES — never break these
1. FOOD ONLY. Every item must be something you EAT as a meal or ingredient.
   No household products, no cleaning supplies, no paper products, no foil,
   no parchment paper, no dish soap, no pet food — nothing you don't eat.
2. DO NOT pad the basket to hit the budget. If the good food deals only add
   up to $40 on a $60 budget, return $40 worth of food. Never add random
   snacks, drinks, or condiments just to spend more. Underspending is a WIN,
   not a problem.
3. Condiments and drinks are ONLY allowed when a specific meal in "meals"
   directly needs them (e.g. soy sauce for the stir fry). Never as filler.
4. Every basket item must play a clear role in at least one meal in "meals".
   If an item doesn't appear in any meal's "uses", cut it from the basket.
5. NEVER put anything the shopper ALREADY HAS AT HOME in the basket — they
   own it, so buying it again wastes their budget. You MAY still cook with
   those ingredients: list them in a meal's "uses" freely. The rule is
   "don't buy it", not "don't use it". (Rule 4 still applies to everything
   you DO buy.)

OTHER RULES
- Only choose items from the deals list above, using their real prices.
- PRIORITISE ingredients common to the shopper's cuisines when several deals
  compete. A Chinese preference should favour pork belly, tofu, bok choy, rice,
  soy-friendly veg over ground beef and cheddar; adapt to whatever cuisines are
  listed. If cuisines are empty or include "Whatever is cheapest", just build
  the cheapest sensible basket.
- Cover reasonable variety: at least one protein, one carb/grain, and some
  vegetables. Scale quantities to the number of people.
- Respect the dietary restriction strictly (e.g. Halal = no pork or alcohol,
  Gluten-free = avoid wheat/barley, Vegetarian/Vegan = no meat/seafood, Vegan
  also no dairy or eggs).
- Keep each "why" to ONE short sentence in a warm, plain, friend-to-friend voice.
- "group" MUST be exactly one of: Proteins / Produce / Grains & Pantry / Other.
- If the cheapest sensible basket goes OVER budget, still return it but set
  "swap_suggestion" to a concrete swap that gets it under budget. If it's within
  budget, set "swap_suggestion" to null.
- Give 3-4 meals that genuinely fit the shopper's cuisine (a Korean preference
  gets bibimbap / doenjang jjigae, NOT pasta), each using items from the basket.
  Meals may also lean on what the shopper already has at home — that's free
  food, so use it.
"""


def _fmt(value) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def generate_budget_plan(profile: dict, deals: list[dict]) -> dict:
    """Generate the weekly basket JSON from a profile and the store's deals.

    ``profile`` keys: budget, people, cuisines (list), restriction (str|None),
    pantry (list), merchant. ``deals`` are cleaned item rows for the chosen
    store (already pantry-filtered by the caller).
    """
    cuisines = profile.get("cuisines") or []
    cuisines_text = ", ".join(cuisines) if cuisines else "None specified"
    restriction = profile.get("restriction") or "No restriction"
    pantry = profile.get("pantry") or []
    pantry_text = ", ".join(pantry) if pantry else "Nothing — assume an empty kitchen"

    deal_lines = [
        f"{d.get('clean_name', '')} | ${_fmt(d.get('price'))} | "
        f"${_fmt(d.get('price_per_kg'))}/kg | {d.get('category', '')}"
        for d in deals
    ]

    prompt = PROMPT_TEMPLATE.format(
        budget=float(profile.get("budget", 60)),
        people=profile.get("people", 1),
        cuisines=cuisines_text,
        restriction=restriction,
        pantry=pantry_text,
        merchant=profile.get("merchant", "the store"),
        deals="\n".join(deal_lines),
    )

    return call_claude_json(prompt, system=SYSTEM_PROMPT, max_tokens=4096)
