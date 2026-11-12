"""Pick the deals worth showing Claude: at most 80, balanced and good value.

A store can have 400+ deals on sale. Sending them all makes the prompt slow and
expensive and buries the useful ones, so this deterministic step:

1. drops things nobody cooks a meal with (non-food, pet food, snacks, candy,
   cookies, drinks);
2. sorts the rest into buckets (proteins, produce, grains & pantry, dairy &
   eggs, other food) and gives each bucket a share of the slots, so the model
   always has options in every group;
3. ranks each bucket by value (price per kg when known, else price);
4. moves items that fit the shopper's cuisines to the front of their bucket.

No randomness and no network: the same deals and cuisines always give the same
list. The parser's categories are only a starting point because many real foods
land in "Other" (e.g. plurals like "Potatoes"), so buckets use their own
plural-tolerant keywords too.
"""

import re

MAX_CANDIDATES = 80

# Share of the slots per bucket (sums to MAX_CANDIDATES). Slots a bucket can't
# fill are handed to the others in this order.
BUCKET_SLOTS = {
    "proteins": 22,
    "produce": 22,
    "grains_pantry": 16,
    "dairy_eggs": 10,
    "other_food": 10,
}

# A per-kg price below this is a parsing artefact (e.g. "$0.02/kg"), not a deal.
_MIN_PLAUSIBLE_PER_KG = 0.10


def _words(*terms: str) -> re.Pattern:
    """Whole-word matcher for the terms, tolerant of a plural s/es."""
    return re.compile(r"\b(?:" + "|".join(terms) + r")(?:e?s)?\b", re.IGNORECASE)


_NON_FOOD = _words(
    "bouquet", "flower", "plant", "rose", "tropical", r"\d+\"\s*pot", "cling wrap", "foil", "parchment", "napkin", "paper towel",
    "tissue", "garbage bag", "trash bag", "freezer bag", "sandwich bag", "cutlery",
    "candle", "batter(?:y|ie)", "light bulb", "pet", "dog food", "cat food", "dog treat",
    "cat treat", "kitten", "puppy", "milk-?bone", "stainless steel", "sauce ?pan", "hatching",
    "litter", "vitamin", "supplement", "advil", "tylenol", "reactine", "claritin", "allegra",
    "benadryl", "razor", "shampoo", "conditioner", "lotion", "deodorant", "toothpaste",
    "toothbrush", "diaper", "wipe", "soap", "detergent", "cleaner", "bleach", "toy", "plush",
    "costume", "gift card", "crib", "air freshener", "air wick", "mower", "machine",
)

_SNACK_OR_DRINK = _words(
    "cookie", "chocolate", "chocolat", "candy", "candie", "lollipop", "fruit snack", "gummie",
    "gummy", "chip", "cracker", "goldfish", "pretzel", "popcorn", "snack", "brownie", "cake",
    "cupcake", "pie", "donut", "doughnut", "dessert", "macaroon", "swiss roll", "pudding",
    "jell-?o", "jelly powder", "ice cream", "popsicle", "novelt(?:y|ie)", "granola bar",
    "protein bar", "chocolate bar", "candy bar", "nature valley",
    "turtle", "brandy bean", "bubly", "ginger ale", "nutella", "marshmallow", "gum", "fudge", "pop", "soda", "cola", "soft drink",
    "garden cocktail", "juice cocktail", "cranberry cocktail", "cocktail mix", "clamato", "juice", "nectar", "lassi", "energy drink", "drink", "beverage", "iced tea",
    "coffee", "espresso", "k-cup", "tea", "water", "kombucha", "beer", "wine", "cider",
)

