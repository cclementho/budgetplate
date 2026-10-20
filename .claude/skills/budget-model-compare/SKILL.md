---
name: budget-model-compare
description: Rerun BudgetPlate's meal-plan (budget-plan) model comparison. Tests a candidate Claude model, with an optional output limit and effort, against the production model on the frozen flyer test cases. Verifies prices from Anthropic's pricing page, stops if the worst case costs over $1, checks every plan against the app's rules, and writes a summary with a recommendation. Use whenever the user asks whether a new or different Claude model (a new Haiku, Sonnet or Opus release) would be better, cheaper or faster for meal plans; wants to benchmark or re-test the budget-plan feature; asks "should we switch models?"; or wants the budget-plan test cases refreshed, even if they don't name this skill or the script.
---

# Budget-plan model comparison

The comparison script is `backend/scripts/compare_budget_models.py`, run from `backend/` with `.venv/bin/python`. Its `compare` command runs two setups side by side on the same test cases:

- **production:** read from the code, `config.CLAUDE_MODEL` plus `ai.budget.MAX_TOKENS`, with no effort setting. It always reflects what the checked-out branch would deploy.
- **candidate:** the model the user names, optionally with `--candidate-max-tokens` and `--candidate-effort`.

Every request is built by the production prompt code and sent the way production sends it. The two setups differ only in model, output limit and effort. Each run writes a folder under `backend/scripts/budget_eval/runs/<date>_<candidate>/`:

- `summary.md`: setup table, results table, violations, mechanical verdict, and a recommendation section for you to fill in;
- `results.csv`: one row per call;
- `violations.csv`: every violation and review flag;
- `responses.jsonl`: raw responses;
- `run.json`: the settings and prices used.

## Ground rules

These exist because the comparison is only useful if it is cheap, honest and comparable across months:

- **Test only.** Don't change production code or config. Don't commit, push or deploy. Recommend a switch; the user decides.
- **Prices come from the official page, not memory.** Model prices change and new models launch with new ones. A wrong price makes the cost conclusions wrong.
- **The $1 limit is the user's.** If the worst case is over `--max-cost` (default $1), stop and report the options. Raise the limit only when the user explicitly approves a number.
- **Use the frozen cases by default** (`budget_eval/cases.json`, Toronto M5S1Z9, flyer week 2026-10-08). New cases make results incomparable with earlier runs. Refresh them only when the user asks.

## Workflow

### 1. Pin down the candidate

You need the candidate model ID. Output limit and effort are optional:

- **Output limit:** leave it unset to use production's.
- **Effort:** leave it unset unless the user asks. Default effort is what production uses.

Note which branch is checked out (`git branch --show-current`), because production's setup is read from it.

### 2. Verify prices and the model ID

Fetch both pages with WebFetch. If WebFetch isn't available, use the `web-fetch` agent:

- https://platform.claude.com/docs/en/about-claude/pricing.md
- https://platform.claude.com/docs/en/about-claude/models/overview.md

Record, quoting the table rows:

- **The candidate's exact API model ID.** If it isn't listed, stop and tell the user.
- **Its base input and output price** per million tokens (standard API, not batch).
- **Any higher rate for long prompts.** Haiku 5.5, for example, charges more above 100,000 prompt tokens; pass that with `--candidate-price-over TOKENS IN OUT`.
- **Production's current prices.** The script has verified prices for Haiku 4.5 and 5.5 in its `PRICES` table. If production's model isn't there, or its price changed, pass `--production-price`.

Fetch tools sometimes summarise long pages wrongly, for example claiming a table is missing. Trust only the quoted rows.

Also check whether the candidate rejects anything the request sends. The request carries `model`, `max_tokens`, `system`, one user message, and `output_config.effort` only if you set effort. The `claude-api` skill's migration notes list per-model changes, such as which parameters a model now rejects. Newer models think by default, and **thinking counts toward the output limit**: a 4,096 limit cut off 7 of 10 Haiku 5.5 plans.

