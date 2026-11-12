# Budget-plan model comparison: claude-haiku-5-5 and claude-sonnet-5-5 vs claude-haiku-4-5

- Date: 2026-11-11  |  cases: `budget_eval/cases.json` (flyer week 2026-10-08, 10 cases, postal code M5S1Z9)
- Prices verified from https://platform.claude.com/docs/en/about-claude/pricing.md on 2026-11-11
- Each case ran once per setup, so run-to-run variation is not measured.

| Setup | Model | Output limit | Effort | Input $/MTok | Output $/MTok |
|---|---|---|---|---|---|
| production | `claude-haiku-4-5` | 4,096 | default | $1.00 | $5.00 |
| candidate | `claude-haiku-5-5` | 16,000 | default | $0.10 | $0.50 |
| candidate2 | `claude-sonnet-5-5` | 16,000 | default | $2.00 | $10.00 |

## Results

| | production (claude-haiku-4-5) | candidate (claude-haiku-5-5) | candidate2 (claude-sonnet-5-5) |
|---|---|---|---|
| Valid plans | 8/10 | 7/10 | 10/10 |
| Stop reasons | end_turn 9, error 1 | end_turn 7, max_tokens 2, error 1 | end_turn 10 |
| Avg cost per call | $0.01054 | $0.00628 | $0.10337 |
| Cost per valid plan | $0.01185 | $0.00807 | $0.10337 |
| Total spent | $0.0948 | $0.0565 | $1.0337 |
| Avg latency (all / valid only) | 11.0 s / 10.4 s | 51.0 s / 44.8 s | 68.2 s / 68.2 s |
| Slowest call | 15.6 s | 73.0 s | 107.7 s |
| Avg tokens in / out (out includes thinking) | 3,386 / 1,220 | 4,357 / 10,428 | 4,890 / 9,359 |
| Rule violations | 10 | 2 | 4 |
| Plans with at least one violation | 6 | 2 | 4 |
| Avg spend vs budget (valid plans) | 74% | 84% | 98% |
| Avg servings vs target (valid plans) | 113% | 100% | 99% |

### Violations by type

| Type | production (claude-haiku-4-5) | candidate (claude-haiku-5-5) | candidate2 (claude-sonnet-5-5) |
|---|---|---|---|
| item_not_on_flyer | 0 | 0 | 0 |
| price_not_on_flyer | 0 | 0 | 0 |
| meal_ingredient_not_in_basket | 3 | 0 | 0 |
| pantry_item_in_basket | 0 | 0 | 0 |
| over_budget | 2 | 0 | 0 |
| non_food_item | 0 | 0 | 0 |
| not_vegetarian | 0 | 0 | 0 |
| contains_dairy | 0 | 0 | 0 |
| meal_name_ingredient_missing | 5 | 0 | 0 |
| bulk_pack_small_household | 0 | 0 | 0 |
| one_deal_as_multiple_products | 0 | 0 | 1 |
| breakfast_potato_or_raw_onion | 0 | 2 | 3 |

### Every violation

