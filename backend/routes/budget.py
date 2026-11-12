"""Budget-mode endpoint: build a weekly basket from a store's deals + preferences."""

import json
import logging
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ai.budget import generate_budget_plan, servings_target, stream_budget_plan
from ai.candidates import bucket_for, cuisine_note, select_candidates
from ai.client import ClaudeResponseError, last_call_stats
from queries import get_items
from scraper.pipeline import has_fresh_data, run_pipeline

router = APIRouter()
logger = logging.getLogger("budgetplate.budget")


class _Stopwatch:
    """Collects per-stage durations in milliseconds for one request."""

    def __init__(self):
        self.started = time.perf_counter()
        self.stages: dict[str, int] = {}

    def time(self, name: str, fn, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.stages[name] = round((time.perf_counter() - t0) * 1000)

    def summary(self) -> str:
        total = round((time.perf_counter() - self.started) * 1000)
        parts = [f"{k}={v}" for k, v in self.stages.items()]
        return " ".join(parts + [f"total_ms={total}"])

# Shown to the shopper (the frontend displays the response's "detail") when
# Claude's reply is cut off, empty, refused or not valid JSON.
PLAN_FAILED_MESSAGE = (
    "We couldn't finish your meal plan this time. Please try again in a moment."
)


class BudgetProfile(BaseModel):
    postal_code: str
    merchant: str
    budget: float = Field(60, ge=20, le=200)
    people: int = Field(1, ge=1)
    cuisines: list[str] = []  # up to 3 cuisine preferences
    restriction: str | None = None  # single dietary restriction (optional)
    pantry: list[str] = []  # ingredients the shopper already has at home
    # What the shopper can cook with; every meal must work with only these.
    appliances: list[
        Literal["Stovetop", "Oven", "Microwave", "Air fryer", "Rice cooker", "Instant Pot"]
    ] = Field(default_factory=lambda: ["Stovetop", "Oven"], min_length=1)
    # Plan breakfast too (21 servings per person a week instead of 14).
    include_breakfast: bool = True


# Coarse category filters we can apply deterministically before calling Claude.
# (Halal / Gluten-free are nuanced, so we pass those to Claude rather than
# guessing from the product name.)
_RESTRICTION_CATEGORY_BLOCK = {
    "Vegetarian": {"Meat & Seafood"},
    "Vegan": {"Meat & Seafood", "Dairy & Eggs"},
}


# --- "already at home" matching ------------------------------------------------
#
# A deal is left out only when the pantry term is what the product IS, not
# merely a word in its name: "Gold Seal Butter" is butter, "Danish Butter
# Cookies" and "Peanut Butter" are not. A product is named by its last words
# ("Chalo! Basmati Rice" is rice; "Rice Vinegar" is vinegar), so the rule is:
# one of the listing's alternatives must END with the term.

# Size and marketing text that can follow the product's real name.
_SIZE_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*[-/x]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:kg|g|ml|l|lb|lbs|oz|pk|pack|'s|ct|m)\b|\b\d+(?:\.\d+)?%",
    re.IGNORECASE,
)
_TRAILER_RE = re.compile(
    r"\b(?:product of|selected variet|any variet|assorted variet).*$", re.IGNORECASE
)
# Words describing the form a product comes in; they can follow its name
# without changing what it is ("Butter Sticks" is butter).
_FORM_WORDS = {"stick", "block", "brick", "tub", "salted", "unsalted"}

# "X <term>" products that are a different thing from the term on its own.
_DIFFERENT_PRODUCT = {
    "butter": {"peanut", "almond", "cashew", "nut", "seed", "sunflower", "apple", "cocoa", "shea",
               "not"},
    "sugar": {"zero", "no", "reduced", "less", "low"},  # "Ketchup Zero Sugar" isn't sugar
    "milk": {"coconut", "almond", "oat", "soy", "chocolate", "condensed", "evaporated"},
    "rice": {"fried", "cauliflower"},
    "egg": {"chocolate", "easter", "mini", "creme"},
    "onion": {"green"},
    "bread": {"garlic", "banana"},
}

