"""'Already at home' filtering: a deal is left out only when it IS the pantry item."""

import pytest

from routes.budget import _filter_deals, _is_pantry_item, _pantry_terms


def skipped(name, *pantry):
    return _is_pantry_item(name, _pantry_terms(list(pantry)))


@pytest.mark.parametrize("pantry, name, expected", [
    # butter vs butter cookies vs peanut butter
    ("Butter", "Gold Seal Butter", True),
    ("Butter", "Gay Lea Butter Sticks", True),
    ("Butter", "Lactantia Salted Butter 454 G", True),
    ("Butter", "Danish Butter Cookies", False),
    ("Butter", "Kraft Peanut Butter, 750 G", False),
    ("Butter", "Butter Chicken", False),
    ("Butter", "Butternut Squash", False),
    # rice vs rice crackers vs rice vinegar
    ("Rice", "Chalo! Basmati Rice", True),
    ("Rice", "Minute Rice", True),
    ("Rice", "Handi Sona Masoori Rice 8 Lb Selected Varieties", True),
    ("Rice", "Rice Crackers", False),
    ("Rice", "Rice Vinegar", False),
    ("Rice", "Rice Noodles", False),
    ("Rice", "Chicken Fried Rice", False),
    ("Rice", "Ground Beef Price Drop", False),
    # soy sauce
    ("Soy sauce", "Kikkoman Soy Sauce", True),
    ("Soy sauce", "Soy Beverage", False),
    ("Soy sauce", "Classico Pasta Sauce", False),
    # eggs vs egg noodles
    ("Eggs", "Large Grade a White Eggs", True),
    ("Eggs", "Large Eggs", True),
    ("Eggs", "Egg Noodles", False),
    ("Eggs", "Eggplant", False),
    ("Eggs", "Cadbury Mini Eggs", False),
    # sugar vs sugar snap peas
    ("Sugar", "Lantic Granulated Sugar", True),
    ("Sugar", "Sugar Snap Peas", False),
    ("Sugar", "Heinz Ketchup Zero Sugar", False),
    ("Butter", "I Can't Believe It's Not Butter!", False),
    # other staples from the form
    ("Milk", "Neilson Trutaste Milk", True),
    ("Milk", "Neilson Chocolate Milk or 18% Cream", False),
    ("Milk", "Grace Coconut Milk", False),
    ("Onions", "Red Onions 7 Lb Product of Ontario, Canada No. 1", True),
    ("Onions", "Green Onions", False),
    ("Onions", "Onion Rings", False),
    ("Garlic", "Garlic Sausage", False),
    ("Cooking oil", "Mazola Canola Oil", True),
    ("Cooking oil", "Bertolli Olive Oil", False),
])
def test_only_products_that_are_the_pantry_item_are_left_out(pantry, name, expected):
    assert skipped(name, pantry) is expected


@pytest.mark.parametrize("pantry, name, expected", [
    # kept: the other alternative is food the shopper doesn't have
    (["Milk"], "Aman's Milk or Suji Rusk", False),
    (["Bread"], "Dempster's White or Whole Wheat Bread 675 G or Buns", False),
    (["Onions"], "Carrots or Onions 3 Lb Product of Canada, Canada No. 1", False),
    (["Onions"], "Carrots or Red Onions", False),
    (["Soy sauce"], "Lady's Choice Sandwich Spread 470 Ml or Silver Swan Soy Sauce", False),
    (["Butter"], "Lactantia Butter Sticks, Kraft Parmesan Shredded Cheese", False),
    (["Bread"], "Christie Cookies 224-303 G or Triscuit Crackers", False),
    # skipped: every alternative is at home ("Basmati or Signature Rice" is two rices)
    (["Rice"], "Chalo! Basmati or Signature Rice", True),
    (["Sugar"], "Aman's Brown or White Sugar", True),
    (["Bread"], "Dempster's White or Whole Wheat Bread 675 G", True),
    (["Rice", "Sugar"], "Jasmine Rice or Brown Sugar", True),
    (["Onions", "Garlic"], "Onions or Garlic", True),
    # skipped: the only other alternative isn't food the plan would use
    (["Butter"], "Sainsbury All Butter or Salted Fudge", True),
])
def test_x_or_y_listings_are_left_out_only_when_every_alternative_is_at_home(pantry, name, expected):
    assert skipped(name, *pantry) is expected


def test_matching_ignores_case_and_plurals_both_ways():
    assert skipped("LARGE EGGS", "eggs") and skipped("Large Egg", "Eggs") and skipped("Large Eggs", "egg")
    assert not skipped("Bok Choy", "", "  ")  # blank entries match nothing


def test_filter_reports_what_was_left_out_and_keeps_lookalikes():
    deals = [
        {"clean_name": "Gold Seal Butter", "category": "Dairy & Eggs"},
        {"clean_name": "Danish Butter Cookies", "category": "Dairy & Eggs"},
        {"clean_name": "Chalo! Basmati Rice", "category": "Grains & Bread"},
        {"clean_name": "Rice Vinegar", "category": "Grains & Bread"},
        {"clean_name": "Gold Seal Butter", "category": "Dairy & Eggs"},   # same listing twice
        {"clean_name": "Tide Detergent", "category": "Household"},
    ]
    kept, left_out = _filter_deals(deals, None, ["Rice", "Butter", "Sugar", "Soy sauce"])
    assert [d["clean_name"] for d in kept] == ["Danish Butter Cookies", "Rice Vinegar"]
    assert left_out == ["Gold Seal Butter", "Chalo! Basmati Rice"]
