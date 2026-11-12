"""Checks on a finished meal plan, shared by production and the model eval.

``validate_plan`` is run on every plan before it is returned: it repairs what
can be repaired in Python and lists what can't (``problems``), which the
caller uses to ask the model for one corrected plan. The individual checks
are also used by scripts/compare_budget_models.py to score raw model output,
so the eval measures exactly what production enforces.
"""

import re

# Meal-name words that promise an ingredient, and what counts as having it.
# A name is only checked for the words listed here, so an unusual dish is
# never flagged just because the checker doesn't know it.
_LEAFY = (r"greens?|bok choy|spinach|kale|lettuce|cabbage|gai lan|rapini|chard|saag|methi|arugula|"
          r"romaine|collards?|napa|coleslaw|salad|watercress|yu choy|broccoli")
NAME_INGREDIENTS = [
    ("greens", r"\bgreens\b", _LEAFY),
    ("egg", r"\beggs?\b(?! ?(?:noodles?|plants?|rolls?))|\bomelett?e\b|\bfrittata\b|\bscrambled?\b", r"\beggs?\b"),
    ("chicken", r"\bchicken\b", r"chicken|drumstick|thigh|wings?\b"),
    ("pork", r"\bpork\b", r"pork|picnic|\bham\b|bacon|side ribs|longanisa"),
    ("beef", r"\bbeef\b", r"beef|steak|brisket|\bchuck\b|sirloin|oxtail|\bround\b|rib roast|short rib"),
    ("turkey", r"\bturkey\b", r"turkey"),
    ("tofu", r"\btofu\b", r"tofu|bean curd"),
    ("fish", r"\bfish\b|\bsalmon\b|\btilapia\b|\btuna\b|\bbasa\b|\bcod\b",
     r"fish|salmon|tilapia|tuna|basa|\bcod\b|haddock|trout|sardine|mackerel|herring|pollock|sole"),
    ("shrimp", r"\bshrimps?\b|\bprawns?\b", r"shrimp|prawn"),
    ("sausage", r"\bsausages?\b", r"sausage|wiener|longanisa|chorizo|kolbassa|hot dog|frank"),
    ("bacon", r"\bbacon\b", r"bacon"),
    ("mushroom", r"\bmushrooms?\b", r"mushroom"),
    ("potato", r"\bpotato(?:es)?\b|\bhash ?browns?\b", r"potato|hash ?brown|fries"),
    ("tomato", r"\btomato(?:es)?\b", r"tomato|passata|salsa|marinara|pasta sauce"),
    ("carrot", r"\bcarrots?\b", r"carrot"),
    ("onion", r"\bonions?\b", r"onion|scallion|shallot|leek"),
    ("cabbage", r"\bcabbage\b", r"cabbage|coleslaw|napa"),
    ("spinach", r"\bspinach\b", r"spinach|saag|palak"),
    ("broccoli", r"\bbroccoli\b", r"broccoli"),
    ("cauliflower", r"\bcauliflower\b", r"cauliflower"),
    ("eggplant", r"\beggplants?\b", r"eggplant|aubergine"),
    ("zucchini", r"\bzucchini\b", r"zucchini"),
    ("cucumber", r"\bcucumbers?\b", r"cucumber"),
    ("corn", r"\bcorn\b(?! ?starch)", r"\bcorn\b"),
    ("bell pepper", r"\bbell peppers?\b|\bpeppers\b", r"pepper"),
    ("rice", r"(?<!cauliflower )\brice\b(?! ?(?:noodles?|vinegar|paper))|\bcongee\b", r"\brice\b"),
    ("noodles", r"\bnoodles?\b|\bramen\b|\bchow mein\b|\blo mein\b",
     r"noodle|ramen|udon|chow mein|spaghetti|pasta|vermicelli|bihon|linguine"),
    ("pasta", r"\bpasta\b|\bspaghetti\b|\bpenne\b|\bmacaroni\b|\blasagna\b",
     r"pasta|spaghetti|penne|macaroni|lasagna|noodle|rotini|fusilli|linguine|fettuccine|gnocchi"),
    ("beans", r"\bbeans?\b(?! ?sprouts?)", r"\bbeans?\b|chickpea|lentil"),
    ("lentils", r"\blentils?\b|\bdal\b|\bdaal\b", r"lentil|\bdal\b|daal"),
    ("chickpeas", r"\bchickpeas?\b|\bchana\b", r"chickpea|chana|garbanzo"),
    ("cheese", r"\bcheese\b|\bcheesy\b|\bcheddar\b|\bmozzarella\b|\bpaneer\b",
     r"cheese|cheddar|mozzarella|paneer|feta|parmesan|havarti|gouda|brie|swiss|singles|cheez|velveeta"),
    ("yogurt", r"\byog(?:h)?o?urt\b|\braita\b", r"yog(?:h)?o?urt|dahi|skyr|kefir"),
    ("toast", r"\btoast\b|\bsandwich(?:es)?\b", r"bread|toast|\bbuns?\b|bagel|baguette|naan|pita|sourdough|muffin|loaf|roll|tortilla|wrap"),
    ("oats", r"\boats?\b|\boatmeal\b|\bporridge\b", r"\boats?\b|oatmeal|porridge"),
    ("banana", r"\bbananas?\b", r"banana"),
    ("apple", r"\bapples?\b", r"apple"),
    ("curry", r"\bcurr(?:y|ied|ies)\b",
     r"curry|masala|garam|turmeric|cumin|coriander|korma|tikka|vindaloo|madras|jerk|spice"),
    ("peanut butter", r"\bpeanut butter\b", r"peanut butter"),
    ("milk", r"\bmilk\b(?! ?bread)", r"\bmilk\b"),
]
NAME_INGREDIENTS = [(label, re.compile(said, re.I), re.compile(has, re.I)) for label, said, has in NAME_INGREDIENTS]