# Staples whose label on the form isn't the word printed on the product.
_PANTRY_ALIASES = {
    "cooking oil": ["canola oil", "vegetable oil", "sunflower oil", "corn oil"],
}


def _singular(word: str) -> str:
    if word.endswith("oes"):
        return word[:-2]  # potatoes, tomatoes
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"  # berries
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _name_words(text: str) -> list[str]:
    return [_singular(w) for w in re.findall(r"[a-z0-9']+", text.lower().replace("’", "'"))]


def _pantry_terms(pantry: list[str]) -> list[list[str]]:
    """Each pantry entry (and its aliases) as a list of singular words."""
    terms = []
    for entry in pantry:
        entry = (entry or "").strip().lower()
        if not entry:
            continue
        for phrase in [entry, *_PANTRY_ALIASES.get(entry, [])]:
            words = _name_words(phrase)
            if words and words not in terms:
                terms.append(words)
    return terms


def _ends_with_term(words: list[str], terms: list[list[str]]) -> bool:
    """True when the words name a pantry item: they end with one of the terms."""
    words = list(words)
    while words and words[-1] in _FORM_WORDS:
        words.pop()
    for term in terms:
        n = len(term)
        if len(words) < n or words[-n:] != term:
            continue
        before = words[-n - 1] if len(words) > n else None
        if before in _DIFFERENT_PRODUCT.get(term[-1], set()):
            continue  # e.g. "peanut butter" when the shopper has butter
        return True
    return False


def _is_pantry_item(name: str, terms: list[list[str]]) -> bool:
    """True when EVERY alternative in the listing is something already at home.

    Flyer names bundle alternatives ("Aman's Milk or Suji Rusk"), and the
    shopper may want the other one, so a listing is left out only when each
    comma- or "or"-separated alternative is a pantry item or isn't food the
    plan would use anyway. At least one must be a real pantry match.

    "Basmati or Signature Rice" means basmati rice or signature rice: an
    alternative that is only a description borrows the noun of the one after
    it. That is not done when the alternative is itself a plural noun
    ("Carrots or Red Onions") or the next one has no describing word of its
    own ("Carrots or Onions").
    """
    text = _SIZE_RE.sub(" ", _TRAILER_RE.sub("", name or ""))
    pieces = re.split(r"(,|\bor\b)", text, flags=re.IGNORECASE)
    parts = []  # (raw text, words, joined to the next part by "or")
    for i in range(0, len(pieces), 2):
        words = _name_words(pieces[i])
        if words:
            joiner = pieces[i + 1].lower() if i + 1 < len(pieces) else ""
            parts.append((pieces[i].strip(), words, joiner == "or"))

    matched = False
    for i, (raw, words, or_next) in enumerate(parts):
        hit = _ends_with_term(words, terms)
        if not hit and or_next and i + 1 < len(parts) and not raw.lower().endswith("s"):
            nxt = parts[i + 1][1]
            # Only the noun is borrowed, so this works for one-word terms:
            # "Sandwich Spread or Silver Swan Soy Sauce" is not soy sauce.
            hit = len(nxt) > 1 and _ends_with_term(words + nxt[-1:], [t for t in terms if len(t) == 1])
        if hit:
            matched = True
        elif bucket_for({"clean_name": raw}) is not None:
            return False  # a food alternative the shopper doesn't have
    return matched


