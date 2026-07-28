"""Budget-mode endpoint: build a weekly basket from a store's deals + preferences."""

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


# Coarse category filters we can apply deterministically before calling Claude.
# (Halal / Gluten-free are nuanced, so we pass those to Claude rather than
# guessing from the product name.)
_RESTRICTION_CATEGORY_BLOCK = {
    "Vegetarian": {"Meat & Seafood"},
    "Vegan": {"Meat & Seafood", "Dairy & Eggs"},
}


def _filter_deals(deals: list[dict], restriction: str | None) -> list[dict]:
    """Drop non-food deals and any that violate the dietary restriction.

    Household products (cleaning supplies, paper goods, etc.) are never edible,
    so they're removed deterministically here — the prompt's food-only rule is
    the backstop for non-food items the categorizer missed (e.g. pet food lands
    in "Other").
    """
    blocked = {"Household"}
    blocked |= _RESTRICTION_CATEGORY_BLOCK.get(restriction or "", set())
    return [d for d in deals if d.get("category") not in blocked]


@router.post("/budget-plan")
def budget_plan(profile: BudgetProfile) -> dict:
    """Build a weekly basket for the shopper's chosen store.

    Pulls the store's deals, drops items that clash with the dietary
    restriction, then asks Claude Haiku to assemble a cuisine-aware weekly
    basket, cuisine-matched meals, and (if over budget) a swap suggestion.
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

    filtered = _filter_deals(deals, profile.restriction)

    plan = generate_budget_plan(
        {
            "budget": profile.budget,
            "people": profile.people,
            "cuisines": profile.cuisines,
            "restriction": profile.restriction,
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
        "plan": plan,
    }
