"""Pure-Python flyer item parser — no AI, no network, instant.

Replaces the Claude Haiku parsing step in the pipeline. For each raw flyer item
it extracts weight, computes per-kg / per-unit pricing, assigns a category by
keyword, and produces a cleaned display name.

Design notes:
- Weight regexes handle single sizes ("500 g", "1.5kg", "2 lb"), ranges
  ("252/336 g", "292-393 g" -> averaged), volumes (ml/L treated as g/kg), and
  count packs ("8's", "12 pack" -> per-unit price).
- Categories are keyword-matched against the lowercase name; first match wins,
  checked in a fixed priority order (e.g. "frozen chicken" is Frozen).
- Clean names strip store-brand prefixes (PC®, NO NAME®, ...) and trailing
  size/pack info, then Title Case.
"""

import re

# ---------------------------------------------------------------------------
# Weight / size extraction
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"

# Multi-size ranges like "252/336 g", "292-393 g", "375 g - 1 kg" are common in
# flyers; we average the bounds. Checked before single sizes.
_RANGE_RE = re.compile(
    rf"{_NUM}\s*(?:g|kg|ml|l|lb|lbs)?\s*[-/–]\s*{_NUM}\s*(g|kg|ml|l|lb|lbs)\b",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(rf"{_NUM}\s*(kg|g|ml|l|lb|lbs)\b", re.IGNORECASE)

# Count packs: "8's", "12 pack", "6-pack", "24pk"
_COUNT_RE = re.compile(
    r"\b(\d+)\s*(?:'s|’s|\s*-?\s*(?:pack|pk)\b)",
    re.IGNORECASE,
)

# Unit -> kilograms conversion factors (ml≈g, L≈kg approximations).
_TO_KG = {
    "g": 0.001,
    "kg": 1.0,
    "ml": 0.001,
    "l": 1.0,
    "lb": 0.453592,
    "lbs": 0.453592,
}


def extract_weight(name: str) -> tuple[float | None, str | None, int | None]:
    """Extract (weight_kg, unit_shown, pack_count) from an item name.

    Returns weight in kg when a size is parseable (ranges are averaged), the
    unit string as shown, and a pack count when the item is a counted pack
    ("8's", "12 pack"). Any field can be None.
    """
    count = None
    m = _COUNT_RE.search(name)
    if m:
        count = int(m.group(1))

    m = _RANGE_RE.search(name)
    if m:
        low, high, unit = float(m.group(1)), float(m.group(2)), m.group(3).lower()
        avg = (low + high) / 2
        return round(avg * _TO_KG[unit], 3), unit, count

    m = _SINGLE_RE.search(name)
    if m:
        value, unit = float(m.group(1)), m.group(2).lower()
        return round(value * _TO_KG[unit], 3), unit, count

    return None, None, count


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------

# Checked in order; first category with a keyword hit wins. Frozen is checked
# first so "frozen chicken wings" lands in Frozen, and Household before Snacks
# so "dish soap" never matches a food keyword. Keywords are matched on WORD
# BOUNDARIES (not substrings) so "ham" doesn't match "Hammer" and "egg"
# doesn't match "eggplant".
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "Frozen",
        ["frozen", "ice cream", "waffle", "pizza", "popsicle", "novelties"],
    ),
    (
        "Household",
        [
            "paper towel", "tissue", "detergent", "soap", "shampoo",
            "toothpaste", "toilet paper", "bleach", "cleaner", "laundry",
            "dish", "deodorant", "diaper",
        ],
    ),
    (
        "Meat & Seafood",
        [
            "chicken", "beef", "pork", "salmon", "shrimp", "turkey", "lamb",
            "fish", "tuna", "crab", "bacon", "sausage", "ham", "wiener",
            "hot dog", "meatball", "tilapia", "basa", "cod", "steak", "ribs",
            "drumstick", "thigh", "veal", "duck", "prawn",
        ],
    ),
    (
        "Dairy & Eggs",
        [
            "milk", "cheese", "yogurt", "yogourt", "butter", "cream", "eggs",
            "egg", "margarine", "cheddar", "mozzarella", "brie", "feta",
        ],
    ),
    (
        "Produce",
        [
            "apple", "banana", "mango", "broccoli", "spinach", "carrot",
            "onion", "tomato", "lettuce", "potato", "pepper", "cucumber",
            "zucchini", "eggplant", "avocado", "grape", "orange", "lemon",
            "lime", "berry", "berries", "melon", "pear", "peach", "plum",
            "celery", "cabbage", "kale", "bok choy", "mushroom", "garlic",
            "ginger", "cauliflower", "corn", "squash", "salad",
        ],
    ),
    (
        "Grains & Bread",
        [
            "rice", "pasta", "bread", "oats", "flour", "noodle", "tortilla",
            "cereal", "bagel", "bun", "roll", "pita", "naan", "cracker",
            "spaghetti", "macaroni", "quinoa", "couscous",
        ],
    ),
    (
        "Snacks & Drinks",
        [
            "chips", "juice", "soda", "water", "coffee", "tea", "cookie",
            "chocolate", "candy", "pop", "cola", "granola", "bar", "popcorn",
            "pretzel", "nuts", "drink", "beverage",
        ],
    ),
]

