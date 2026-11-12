"""The deterministic shortlist of deals sent to Claude (ai/candidates.py)."""

import pytest

from ai.candidates import BUCKET_SLOTS, CUISINE_KEYWORDS, bucket_for, select_candidates

# Cuisine options offered by the frontend (frontend/src/components/Home.jsx).
FRONTEND_CUISINES = ["Chinese", "South Asian", "Middle Eastern", "Mexican", "Italian",
                     "American", "Korean", "Japanese", "Whatever is cheapest"]


def deal(name, price=3.00, per_kg=None, category="Other"):
    return {"clean_name": name, "price": price, "price_per_kg": per_kg, "category": category}


def many(prefix, n, category, start=1.0):
    return [deal(f"{prefix} {i:03d}", price=start + i, per_kg=start + i, category=category)
            for i in range(n)]


def test_non_food_snacks_candy_cookies_and_drinks_are_dropped():
    dropped = [
        deal("Palmolive Dish Soap", category="Household"),
        deal("Fancy Feast Wet Cat Food"), deal("Fall Mixed Bouquets"), deal("Glad Cling Wrap 60 M"),
        deal("Christie Cookies Selected Varieties"), deal("Chupa Chups Candy"),
        deal("Hershey's Assorted Chocolates"), deal("Lay's Potato Chips", category="Produce"),
        deal("Coca-cola or Pepsi Soft Drinks", category="Snacks & Drinks"),
        deal("Mott's Clamato or Garden Cocktail"), deal("Folgers Ground Coffee"),
        deal("Orange Juice", category="Produce"),
    ]
    assert [bucket_for(d) for d in dropped] == [None] * len(dropped)
    assert select_candidates(dropped) == []


def test_real_foods_the_parser_left_in_other_still_get_a_bucket():
    assert bucket_for(deal("Yellow Potatoes 10 Lb Product of Canada")) == "produce"
    assert bucket_for(deal("Aman's Lentils 1.81 Kg")) == "proteins"
    assert bucket_for(deal("Green Beans Product of Canada")) == "produce"
    assert bucket_for(deal("Campbell's Broth")) == "grains_pantry"
    assert bucket_for(deal("Large Eggs", category="Dairy & Eggs")) == "dairy_eggs"
    assert bucket_for(deal("Heluva Good! Dip")) == "other_food"
    # Meal names in the meat category keep their snack-like words.
    assert bucket_for(deal("Crispy Chicken or Shepherd's Pie", category="Meat & Seafood")) == "proteins"
    assert bucket_for(deal("Cocktail Wieners", category="Meat & Seafood")) == "proteins"


def test_at_most_80_with_every_bucket_represented():
    deals = (many("Chicken", 150, "Meat & Seafood") + many("Carrots", 150, "Produce")
             + many("Rice", 100, "Grains & Bread") + many("Cheese", 60, "Dairy & Eggs")
             + many("Frozen Dinner", 40, "Frozen"))
    out = select_candidates(deals)
    assert len(out) == 80 == sum(BUCKET_SLOTS.values())
    counts = {b: sum(bucket_for(d) == b for d in out) for b in BUCKET_SLOTS}
    assert counts == BUCKET_SLOTS


def test_unused_slots_go_to_buckets_that_still_have_deals():
    # A vegetarian shopper: no proteins bucket at all.
    deals = many("Carrots", 100, "Produce") + many("Rice", 100, "Grains & Bread")
    out = select_candidates(deals)
    assert len(out) == 80
    assert {bucket_for(d) for d in out} == {"produce", "grains_pantry"}
    # A small store: everything edible is kept.
    assert len(select_candidates(many("Carrots", 12, "Produce"))) == 12


def test_best_value_first_within_a_bucket():
    deals = [deal("Pork Chops", 9.0, per_kg=11.0, category="Meat & Seafood"),
             deal("Chicken Legs", 5.0, per_kg=4.4, category="Meat & Seafood"),
             deal("Ground Beef", 7.0, per_kg=8.8, category="Meat & Seafood")]
    assert [d["clean_name"] for d in select_candidates(deals)] == [
        "Chicken Legs", "Ground Beef", "Pork Chops"]


def test_items_without_a_per_kg_price_are_ranked_by_price_and_mixed_in_fairly():
    deals = [deal("Whole Chicken", 13.0, category="Meat & Seafood"),
             deal("Pork Tenderloin", 4.0, category="Meat & Seafood"),
             deal("Chicken Legs", 5.0, per_kg=4.4, category="Meat & Seafood"),
             deal("Steak", 20.0, per_kg=40.0, category="Meat & Seafood"),
             # $0.02/kg is a parsing artefact, so this one is ranked by its price.
             deal("Beef Burgers", 99.0, per_kg=0.02, category="Meat & Seafood")]
    names = [d["clean_name"] for d in select_candidates(deals)]
    assert names.index("Chicken Legs") < names.index("Steak")
    assert names.index("Pork Tenderloin") < names.index("Whole Chicken") < names.index("Beef Burgers")
    assert names[-1] in ("Steak", "Beef Burgers")