# Things every kitchen has; a meal may use them without the shopper listing them.
# Problems a new name settles; anything else needs the plan itself changed.
RENAMEABLE = {"name_promises_missing_ingredient"}

ALWAYS_AT_HOME = re.compile(r"^\s*(?:salt|pepper|black pepper|water|salt and pepper|salt & pepper)\s*$", re.I)


def _norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def _words(text) -> set[str]:
    out = set()
    for word in _norm(text).split():
        if word.endswith("oes"):
            word = word[:-2]
        elif word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
    return out


def in_pantry(item: str, pantry: list[str]) -> bool:
    """True when a "from home" ingredient is something the shopper said they have.

    "cooked rice" and "eggs" match Rice and Eggs; "oil" and "vegetable oil"
    match Cooking oil. Salt, pepper and water always count.
    """
    if ALWAYS_AT_HOME.match(item or ""):
        return True
    words = _words(item)
    if not words:
        return False
    for entry in pantry or []:
        term = _words(entry)
        if not term:
            continue
        if term <= words or words <= term:
            return True
        if "oil" in term and "oil" in words and term == {"cooking", "oil"}:
            return True
    return False


def ingredient_text(meal: dict, basket: list[dict]) -> str:
    """Everything a meal has to cook with, as one searchable string: the
    basket lines it uses (the model's name and the flyer's) and what's at home."""
    by_name = {_norm(line.get("name")): line for line in basket}
    having = []
    for use in meal.get("uses") or []:
        line = by_name.get(_norm(use), {})
        having += [str(use), str(line.get("deal_name") or "")]
    having += [str(x) for x in meal.get("from_home") or []]
    return " | ".join(having)


def missing_named_ingredients(meal: dict, basket: list[dict]) -> list[str]:
    """Ingredients the meal's name promises but the meal doesn't use.

    "Scrambled Eggs & Toast" that only uses bread returns ["egg"].
    """
    name = meal.get("name") or ""
    has_text = ingredient_text(meal, basket)
    return [label for label, said, has in NAME_INGREDIENTS if said.search(name) and not has.search(has_text)]


_NOT_FOR_BREAKFAST = [
    ("potatoes", re.compile(r"potato|hash ?brown|home fries", re.I)),
    ("onion", re.compile(r"(?<!green )(?<!spring )\bonions?\b", re.I)),
]


def breakfast_misfits(meal: dict, basket: list[dict]) -> list[str]:
    """What a breakfast contains that doesn't belong in one: potatoes, onion.

    Empty for lunches and dinners. Green onion (on congee, say) is fine.
    """
    if meal.get("meal") != "breakfast":
        return []
    text = f"{meal.get('name') or ''} | {ingredient_text(meal, basket)}"
    return [label for label, pattern in _NOT_FOR_BREAKFAST if pattern.search(text)]


def unknown_uses(meal: dict, basket: list[dict]) -> list[str]:
    """Entries in a meal's "uses" that aren't on the shopping list."""
    names = {_norm(line.get("name")) for line in basket}
    return [use for use in meal.get("uses") or [] if _norm(use) not in names]


def not_at_home(meal: dict, pantry: list[str]) -> list[str]:
    """Entries in a meal's "from_home" the shopper never said they have."""
    return [item for item in meal.get("from_home") or [] if not in_pantry(str(item), pantry)]


def basket_total(basket: list[dict]) -> float:
    return round(sum(float(line.get("subtotal") or 0) for line in basket), 2)


