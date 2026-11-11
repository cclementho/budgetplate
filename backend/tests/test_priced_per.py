"""Per-weight flyer prices ("$2.00/lb"): scraping, parsing, loading and planning."""

import pytest

from ai.budget import build_budget_prompt, finalize_plan
from scraper import flipp
from scraper.parser import may_be_sold_by_weight, parse_item, price_unit
from scraper.warehouse import load_batch
from tests.helpers import current_row, parsed, query

PC = "N6G2J8"


# --- which items get a price-unit lookup ------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("PORK SHOULDER BLADE ROAST", True),
    ("GREEN CABBAGE", True),
    ("Sweet Green Bell Peppers Product of USA", True),      # plural the categorizer misses
    ("BABY BOK CHOI, SHANGHAI BOK CHOI", True),
    ("COOK'S BONELESS HAM STEAK, 227 G", False),            # has a pack size: priced each
    ("LARGE EGGS, 12's", False),
    ("Great Value Paper Towels", False),
    ("Kellogg's Cereal", False),
])
def test_only_weighable_items_without_a_pack_size_are_looked_up(name, expected):
    assert may_be_sold_by_weight(name) is expected


def test_scraper_fetches_details_only_for_those_items(monkeypatch):
    rows = [
        {"item_id": 1, "name": "PORK SPLIT SIDE RIBS", "price": "3.0"},
        {"item_id": 2, "name": "Kellogg's Cereal", "price": "4.99"},
        {"item_id": 3, "name": "GREEN CABBAGE", "price": ""},            # no price: skipped anyway
        {"item_id": 4, "name": "UTILITY DUCK", "price": "3.88"},
    ]
    asked = []

    def detail(item_id):
        asked.append(item_id)
        return {} if item_id == 4 else {"price_text": "lb", "description": "6.61/kg"}  # 4: lookup failed

    monkeypatch.setattr(flipp, "get_flyer_item_detail", detail)
    assert flipp.add_price_units(rows) == 2
    assert sorted(asked) == [1, 4]
    assert (rows[0]["price_text"], rows[0]["description"]) == ("lb", "6.61/kg")
    assert rows[3]["price_text"] is None and "price_text" not in rows[1]


def test_lookups_are_capped_per_scrape(monkeypatch):
    monkeypatch.setattr(flipp, "get_flyer_item_detail", lambda item_id: {})
    rows = [{"item_id": i, "name": "CHICKEN LEGS", "price": "4.0"} for i in range(flipp.MAX_DETAIL_LOOKUPS + 50)]
    assert flipp.add_price_units(rows) == flipp.MAX_DETAIL_LOOKUPS


# --- reading the unit (values below are real Flipp price_text / descriptions) -----

@pytest.mark.parametrize("price, price_text, description, expected", [
    (13.0, "lb", "28.67/kg", ("lb", 28.67)),            # flyer's own per-kg figure is used
    (4.0, "lb", "PRODUCT OF U.S.A., NO. 1 GRADE\n8.82/KG", ("lb", 8.82)),
    (6.43, "lb", "#51004542. $14.17/kg.", ("lb", 14.17)),
    (2.0, "/lb", None, ("lb", 4.41)),                   # no per-kg figure: converted
    (5.0, "/lb 11 /kg", None, ("lb", 11.02)),
    (8.8, "/kg", None, ("kg", 8.8)),
    (1.99, "/100 g", None, ("100g", 19.9)),
    (0.99, "ea./lb", "2.18/kg, product of Canada or each", (None, None)),   # two products: ambiguous
    (1.99, "each", None, (None, None)),
    (9.0, "or $5.79 EA.", None, (None, None)),
    (8.46, None, "Each. Selected varieties. 500 g.", (None, None)),
])
def test_price_unit_from_flipp_fields(price, price_text, description, expected):
    assert price_unit(price, price_text, "", description) == expected