DEFAULT_CATEGORY = "Other"

# Pre-compile one word-boundary regex per category (fast: one scan each).
_CATEGORY_RES: list[tuple[str, re.Pattern]] = [
    (
        category,
        re.compile(
            r"\b(?:" + "|".join(re.escape(kw) for kw in keywords) + r")\b",
            re.IGNORECASE,
        ),
    )
    for category, keywords in CATEGORY_KEYWORDS
]


def categorize(name: str) -> str:
    """Assign a category by word-boundary keyword match; first match wins."""
    for category, pattern in _CATEGORY_RES:
        if pattern.search(name):
            return category
    return DEFAULT_CATEGORY


# ---------------------------------------------------------------------------
# Name cleaning
# ---------------------------------------------------------------------------

# Store-brand prefixes to strip from the front of names.
_BRAND_PREFIX_RE = re.compile(
    r"^\s*(?:pc|no name|your fresh market|president'?s choice|great value|"
    r"compliments|selection|exact|life brand)\s*[®™©]?\s*",
    re.IGNORECASE,
)

# Trailing size/pack info: ", 500 g", " 252/336 g", ", 8's", " 12 pack",
# possibly chained ("375 g or 8's"). Applied repeatedly from the end.
_TRAILING_SIZE_RE = re.compile(
    r"[,\s]*(?:or\s+)?(?:\d+(?:\.\d+)?\s*[-/–]\s*)?\d+(?:\.\d+)?\s*"
    r"(?:kg|g|ml|l|lb|lbs|'s|’s|pack|pk)\.?\s*$",
    re.IGNORECASE,
)

_MARKS_RE = re.compile(r"[®™©]")

# Small words kept lowercase in Title Case (unless first word).
_MINOR_WORDS = {"and", "or", "of", "the", "with", "in", "a", "an"}


def _title_case(text: str) -> str:
    words = text.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in _MINOR_WORDS:
            out.append(lw)
        else:
            out.append(lw.capitalize())
    return " ".join(out)


def clean_name(name: str) -> str:
    """Produce a human-friendly display name from a raw flyer name."""
    text = _MARKS_RE.sub("", name or "").strip()
    text = _BRAND_PREFIX_RE.sub("", text)

    # Strip trailing size/pack info, repeatedly (names often chain sizes).
    for _ in range(4):
        stripped = _TRAILING_SIZE_RE.sub("", text).rstrip(" ,.-")
        if stripped == text:
            break
        text = stripped

    text = re.sub(r"\s{2,}", " ", text).strip(" ,.-")
    if not text:  # everything got stripped — fall back to the original
        text = _MARKS_RE.sub("", name or "").strip()
    return _title_case(text)


# ---------------------------------------------------------------------------
# Item parsing
# ---------------------------------------------------------------------------


def _to_price(value) -> float | None:
    try:
        p = float(value)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None


def parse_item(raw: dict) -> dict:
    """Parse one raw flyer item into the cleaned record the DB stores.

    Pricing rules:
    - weight parseable  -> price_per_kg = price / weight_kg
    - counted pack      -> price_per_unit = price / count
    - neither           -> sold per unit; price_per_unit = listed price
    """
    name = raw.get("name", "") or ""
    price = _to_price(raw.get("price"))

    weight_kg, unit, count = extract_weight(name)

    price_per_kg = None
    price_per_unit = None
    if price is not None:
        if weight_kg:
            price_per_kg = round(price / weight_kg, 2)
        elif count:
            price_per_unit = round(price / count, 2)
        else:
            price_per_unit = price

    return {
        "merchant": raw.get("merchant"),
        "original_name": name,
        "clean_name": clean_name(name),
        "category": categorize(name),
        "price": price,
        "price_per_kg": price_per_kg,
        "price_per_unit": price_per_unit,
        "weight_kg": weight_kg,
        "valid_from": raw.get("valid_from") or None,
        "valid_to": raw.get("valid_to") or None,
    }


def parse_items(raw_items: list[dict], progress_cb=None) -> list[dict]:
    """Parse all raw items. Pure Python — runs in milliseconds.

    ``progress_cb`` is kept for interface compatibility with the pipeline's
    progress tracking; it's called once at the end since parsing is instant.
    """
    cleaned = [parse_item(r) for r in raw_items]
    if progress_cb is not None:
        progress_cb(len(cleaned))
    return cleaned