### 3. Dry run: estimate only

```bash
.venv/bin/python scripts/compare_budget_models.py compare --candidate MODEL \
  --candidate-price IN OUT --price-source https://platform.claude.com/docs/en/about-claude/pricing.md \
  [--candidate-max-tokens N] [--candidate-effort LEVEL] --dry-run
```

- **`--dry-run`** counts input tokens exactly with the free `count_tokens` endpoint and prices output at each setup's limit, as a worst case. It makes no model calls.
- **`--offline`** makes no API calls at all. It reuses token counts measured in earlier runs of the same prompts and otherwise assumes 1.5 characters per token, which deliberately overestimates.

If the script prints **STOP** (worst case over the limit), report the worst-case figure and the options:

- a lower `--candidate-max-tokens`, with the cut-off risk noted;
- `--limit N` to run fewer cases;
- a higher `--max-cost`, only with the user's approval.

### 4. Run

Run the same command without `--dry-run`. The script re-checks the estimate and refuses to spend over the limit. It makes 2 calls per case, about 15–30 s each, so run it in the background and wait for it to finish.

### 5. Review the results before believing them

The automatic checks are a first pass, and twice they have been wrong in ways that changed the story. So:

- **Check every violation against the flyer.** Look up the item in `budget_eval/cases.json` (`stores` → `clean_name` and `original_name`) before reporting it. Flyer listings bundle products ("Iögo Creamy 16x100 G or Siggi's Skyr Yogurt"), and several listings can share a price.
- **Skim the `item_name_reworded` review flags.** A flag pairing a basket item with an obviously different product means either a misnamed item or the checker matching the wrong listing.
- **If you fix the checker, re-score instead of re-running.** `compare --rescore scripts/budget_eval/runs/<run>` re-applies the checks without API calls. Tell the user what you changed.
- **Read the review flags** (possible meat or dairy in restricted plans, wrong stated totals) and mention the ones that matter.

### 6. Report

Fill in the **Recommendation** section of `summary.md`, then reply to the user with:

1. the source URL and the verified prices;
2. the results table, for both setups: valid plans, cost per call and per valid plan, latency (average and slowest), tokens, violations, and spend versus budget;
3. every violation, each with its example;
4. a plain-English recommendation with exact numbers: switch, don't switch, or test another setting, and why. Weigh reliability first (a plan that fails helps no one), then accuracy, then cost and latency;
5. the limits: 10 cases, one run each, one Toronto flyer week, and checks that are keyword-based.

## Refreshing the test cases (only when asked)

```bash
.venv/bin/python scripts/compare_budget_models.py build-cases
```

This takes the latest successful flyer batch for M5S1Z9 in the local warehouse and writes `budget_eval/cases_<flyer week>.json`. It never overwrites the frozen set unless you pass `--out scripts/budget_eval/cases.json --force`, and do that only if the user explicitly asks to replace it.

If the warehouse has no recent batch, load one with `.venv/bin/python weekly_refresh.py M5S1Z9`. That scrapes Flipp and writes to the local database, so confirm with the user first.

Then pass `--cases scripts/budget_eval/cases_<week>.json` to `compare`. Results on new cases aren't comparable with runs on the frozen set; say so when reporting. Both setups always run on the same cases within one run, so the comparison itself stays fair.

## Things to know

- **`cases.json` is not in git.** It's a flyer snapshot and the repo is public, so it lives only on this machine. If it's missing, the script says so. Ask the user to restore it rather than silently building new cases.
- **Old SDK:** the project pins `anthropic==0.42`, which predates the `output_config` argument, so the script sends effort through `extra_body`. Don't upgrade the SDK for a test.
- **The original study** (Haiku 4.5 vs 5.5, four setups) lives in `budget_eval/results.csv` and `violations.csv`. `compare` runs never touch those files.