- **production**, case01, `over_budget`: total $42.78 > budget $40 (swap_suggestion: None)
- **production**, case02, `meal_name_ingredient_missing`: 'Potato and cauliflower curry' names curry but uses ['White potatoes', 'Cauliflower', 'Vegetable broth', 'Diced tomatoes', 'cooking oil']
- **production**, case03, `meal_name_ingredient_missing`: 'Ground beef and potato curry with rice' names curry but uses ['Ground beef and pork', 'White potatoes', 'Basmati rice', 'Broth', 'Vegetable oil', 'Diced tomatoes', 'salt', 'pepper', 'water']
- **candidate**, case05, `breakfast_potato_or_raw_onion`: breakfast 'Potato and egg scramble with toast' contains potatoes: ['Yellow potatoes (10 lb bag)', 'White sandwich bread (675 g)']
- **candidate2**, case05, `breakfast_potato_or_raw_onion`: breakfast 'Potato, pepper and egg breakfast hash' contains potatoes: ['Yellow potatoes 10 lb bag', 'Green peppers', 'Margarine']
- **candidate2**, case06, `breakfast_potato_or_raw_onion`: breakfast 'Bacon and Pepper Breakfast Hash' contains potatoes: ['Bacon', 'Breakfast potatoes', 'Rainbow peppers']
- **candidate**, case06, `breakfast_potato_or_raw_onion`: breakfast 'Bacon and potato breakfast hash' contains potatoes: ['Schneiders bacon', 'Breakfast potatoes']
- **production**, case06, `meal_name_ingredient_missing`: 'Scrambled Eggs & Toast' names egg but uses ['Protein Bread']
- **production**, case07, `meal_ingredient_not_in_basket`: 'Scrambled Eggs with Toast' uses 'eggs', which is not on the shopping list
- **production**, case07, `meal_ingredient_not_in_basket`: 'Vegetable Potato Curry with Rice' uses 'turmeric', which is not on the shopping list
- **production**, case07, `meal_ingredient_not_in_basket`: 'Vegetable Potato Curry with Rice' uses 'cumin', which is not on the shopping list
- **production**, case07, `meal_name_ingredient_missing`: 'Bean and Cheese Quesadillas' names beans but uses ['Dempster\'s 7" Tortillas', 'Green Giant Frozen Vegetables', 'salt', 'pepper']
- **production**, case07, `meal_name_ingredient_missing`: 'Bean and Cheese Quesadillas' names cheese but uses ['Dempster\'s 7" Tortillas', 'Green Giant Frozen Vegetables', 'salt', 'pepper']
- **production**, case08, `over_budget`: total $40.57 > budget $40 (swap_suggestion: None)
- **candidate2**, case09, `one_deal_as_multiple_products`: 'Frozen mixed vegetables' @ $3.0: one line of the listing 'Green Giant Vegetables 400-750 G or Mccain Frozen Potato Products'
- **candidate2**, case10, `breakfast_potato_or_raw_onion`: breakfast 'Sweet potato and onion hash with fried egg' contains potatoes: ['Sweet potatoes', 'Yellow onions (3 lb bag)', 'Eggs']

### Review flags (not violations; check by hand)

- production: meal_uses_pantry_item 26, maybe_not_vegetarian 7, maybe_contains_dairy 8
- candidate: meal_uses_pantry_item 31, maybe_contains_dairy 8
- candidate2: meal_uses_pantry_item 38, maybe_not_vegetarian 5, maybe_contains_dairy 7
  - candidate2, case02, `maybe_not_vegetarian`: meal 'Grilled cheese sandwiches with creamy tomato soup'
  - candidate2, case02, `maybe_not_vegetarian`: meal 'Cheesy potato soup'
  - production, case02, `maybe_not_vegetarian`: basket 'Vegetable broth'
  - production, case02, `maybe_not_vegetarian`: meal ingredient 'Vegetable broth'
  - production, case02, `maybe_not_vegetarian`: meal ingredient 'Vegetable broth'
  - production, case02, `maybe_not_vegetarian`: meal 'Squash and herb soup'
  - production, case02, `maybe_not_vegetarian`: meal ingredient 'Vegetable broth'
  - production, case03, `maybe_contains_dairy`: basket 'Bread'
  - production, case03, `maybe_contains_dairy`: basket 'Pasta sauce'
  - production, case03, `maybe_contains_dairy`: meal ingredient 'Bread'
  - production, case03, `maybe_contains_dairy`: meal ingredient 'Pasta sauce'
  - candidate, case03, `maybe_contains_dairy`: basket 'Pasta sauce'
  - candidate, case03, `maybe_contains_dairy`: basket 'Bread loaf'
  - candidate, case03, `maybe_contains_dairy`: meal ingredient 'Bread loaf'
  - candidate, case03, `maybe_contains_dairy`: meal ingredient 'Pasta sauce'
  - candidate2, case03, `maybe_contains_dairy`: basket 'Classico pasta sauce'
  - candidate2, case03, `maybe_contains_dairy`: basket 'Country Harvest bread'
  - candidate2, case03, `maybe_contains_dairy`: meal ingredient 'Country Harvest bread'
  - candidate2, case03, `maybe_contains_dairy`: meal ingredient 'Classico pasta sauce'
  - candidate2, case04, `maybe_not_vegetarian`: meal 'Minestrone soup'
  - candidate2, case04, `maybe_not_vegetarian`: meal 'Grilled cheese with fresh tomato soup'
  - candidate2, case04, `maybe_not_vegetarian`: meal 'Butternut squash and carrot soup with toast'
  - candidate2, case06, `maybe_contains_dairy`: basket 'Pasta sauce'
  - candidate2, case06, `maybe_contains_dairy`: meal ingredient 'Pasta sauce'
  - candidate2, case06, `maybe_contains_dairy`: meal ingredient 'Pasta sauce'
  - candidate, case06, `maybe_contains_dairy`: basket 'Raisin bread'
  - candidate, case06, `maybe_contains_dairy`: basket 'Pasta sauce'
  - candidate, case06, `maybe_contains_dairy`: meal ingredient 'Raisin bread'
  - candidate, case06, `maybe_contains_dairy`: meal ingredient 'Pasta sauce'
  - production, case06, `maybe_contains_dairy`: basket 'Pasta Sauce'
  - production, case06, `maybe_contains_dairy`: basket 'Protein Bread'
  - production, case06, `maybe_contains_dairy`: meal ingredient 'Protein Bread'
  - production, case06, `maybe_contains_dairy`: meal ingredient 'Pasta Sauce'
  - production, case07, `maybe_not_vegetarian`: basket "Campbell's Broth"
  - production, case07, `maybe_not_vegetarian`: meal ingredient "Campbell's Broth"

