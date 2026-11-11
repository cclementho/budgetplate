"""The deterministic shortlist of deals sent to Claude (ai/candidates.py)."""

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