def validate_plan(plan: dict, profile: dict) -> tuple[dict, dict]:
    """Repair a priced plan and report what is still wrong with it.

    Fixed here (listed in ``fixes``):
    - a meal "uses" something that isn't in the basket: removed from the meal;
    - a meal takes something "from home" that isn't in the shopper's pantry:
      removed from the meal;
    - the basket is over budget: items no meal uses are dropped, cheapest
      first; if it is still over, an item the fewest meals rely on loses one
      unit, or is dropped when only one was bought, and so on until it fits.
      Where one removal can settle it, the cheapest such item is chosen
      rather than several smaller ones. An item a meal is named for (the
      chicken in "Chicken soup") is never removed.

    Not fixable here (listed in ``problems``, each naming its meal):
    - a meal's name promises an ingredient the meal doesn't use;
    - a breakfast contains potatoes or onion;
    - a meal is left with nothing to cook.
    - the basket is still over budget because everything left is named in a meal.

    Returns ``(plan, {"fixes": [...], "problems": [...]})``; the input is not
    modified. Totals are recomputed from what is left.
    """
    pantry = profile.get("pantry") or []
    basket = [dict(line) for line in plan.get("basket") or []]
    meals = [dict(meal) for meal in plan.get("meals") or []]
    fixes: list[str] = []

    for meal in meals:
        unknown = unknown_uses(meal, basket)
        if unknown:
            meal["uses"] = [u for u in meal.get("uses") or [] if u not in unknown]
            fixes.append(f"{meal.get('name')!r}: removed {unknown}, not on the shopping list")
        missing = not_at_home(meal, pantry)
        if missing:
            meal["from_home"] = [h for h in meal.get("from_home") or [] if h not in missing]
            fixes.append(f"{meal.get('name')!r}: removed {missing}, not in the shopper's pantry")

    budget = profile.get("budget")

    def uses_of(line) -> list[dict]:
        key = _norm(line.get("name"))
        return [meal for meal in meals if key in {_norm(u) for u in meal.get("uses") or []}]

    def named_in_a_meal(line) -> bool:
        """True when taking this item away would leave a meal's name promising
        something the meal no longer has ("Chicken soup" without the chicken)."""
        key = _norm(line.get("name"))
        for meal in uses_of(line):
            without = {**meal, "uses": [u for u in meal.get("uses") or [] if _norm(u) != key]}
            if set(missing_named_ingredients(without, basket)) - set(missing_named_ingredients(meal, basket)):
                return True
        return False

    def can_buy_fewer(line) -> bool:
        return not line.get("sold_by_weight") and float(line.get("quantity") or 1) >= 2

    while budget is not None and basket and basket_total(basket) > float(budget) + 0.005:
        over = basket_total(basket) - float(budget)
        relied_on = {id(line): len(uses_of(line)) for line in basket}
        # An item a meal is named for is never removed (one fewer is fine).
        allowed = [line for line in basket
                   if not relied_on[id(line)] or can_buy_fewer(line) or not named_in_a_meal(line)]
        if not allowed:
            break  # reported below as a problem instead
        fewest = min(relied_on[id(line)] for line in allowed)
        tier = [line for line in allowed if relied_on[id(line)] == fewest]
        # Among the items the fewest meals rely on, take the cheapest one whose
        # removal settles it in one go; dropping several small items would
        # damage more meals than dropping one. Otherwise the cheapest.
        enough = [line for line in tier if float(line.get("subtotal") or 0) >= over - 0.005]
        pool = enough if (fewest and enough) else tier
        victim = min(pool, key=lambda line: float(line.get("subtotal") or 0))
        if relied_on[id(victim)] and can_buy_fewer(victim):
            # A meal needs it: buy one fewer rather than none.
            victim["quantity"] = round(float(victim["quantity"]) - 1, 2)
            victim["subtotal"] = round(float(victim.get("price") or 0) * victim["quantity"], 2)
            fixes.append(f"over budget: bought one fewer {victim.get('name')!r} (now {victim['quantity']:g})")
            continue
        basket.remove(victim)
        gone = _norm(victim.get("name"))
        for meal in meals:
            meal["uses"] = [u for u in meal.get("uses") or [] if _norm(u) != gone]
        fixes.append(
            f"over budget: dropped {victim.get('name')!r} (${float(victim.get('subtotal') or 0):.2f}, "
            f"used by {relied_on[id(victim)]} meal(s))")

    problems: list[dict] = []
    if budget is not None and basket_total(basket) > float(budget) + 0.005:
        problems.append({"meal": None, "type": "over_budget",
                         "detail": f"the basket costs ${basket_total(basket):.2f}, over the ${float(budget):.2f} "
                                   "budget, and every item left is one a meal is named for"})
    for meal in meals:
        name = meal.get("name") or "A meal"
        for label in missing_named_ingredients(meal, basket):
            problems.append({"meal": name, "type": "name_promises_missing_ingredient",
                             "detail": f'"{name}" is named for {label} but uses none: it has only '
                                       f'{(meal.get("uses") or []) + (meal.get("from_home") or [])}'})
        for label in breakfast_misfits(meal, basket):
            problems.append({"meal": name, "type": "breakfast_with_potato_or_onion",
                             "detail": f'the breakfast "{name}" contains {label}; breakfasts must not use '
                                       "potatoes or onion"})
        if not meal.get("uses") and not meal.get("from_home"):
            problems.append({"meal": name, "type": "meal_has_no_ingredients",
                             "detail": f'"{name}" has no ingredients from the basket or from home'})

    fixed = {**plan, "basket": basket, "meals": meals,
             "estimated_total": basket_total(basket),
             "servings_total": sum(int(meal.get("servings") or 0) for meal in meals)}
    return fixed, {"fixes": fixes, "problems": problems}