### API errors

- BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CgxUD6NVWCDvBTh3F4Nv7'}
- BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CgxUCLX1BD7PwTDWyfotq'}

## Mechanical verdict

- candidate (claude-haiku-5-5): FAIL reliability: candidate 7/10 valid vs production 8/10
- candidate (claude-haiku-5-5): PASS accuracy: candidate 2 rule violation(s) vs production 10
- candidate (claude-haiku-5-5): INFO cost per valid plan: candidate $0.0081 vs production $0.0119 (0.68x)
- candidate (claude-haiku-5-5): INFO latency: candidate avg 51.0 s (max 73.0 s) vs production avg 11.0 s (max 15.6 s)
- candidate2 (claude-sonnet-5-5): PASS reliability: candidate 10/10 valid vs production 8/10
- candidate2 (claude-sonnet-5-5): PASS accuracy: candidate 4 rule violation(s) vs production 10
- candidate2 (claude-sonnet-5-5): INFO cost per valid plan: candidate $0.1034 vs production $0.0119 (8.72x)
- candidate2 (claude-sonnet-5-5): INFO latency: candidate avg 68.2 s (max 107.7 s) vs production avg 11.0 s (max 15.6 s)

## Recommendation

**Stay on claude-haiku-4-5 for now, and fix its failures in Python rather than switching.**

- Sonnet 5.5 wrote the best plans (10/10 valid, spends 98% of budget, 99% of servings; its only real
  violations are three breakfast potato hashes, and the one "one deal as multiple products" is a checker
  false positive). But it averaged 68 s per plan (slowest 108 s) and costs $0.103 per plan, 8.7x Haiku 4.5.
  At default (high) effort it thinks for most of that time before writing, so streaming would not hide the wait.
- Haiku 5.5 at 16,000 tokens was cut off in 2 of 9 calls and averaged 51 s. Not usable at default effort.
- Haiku 4.5 is fast (11 s) and cheap ($0.012) but made 10 real violations in 8 valid plans and one reply
  was unusable (it wrote the JSON, then "Wait, I need to recalculate" and a second JSON).

Caveats: case10 failed for both Haiku models because the API account ran out of credit mid-run, so they have
9 cases each. One run per case. The meal-name and breakfast checks are keyword-based.

Worth testing next (needs credit): Sonnet 5.5 and Haiku 5.5 at low effort.