_BUCKET_WORDS = {
    "proteins": _words(
        "chicken", "beef", "pork", "turkey", "lamb", "veal", "duck", "sausage", "ham", "bacon",
        "wiener", "hot dog", "meatball", "burger", "steak", "rib", "roast", "kolbassa", "salami",
        "meat", "fish", "salmon", "tuna", "cod", "tilapia", "basa", "haddock", "shrimp", "prawn",
        "tofu", "tempeh", "lentil", "dal", "chickpea", "(?<!green )bean", "falafel",
    ),
    "produce": _words(
        "apple", "banana", "mango", "orange", "lemon", "lime", "grape", "berr(?:y|ie)",
        "strawberr(?:y|ie)", "blueberr(?:y|ie)", "raspberr(?:y|ie)", "melon", "pear", "peach",
        "plum", "nectarine", "pineapple", "kiwi", "avocado", "tomato", "potato", "onion",
        "scallion", "carrot", "celery", "pepper", "cucumber", "zucchini", "eggplant", "squash",
        "pumpkin", "broccoli", "cauliflower", "cabbage", "bok choy", "gai lan", "kale",
        "spinach", "lettuce", "salad", "coleslaw", "mushroom", "garlic", "ginger", "corn",
        "cilantro", "parsley", "mint", "basil", "methi", "radish", "beet", "yam", "okra",
        "green bean", "sprout",
    ),
    "grains_pantry": _words(
        "rice", "pasta", "spaghetti", "macaroni", "penne", "lasagna", "gnocchi", "noodle",
        "ramen", "udon", "bread", "bun", "roll", "bagel", "pita", "naan", "roti", "paratha",
        "tortilla", "wrap", "flour", "oat", "cereal", "quinoa", "couscous", "barley",
        "oil", "broth", "stock", "soup", "sauce", "passata", "salsa", "paste", "vinegar",
        "canned", "spice", "masala", "curry", "seasoning", "sugar", "honey", "peanut butter",
    ),
}

_PARSER_CATEGORY_BUCKET = {
    "Meat & Seafood": "proteins",
    "Produce": "produce",
    "Grains & Bread": "grains_pantry",
    "Dairy & Eggs": "dairy_eggs",
}

# Ingredients each cuisine reaches for. Matching deals move to the front of
# their bucket; nothing is added or removed because of a cuisine. Keys are the
# cuisine options in the frontend (Home.jsx); anything else, including
# "Whatever is cheapest", simply has no boost.
CUISINE_KEYWORDS = {
    "Chinese": _words(
        "tofu", "bok choy", "gai lan", "noodle", "pork", "ginger", "scallion", "green onion",
        "soy sauce", "sesame", "napa", "cabbage", "mushroom", "garlic", "dumpling", "hoisin",
        "oyster sauce", "broccoli", "egg",
    ),
    "South Asian": _words(
        "lentil", "dal", "chickpea", "basmati", "paneer", "naan", "roti", "paratha", "ghee",
        "curry", "masala", "turmeric", "cumin", "coriander", "cilantro", "methi", "spinach",
        "cauliflower", "potato", "onion", "tomato", "yogurt", "yogourt", "okra",
    ),
    "Middle Eastern": _words(
        "chickpea", "lentil", "pita", "hummus", "tahini", "lamb", "eggplant", "cucumber",
        "tomato", "parsley", "mint", "yogurt", "yogourt", "feta", "olive", "bulgur", "couscous",
        "lemon", "falafel", "halal",
    ),
    "Mexican": _words(
        "tortilla", "bean", "salsa", "avocado", "cilantro", "lime", "corn", "jalape[nñ]o",
        "pepper", "ground beef", "taco", "chorizo", "cheddar", "tomato", "onion", "rice",
    ),
    "Italian": _words(
        "pasta", "spaghetti", "penne", "lasagna", "gnocchi", "tomato", "passata", "basil",
        "mozzarella", "parmesan", "ricotta", "olive oil", "garlic", "italian sausage",
        "zucchini", "mushroom", "pesto", "eggplant",
    ),
    "American": _words(
        "ground beef", "chicken", "potato", "cheddar", "bacon", "bread", "bun", "corn",
        "broccoli", "macaroni", "egg", "sausage", "burger", "turkey", "ham",
    ),
    "Korean": _words(
        "tofu", "kimchi", "gochujang", "noodle", "pork", "beef", "scallion", "green onion",
        "sesame", "garlic", "cabbage", "napa", "egg", "mushroom", "zucchini", "spinach",
        "radish", "rice",
    ),
    "Japanese": _words(
        "tofu", "noodle", "udon", "soba", "ramen", "salmon", "miso", "soy sauce", "nori",
        "seaweed", "egg", "cabbage", "mushroom", "scallion", "green onion", "ginger", "panko",
        "teriyaki", "edamame", "rice",
    ),
}