def test_a_per_kg_price_printed_in_the_name_identifies_a_per_lb_price():
    # FreshCo prints the per-kg price in the name; 5.99 x 2.2046 = 13.21.
    item = parse_item({"merchant": "FreshCo", "price": "5.99",
                       "name": "Eye of Round Roast Whole Cryovac, Product of Australia 13.21/kg"})
    assert (item["priced_per"], item["price_per_kg"], item["price_per_unit"]) == ("lb", 13.21, None)
    # "5-11 kg" is the range turkeys come in, not a pack: it used to give $0.31/kg.
    turkey = parse_item({"merchant": "FreshCo", "price": "2.49", "name": "Frozen Grade A Turkey 5-11 kg 5.49/kg"})
    assert (turkey["priced_per"], turkey["price_per_kg"], turkey["weight_kg"]) == ("lb", 5.49, None)
    # A per-kg figure that doesn't fit the price proves nothing.
    assert parse_item({"merchant": "FreshCo", "price": "4.99", "name": "Sumo Oranges 9.00/kg"})["priced_per"] is None


def test_items_priced_each_are_unchanged():
    item = parse_item({"merchant": "Walmart", "name": "Great Value pork back ribs", "price": "8.46",
                       "price_text": None, "description": "Each. Selected varieties. 500 g."})
    assert (item["priced_per"], item["price_per_kg"], item["price_per_unit"]) == (None, None, 8.46)


# --- warehouse, view and API --------------------------------------------------------

def test_priced_per_is_stored_and_served_through_the_view(db):
    import queries

    roast = {**current_row("PORK SHOULDER BLADE ROAST", "2.00"), "price_text": "lb", "description": "4.41/kg"}
    load_batch(PC, parsed(roast, current_row("BOK CHOY, 500 g", "1.99")))

    facts = {r["original_name"]: r for r in query(
        db, "SELECT original_name, price, priced_per, price_per_kg, unit_type, unit_price FROM fact_price_observation")}
    pork = facts["PORK SHOULDER BLADE ROAST"]
    assert (float(pork["price"]), pork["priced_per"], float(pork["price_per_kg"])) == (2.0, "lb", 4.41)
    assert (pork["unit_type"], float(pork["unit_price"])) == ("kg", 4.41)
    assert facts["BOK CHOY, 500 g"]["priced_per"] is None

    items = {i["clean_name"]: i for i in queries.get_items(PC)}
    assert items["Pork Shoulder Blade Roast"]["priced_per"] == "lb"
    assert items["Bok Choy"]["priced_per"] is None


def test_sold_by_weight_counts_as_having_a_unit_in_the_batch_stats(db):
    roast = {**current_row("PORK SHOULDER BLADE ROAST", "2.00"), "price_text": "lb"}
    load_batch(PC, parsed(roast, current_row("GREEN CABBAGE", "0.99")))
    (batch,) = query(db, "SELECT missing_size_count FROM etl_batch")
    assert batch["missing_size_count"] == 1     # only the cabbage, which is priced each


# --- the plan -----------------------------------------------------------------------

ROAST = {"clean_name": "Pork Shoulder Blade Roast", "price": 2.00, "price_per_kg": 4.41,
         "priced_per": "lb", "category": "Meat & Seafood"}
BOK = {"clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "priced_per": None, "category": "Produce"}


def test_prompt_spells_out_per_weight_prices():
    prompt = build_budget_prompt({"budget": 60, "people": 1, "merchant": "FreshCo"}, [ROAST, BOK])
    assert "1 | Pork Shoulder Blade Roast | $2.00/lb, sold by weight | $4.41/kg | Meat & Seafood" in prompt
    assert "2 | Bok Choy | $1.99 | $n/a/kg | Produce" in prompt
    assert "the weight to buy in kg" in prompt


def test_by_weight_items_cost_their_per_kg_price_times_the_kilograms():
    raw = {"basket": [{"id": 1, "name": "Pork shoulder", "quantity": 1.5},
                      {"id": 2, "name": "Bok choy", "quantity": 2}],
           "meals": [{"name": "Braised pork", "servings": 8, "uses": [1, 2]}]}
    pork, bok = finalize_plan(raw, [ROAST, BOK], {"people": 1})["basket"]
    assert (pork["price"], pork["quantity"], pork["subtotal"]) == (4.41, 1.5, 6.62)   # not 1.5 x $2.00
    assert (pork["sold_by_weight"], pork["priced_per"], pork["listed_price"]) == (True, "lb", 2.0)
    assert (bok["subtotal"], bok["sold_by_weight"], bok["priced_per"]) == (3.98, False, None)