def _filter_deals(
    deals: list[dict], restriction: str | None, pantry: list[str]
) -> tuple[list[dict], list[str]]:
    """Drop non-food deals, dietary clashes, and anything already at home.

    Household products (cleaning supplies, paper goods, etc.) are never edible,
    so they're removed deterministically here — the prompt's food-only rule is
    the backstop for non-food items the categorizer missed (e.g. pet food lands
    in "Other").

    Pantry items are removed so Claude can't spend budget on something the
    shopper already owns. Only deals that ARE the pantry item go: having
    butter at home doesn't hide butter cookies or peanut butter. A listing
    with alternatives ("X or Y") goes only when every alternative is at home. Returns ``(kept_deals, skipped_names)`` — the skipped
    names let the UI confirm what was left out.
    """
    blocked = {"Household"}
    blocked |= _RESTRICTION_CATEGORY_BLOCK.get(restriction or "", set())
    terms = _pantry_terms(pantry)

    kept: list[dict] = []
    skipped: list[str] = []
    for d in deals:
        if d.get("category") in blocked:
            continue
        name = d.get("clean_name") or ""
        if terms and _is_pantry_item(name, terms):
            skipped.append(name)
            continue
        kept.append(d)

    # De-duplicate skipped names while preserving order.
    seen: set[str] = set()
    unique_skipped = []
    for n in skipped:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique_skipped.append(n)

    return kept, unique_skipped


def _plan_profile(profile: BudgetProfile) -> dict:
    return {
        "budget": profile.budget,
        "people": profile.people,
        "cuisines": profile.cuisines,
        "restriction": profile.restriction,
        "pantry": profile.pantry,
        "appliances": profile.appliances,
        "include_breakfast": profile.include_breakfast,
        "merchant": profile.merchant,
    }


def _prepare(profile: BudgetProfile, postal_code: str, watch: _Stopwatch) -> dict | None:
    """Everything before the Claude call: fresh data, deals, filtering, shortlist.

    Returns None when the store has no deals this week.
    """
    if not watch.time("fresh_check_ms", has_fresh_data, postal_code):
        watch.time("pipeline_ms", run_pipeline, postal_code)

    deals = watch.time("get_items_ms", get_items, postal_code, merchant=profile.merchant)
    if not deals:
        return None

    filtered, skipped_pantry = watch.time(
        "filter_ms", _filter_deals, deals, profile.restriction, profile.pantry
    )
    # Cut 400+ deals down to a balanced, good-value shortlist for Claude.
    candidates = watch.time("prefilter_ms", select_candidates, filtered, profile.cuisines)
    return {
        "candidates": candidates,
        "deals_available": len(filtered),
        "skipped_pantry": skipped_pantry,
        # Say so up front when the store has little for the shopper's cuisine.
        "cuisine_note": cuisine_note(candidates, profile.cuisines, profile.merchant),
    }


def _no_deals(profile: BudgetProfile, postal_code: str) -> dict:
    return {
        "postal_code": postal_code,
        "merchant": profile.merchant,
        "plan": None,
        "message": "No deals found at this store this week.",
    }


def _response(profile: BudgetProfile, postal_code: str, prep: dict, plan: dict) -> dict:
    # Prices, subtotals and the total were computed from the flyer in
    # ai.budget.finalize_plan, not by the model.
    estimated = plan["estimated_total"] if plan["basket"] else None
    return {
        "postal_code": postal_code,
        "merchant": profile.merchant,
        "budget": profile.budget,
        "estimated_total": estimated,
        "under_budget": (estimated is not None and estimated <= profile.budget),
        # Deals left out because the shopper already has them.
        "skipped_pantry_items": prep["skipped_pantry"],
        # Set when fewer than 5 shortlisted deals fit the shopper's cuisines.
        "cuisine_note": prep["cuisine_note"],
        "plan": plan,
    }


def _log_timing(profile, postal_code, outcome, watch, prep, stats) -> None:
    """One line per request so Render logs show where the time went."""
    logger.info(
        "budget_plan timing: postal=%s merchant=%s outcome=%s %s deals_available=%d "
        "deals_sent=%d model=%s input_tokens=%s output_tokens=%s",
        postal_code, profile.merchant, outcome, watch.summary(),
        prep["deals_available"] if prep else 0, len(prep["candidates"]) if prep else 0,
        stats.get("model"), stats.get("input_tokens"), stats.get("output_tokens"),
    )