# The ingredients that make a cuisine that cuisine, used only to judge whether
# the store has much for it this week. Stricter than CUISINE_KEYWORDS, which
# also boosts everyday items (pork, garlic, eggs) that say nothing about a
# cuisine being well served.
CUISINE_STAPLES = {
    "Chinese": _words(
        "tofu", "bok choy", "gai lan", "napa", "ginger", "scallion", "green onion", "soy sauce",
        "sesame", "hoisin", "oyster sauce", "dumpling", "wonton", "noodle", "chow mein",
        "shiitake", "bean sprout", "five spice", "congee", "chinese",
    ),
    "South Asian": _words(
        "lentil", "dal", "chickpea", "basmati", "paneer", "naan", "roti", "paratha", "ghee",
        "curry", "masala", "turmeric", "cumin", "coriander", "methi", "okra", "atta",
    ),
    "Middle Eastern": _words(
        "chickpea", "lentil", "pita", "hummus", "tahini", "lamb", "eggplant", "feta", "olive",
        "bulgur", "couscous", "falafel", "halal", "za'?atar", "sumac",
    ),
    "Mexican": _words(
        "tortilla", "bean", "salsa", "avocado", "cilantro", "lime", "jalape[nñ]o", "taco",
        "chorizo", "corn",
    ),
    "Italian": _words(
        "pasta", "spaghetti", "penne", "lasagna", "gnocchi", "passata", "basil", "mozzarella",
        "parmesan", "ricotta", "olive oil", "italian sausage", "pesto", "tomato sauce",
        "tomato paste",
    ),
    "American": _words(
        "ground beef", "cheddar", "bacon", "burger", "bun", "hot dog", "wiener", "macaroni",
        "potato", "corn", "turkey", "chicken",
    ),
    "Korean": _words(
        "tofu", "kimchi", "gochujang", "sesame", "napa", "scallion", "green onion", "noodle",
        "radish", "short rib", "korean",
    ),
    "Japanese": _words(
        "tofu", "udon", "soba", "ramen", "noodle", "miso", "nori", "seaweed", "panko",
        "teriyaki", "edamame", "salmon", "sushi", "japanese",
    ),
}


def bucket_for(deal: dict) -> str | None:
    """Which bucket a deal belongs to, or None when it shouldn't be offered."""
    name = deal.get("clean_name") or ""
    category = deal.get("category")
    if category == "Household" or _NON_FOOD.search(name):
        return None
    # Snack words are trusted everywhere except in meat listings, where they
    # are usually part of a meal's name ("Chicken or Shepherd's Pie").
    if category == "Snacks & Drinks" or (
        category != "Meat & Seafood" and _SNACK_OR_DRINK.search(name)
    ):
        return None
    if category in _PARSER_CATEGORY_BUCKET:
        return _PARSER_CATEGORY_BUCKET[category]
    for bucket, pattern in _BUCKET_WORDS.items():
        if pattern.search(name):
            return bucket
    return "other_food"