def test_cuisine_matches_move_to_the_front_of_their_bucket():
    deals = [deal("Ground Beef", 5.0, per_kg=5.0, category="Meat & Seafood"),
             deal("Pork Belly", 9.0, per_kg=15.0, category="Meat & Seafood"),
             deal("Sunrise Tofu", 2.5),
             deal("Carrots", 1.0, per_kg=1.0, category="Produce"),
             deal("Bok Choy", 3.0, per_kg=6.0, category="Produce")]
    plain = [d["clean_name"] for d in select_candidates(deals)]
    chinese = [d["clean_name"] for d in select_candidates(deals, ["Chinese"])]
    assert plain.index("Ground Beef") < plain.index("Pork Belly")
    assert plain.index("Carrots") < plain.index("Bok Choy")
    assert chinese.index("Pork Belly") < chinese.index("Ground Beef")
    assert chinese.index("Sunrise Tofu") < chinese.index("Ground Beef")
    assert chinese.index("Bok Choy") < chinese.index("Carrots")
    assert sorted(plain) == sorted(chinese)  # a boost reorders; it never adds or removes


def test_every_frontend_cuisine_works_and_empty_or_cheapest_means_no_boost():
    deals = many("Chicken", 30, "Meat & Seafood") + many("Carrots", 30, "Produce")
    baseline = select_candidates(deals)
    assert select_candidates(deals, None) == baseline
    assert select_candidates(deals, []) == baseline
    assert select_candidates(deals, ["Whatever is cheapest"]) == baseline
    assert select_candidates(deals, ["Not A Cuisine"]) == baseline
    for cuisine in FRONTEND_CUISINES:
        assert len(select_candidates(deals, [cuisine])) == len(baseline)
    assert set(CUISINE_KEYWORDS) == set(FRONTEND_CUISINES) - {"Whatever is cheapest"}


def test_deterministic_and_does_not_modify_the_input():
    deals = many("Chicken", 50, "Meat & Seafood") + many("Carrots", 50, "Produce")
    snapshot = [dict(d) for d in deals]
    assert select_candidates(deals, ["Korean"]) == select_candidates(list(reversed(deals)), ["Korean"])
    assert deals == snapshot


def test_duplicate_listings_are_sent_once():
    deals = [deal("Bok Choy", 1.99, category="Produce")] * 3
    assert len(select_candidates(deals)) == 1


# --- honest cuisine matching ----------------------------------------------------

def _named(*names):
    return [{"clean_name": n, "price": 2.0, "category": "Other"} for n in names]


def test_cuisine_note_appears_when_fewer_than_five_candidates_match():
    from ai.candidates import cuisine_match_count, cuisine_note

    few = _named("Sunrise Tofu", "Bok Choy", "Ground Beef", "Cheddar", "Tortillas", "Bananas")
    assert cuisine_match_count(few, ["Chinese"]) == 2
    assert cuisine_note(few, ["Chinese"], "FreshCo") == (
        "Not many Chinese staples on sale at FreshCo this week, so some meals lean on what's cheap."
    )
    assert "Chinese or Korean staples" in cuisine_note(few[:1], ["Chinese", "Korean"], "FreshCo")

    # Everyday items (pork, garlic, eggs, mushrooms) don't count as staples.
    assert cuisine_match_count(_named("Pork Chops", "Garlic Bread", "Large Eggs", "Mushrooms"), ["Chinese"]) == 0
    plenty = _named("Sunrise Tofu", "Bok Choy", "Egg Noodles", "Fresh Ginger", "Napa Cabbage")
    assert cuisine_note(plenty, ["Chinese"], "FreshCo") is None


def test_no_cuisine_note_without_a_real_cuisine():
    from ai.candidates import cuisine_note

    deals = _named("Ground Beef")
    assert cuisine_note(deals, [], "FreshCo") is None
    assert cuisine_note(deals, None, "FreshCo") is None
    assert cuisine_note(deals, ["Whatever is cheapest"], "FreshCo") is None


def test_every_frontend_cuisine_has_staples():
    from ai.candidates import CUISINE_KEYWORDS, CUISINE_STAPLES

    assert set(CUISINE_STAPLES) == set(CUISINE_KEYWORDS)


def test_dog_biscuits_liqueur_chocolates_and_sparkling_water_are_dropped():
    for name in ["Milk-Bone Real Salmon Dog Biscuits", "Maitre Truffout Brandy Beans", "Bubly Lime 12x355 mL"]:
        assert bucket_for({"clean_name": name, "category": "Other"}) is None


# --- one candidate per product ---------------------------------------------------