@router.post("/budget-plan")
def budget_plan(profile: BudgetProfile) -> dict:
    """Build a weekly basket for the shopper's chosen store.

    Pulls the store's deals, drops items that clash with the dietary
    restriction or that the shopper already has at home, then asks Claude Haiku
    to assemble a cuisine-aware weekly basket, cuisine-matched meals, and (if
    over budget) a swap suggestion.
    """
    postal_code = profile.postal_code.replace(" ", "").upper()
    watch = _Stopwatch()
    prep = None
    outcome = "ok"
    try:
        prep = _prepare(profile, postal_code, watch)
        if prep is None:
            outcome = "no_deals"
            return _no_deals(profile, postal_code)
        try:
            plan = watch.time(
                "claude_ms", generate_budget_plan, _plan_profile(profile), prep["candidates"]
            )
        except ClaudeResponseError as exc:
            # Already logged with stop_reason and token counts in ai.client.
            outcome = f"claude_{exc.reason}"
            raise HTTPException(status_code=502, detail=PLAN_FAILED_MESSAGE)
    finally:
        stats = last_call_stats() if "claude_ms" in watch.stages else {}
        _log_timing(profile, postal_code, outcome, watch, prep, stats)

    return _response(profile, postal_code, prep, plan)


def _sse(event: str, data: dict) -> str:
    """One server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/budget-plan/stream")
def budget_plan_stream(profile: BudgetProfile):
    """The same plan as /budget-plan, sent as it is written (server-sent events).

    Events, in order:
      start  what is known before Claude answers (pantry items left out,
             cuisine note, servings target)
      item   one basket line, priced from the flyer, as soon as it is complete
      meal   one meal, as soon as it is complete
      done   the full, validated /budget-plan response; it replaces whatever
             was shown from item and meal events
      error  {"detail": ...} if the plan could not be finished

    Problems found before the Claude call (no deals, bad input) are returned
    as ordinary JSON responses, exactly as /budget-plan does.
    """
    postal_code = profile.postal_code.replace(" ", "").upper()
    watch = _Stopwatch()
    try:
        prep = _prepare(profile, postal_code, watch)
    except Exception:
        _log_timing(profile, postal_code, "error", watch, None, {})
        raise
    if prep is None:
        _log_timing(profile, postal_code, "no_deals", watch, None, {})
        return _no_deals(profile, postal_code)

    plan_profile = _plan_profile(profile)

    def events():
        outcome = "client_gone"  # replaced unless the shopper leaves mid-stream
        stats: dict = {}
        started = time.perf_counter()
        try:
            yield _sse("start", {
                "postal_code": postal_code,
                "merchant": profile.merchant,
                "budget": profile.budget,
                "skipped_pantry_items": prep["skipped_pantry"],
                "cuisine_note": prep["cuisine_note"],
                "servings_target": servings_target(profile.people, profile.include_breakfast),
                "include_breakfast": profile.include_breakfast,
            })
            for kind, payload in stream_budget_plan(plan_profile, prep["candidates"]):
                if kind == "plan":
                    plan, stats = payload
                    outcome = "ok"
                    yield _sse("done", _response(profile, postal_code, prep, plan))
                else:
                    if "first_item_ms" not in watch.stages:
                        watch.stages["first_item_ms"] = round((time.perf_counter() - started) * 1000)
                    yield _sse(kind, payload)
        except ClaudeResponseError as exc:
            stats = exc.stats
            outcome = f"claude_{exc.reason}"
            yield _sse("error", {"detail": PLAN_FAILED_MESSAGE})
        except Exception:
            outcome = "error"
            logger.exception("budget plan stream failed")
            yield _sse("error", {"detail": PLAN_FAILED_MESSAGE})
        finally:
            watch.stages["claude_ms"] = stats.get(
                "claude_ms", round((time.perf_counter() - started) * 1000)
            )
            _log_timing(profile, postal_code, outcome, watch, prep, stats)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Tell proxies not to hold the events back.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