def _value_ranks(deals: list[dict]) -> dict[int, float]:
    """Rank deals by value as a 0..1 fraction (0 = best), keyed by list index.

    Per-kg prices and per-item prices aren't comparable, so each group is
    ranked on its own scale and the two are then interleaved by fraction.
    """
    def per_kg(d):
        v = d.get("price_per_kg")
        return float(v) if v is not None and float(v) >= _MIN_PLAUSIBLE_PER_KG else None

    ranks: dict[int, float] = {}
    by_weight = [(per_kg(d), i) for i, d in enumerate(deals) if per_kg(d) is not None]
    by_item = [(float(d.get("price") or 0), i) for i, d in enumerate(deals) if per_kg(d) is None]
    for group in (by_weight, by_item):
        group.sort(key=lambda pair: (pair[0], deals[pair[1]].get("clean_name") or ""))
        for position, (_, index) in enumerate(group):
            ranks[index] = position / max(len(group), 1)
    return ranks


def _cuisine_patterns(cuisines) -> list[re.Pattern]:
    return [CUISINE_KEYWORDS[c] for c in (cuisines or []) if c in CUISINE_KEYWORDS]


# Below this many matching deals on the shortlist, the shopper is told the
# store has little for their cuisine this week.
MIN_CUISINE_MATCHES = 5


def cuisine_match_count(deals: list[dict], cuisines: list[str] | None) -> int | None:
    """How many of the deals are staples of the shopper's cuisines.

    None when there is nothing to measure: no cuisine chosen, or only options
    without keywords such as "Whatever is cheapest".
    """
    patterns = [CUISINE_STAPLES[c] for c in (cuisines or []) if c in CUISINE_STAPLES]
    if not patterns:
        return None
    return sum(
        1 for deal in deals if any(p.search(deal.get("clean_name") or "") for p in patterns)
    )


def cuisine_note(deals: list[dict], cuisines: list[str] | None, merchant: str) -> str | None:
    """A short, honest heads-up when few shortlisted deals fit the cuisines."""
    count = cuisine_match_count(deals, cuisines)
    if count is None or count >= MIN_CUISINE_MATCHES:
        return None
    names = [c for c in cuisines if c in CUISINE_STAPLES]
    label = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return (
        f"Not many {label} staples on sale at {merchant} this week, "
        "so some meals lean on what's cheap."
    )


def select_candidates(
    deals: list[dict], cuisines: list[str] | None = None, limit: int = MAX_CANDIDATES
) -> list[dict]:
    """Return at most ``limit`` deals: food only, balanced, best value first.

    ``deals`` are cleaned rows (clean_name, category, price, price_per_kg).
    The result is grouped by bucket; the input is not modified.
    """
    patterns = _cuisine_patterns(cuisines)

    buckets: dict[str, list[dict]] = {b: [] for b in BUCKET_SLOTS}
    seen: set[tuple] = set()
    for deal in deals:
        key = ((deal.get("clean_name") or "").lower(), deal.get("price"))
        if key in seen:  # the same listing twice adds nothing
            continue
        seen.add(key)
        bucket = bucket_for(deal)
        if bucket is not None:
            buckets[bucket].append(deal)

    ordered: dict[str, list[dict]] = {}
    for bucket, items in buckets.items():
        ranks = _value_ranks(items)

        def sort_key(pair):
            index, deal = pair
            name = deal.get("clean_name") or ""
            boosted = any(p.search(name) for p in patterns)
            return (0 if boosted else 1, ranks[index], name)

        ordered[bucket] = [deal for _, deal in sorted(enumerate(items), key=sort_key)]

    # Give each bucket its share, scaled to the limit, then hand unused slots
    # to buckets that still have deals.
    total_slots = sum(BUCKET_SLOTS.values())
    take = {b: min(len(ordered[b]), BUCKET_SLOTS[b] * limit // total_slots) for b in BUCKET_SLOTS}
    spare = limit - sum(take.values())
    while spare > 0:
        grew = False
        for bucket in BUCKET_SLOTS:
            if spare > 0 and take[bucket] < len(ordered[bucket]):
                take[bucket] += 1
                spare -= 1
                grew = True
        if not grew:
            break

    return [deal for bucket in BUCKET_SLOTS for deal in ordered[bucket][: take[bucket]]]