@pytest.mark.parametrize("name, products", [
    ("White Whole Mushrooms 227 G Product of Ontario, Mini Seedless Cucumbers or Baby Carrots",
     ["White Whole Mushrooms 227 G", "Mini Seedless Cucumbers", "Baby Carrots"]),
    ("Whole White or Brown Mushrooms 227 G Product of Ontario or Sunrise Firm Tofu",
     ["Whole White or Brown Mushrooms 227 G", "Sunrise Firm Tofu"]),
    ("Dempster's White or Whole Wheat Bread 675 G or Buns",
     ["Dempster's White or Whole Wheat Bread 675 G", "Buns"]),
    ("Carrots or Onions 3 Lb Product of Canada, Canada No. 1", ["Carrots", "Onions 3 Lb"]),
    ("Hunt's Tomato Paste 156 Ml or Old El Paso Seasonings",
     ["Hunt's Tomato Paste 156 Ml", "Old El Paso Seasonings"]),
    # one product in several varieties: left whole
    ("Aman's Brown or White Sugar", None),
    ("Chalo! Basmati or Signature Rice", None),
    ("Natrel Fine Filtered Milk 2 L Skim, 1 or 2%", None),
    ("Castello Havarti, Cheddar or Blue Cheese", None),
    ("Indian Eggplant Product of Usa or Mexico 3.28/kg", None),
    # flavours of a drink or sweet, not products
    ("Sherbon Mango or Guava Juice", None),
    ("Milk or Dark Chocolate Almonds", None),
    ("Bok Choy", None),
])
def test_listings_are_split_into_the_products_they_offer(name, products):
    from ai.candidates import split_listing

    assert split_listing(name) == (products or [name])


def test_each_product_becomes_its_own_candidate_with_the_listing_price():
    from ai.candidates import expand_listings

    listing = {"id": 7, "clean_name": "White Whole Mushrooms 227 G Product of Ontario, Mini Seedless Cucumbers or Baby Carrots",
               "price": 2.0, "price_per_kg": 8.81, "weight_kg": 0.227, "category": "Other"}
    single = {"id": 8, "clean_name": "Bok Choy", "price": 1.99, "price_per_kg": None, "category": "Produce"}
    mushrooms, cucumbers, carrots, bok = expand_listings([listing, single])

    assert [r["clean_name"] for r in (mushrooms, cucumbers, carrots)] == [
        "White Whole Mushrooms 227 G", "Mini Seedless Cucumbers", "Baby Carrots"]
    assert {r["price"] for r in (mushrooms, cucumbers, carrots)} == {2.0}
    assert {r["source_key"] for r in (mushrooms, cucumbers, carrots)} == {7}
    assert carrots["source_name"] == listing["clean_name"]
    assert mushrooms["weight_kg"] == 0.227                    # the size is printed on the mushrooms
    assert carrots["weight_kg"] is None and carrots["price_per_kg"] is None
    assert bok == {**single, "source_name": "Bok Choy", "source_key": 8}
    assert listing["clean_name"].startswith("White Whole")     # input untouched

    # Meat stays marked as meat so a vegetarian filter still catches every part.
    meat = expand_listings([{"clean_name": "Maple Leaf Bacon 375 G or Schneiders Wieners", "price": 4.0,
                             "category": "Meat & Seafood"}])
    assert [r["category"] for r in meat] == ["Meat & Seafood", "Meat & Seafood"]


# --- bulk packs for one or two people ---------------------------------------------

def _pack(name, kg, category="Produce", **extra):
    return {"clean_name": name, "price": 3.0, "price_per_kg": 3.0 / kg, "weight_kg": kg,
            "category": category, **extra}


def test_bulk_produce_and_pantry_packs_are_dropped_for_small_households():
    deals = [
        _pack("Red Onions 7 Lb", 3.17), _pack("Potatoes 5 Lb", 2.27), _pack("Carrots 3 Lb", 1.36),
        _pack("Baby Carrots 340 G", 0.34), _pack("Basmati Rice 4 Kg", 4.0, "Grains & Bread"),
        _pack("Spaghetti 900 G", 0.9, "Grains & Bread"),
        _pack("Whole Chicken", 2.0, "Meat & Seafood"),                      # protein: not affected
        _pack("Pork Shoulder", 3.0, "Meat & Seafood", priced_per="lb"),
    ]
    names = lambda people: {d["clean_name"] for d in select_candidates(deals, people=people)}

    assert names(1) == names(2) == {"Carrots 3 Lb", "Baby Carrots 340 G", "Spaghetti 900 G",
                                    "Whole Chicken", "Pork Shoulder"}
    assert names(3) == names(None) == {d["clean_name"] for d in deals}      # bigger households keep them


def test_bulk_packs_stay_when_the_category_has_nothing_smaller():
    deals = [_pack("Red Onions 7 Lb", 3.17), _pack("Potatoes 5 Lb", 2.27),
             _pack("Loose Bananas", 1.0, priced_per="lb")]
    assert len(select_candidates(deals[:2], people=1)) == 2                  # only bulk on sale: keep it
    assert [d["clean_name"] for d in select_candidates(deals, people=1)] == ["Loose Bananas"]
