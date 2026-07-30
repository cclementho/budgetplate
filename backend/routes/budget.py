"""Budget-mode endpoint: build a weekly basket from a store's deals + preferences."""

import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ai.budget import generate_budget_plan
from queries import get_items
from scraper.pipeline import has_fresh_data, run_pipeline

router = APIRouter()


class BudgetProfile(BaseModel):
    postal_code: str
    merchant: str
    budget: float = Field(60, ge=20, le=200)
    people: int = Field(1, ge=1)
    cuisines: list[str] = []  # up to 3 cuisine preferences
    restriction: str | None = None  # single dietary restriction (optional)
    pantry: list[str] = []  # ingredients the shopper already has at home


# Coarse category filters we can apply deterministically before calling Claude.
# (Halal / Gluten-free are nuanced, so we pass those to Claude rather than
# guessing from the product name.)
_RESTRICTION_CATEGORY_BLOCK = {
    "Vegetarian": {"Meat & Seafood"},
    "Vegan": {"Meat & Seafood", "Dairy & Eggs"},
}


def _pantry_pattern(pantry: list[str]) -> re.Pattern | None:
    """Compile a word-boundary regex matching any pantry term.

    Word boundaries keep "rice" from matching "price" and "oil" from matching
    "broil", which plain substring matching would get wrong.
    """
    terms = sorted(
        {t.strip() for t in pantry if t and t.strip()},
        key=len,
        reverse=True,  # longest first so "soy sauce" wins over "soy"
    )
    if not terms:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")\b",
        re.IGNORECASE,
    )


def _filter_deals(
    deals: list[dict], restriction: str | None, pantry: list[str]
) -> tuple[list[dict], list[str]]:
    """Drop non-food deals, dietary clashes, and anything already at home.

    Household products (cleaning supplies, paper goods, etc.) are never edible,
    so they're removed deterministically here — the prompt's food-only rule is
    the backstop for non-food items the categorizer missed (e.g. pet food lands
    in "Other").

    Pantry items are removed so Claude can't spend budget on something the
    shopper already owns. Returns ``(kept_deals, skipped_names)`` — the skipped
    names let the UI confirm what was left out.
    """
    blocked = {"Household"}
    blocked |= _RESTRICTION_CATEGORY_BLOCK.get(restriction or "", set())
    pantry_re = _pantry_pattern(pantry)

    kept: list[dict] = []
    skipped: list[str] = []
    for d in deals:
        if d.get("category") in blocked:
            continue
        name = d.get("clean_name") or ""
        if pantry_re is not None and pantry_re.search(name):
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


@router.post("/budget-plan")
def budget_plan(profile: BudgetProfile) -> dict:
    """Build a weekly basket for the shopper's chosen store.

    Pulls the store's deals, drops items that clash with the dietary
    restriction or that the shopper already has at home, then asks Claude Haiku
    to assemble a cuisine-aware weekly basket, cuisine-matched meals, and (if
    over budget) a swap suggestion.
    """
    postal_code = profile.postal_code.replace(" ", "").upper()

    if not has_fresh_data(postal_code):
        run_pipeline(postal_code)

    deals = get_items(postal_code, merchant=profile.merchant)
    if not deals:
        return {
            "postal_code": postal_code,
            "merchant": profile.merchant,
            "plan": None,
            "message": "No deals found at this store this week.",
        }

    filtered, skipped_pantry = _filter_deals(
        deals, profile.restriction, profile.pantry
    )

    plan = generate_budget_plan(
        {
            "budget": profile.budget,
            "people": profile.people,
            "cuisines": profile.cuisines,
            "restriction": profile.restriction,
            "pantry": profile.pantry,
            "merchant": profile.merchant,
        },
        filtered,
    )

    # Recompute the total from the basket subtotals so the budget verdict is
    # correct even if the model's arithmetic drifts.
    estimated = None
    if isinstance(plan, dict):
        basket = plan.get("basket") or []
        subtotals = [
            item.get("subtotal")
            for item in basket
            if isinstance(item.get("subtotal"), (int, float))
        ]
        if subtotals:
            estimated = round(sum(subtotals), 2)
            plan["estimated_total"] = estimated
        else:
            estimated = plan.get("estimated_total")

    return {
        "postal_code": postal_code,
        "merchant": profile.merchant,
        "budget": profile.budget,
        "estimated_total": estimated,
        "under_budget": (estimated is not None and estimated <= profile.budget),
        # Deals left out because the shopper already has them.
        "skipped_pantry_items": skipped_pantry,
        "plan": plan,
    }
