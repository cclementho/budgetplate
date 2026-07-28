# 🛒 BudgetPlate

**Find the cheapest groceries near you this week — built for Canadian students and budget shoppers without a car.**

BudgetPlate pulls real weekly flyer deals from major Canadian grocery chains, cleans them with AI, and ranks them by **price per kg _and_ distance**. The cheapest store 45 minutes away loses to a slightly pricier one 10 minutes on foot.

**One entry point:** enter your postal code, weekly budget, and household size. BudgetPlate finds the best-value store near you and builds a results page around what's actually on sale this week:

- **Worth buying this week** — an AI-curated weekly shop built from real deals, with simple meal ideas and a shopping list checked against your budget.
- **This week's deals** — the store's full deal list, filterable by keyword.
- **Search a specific item** — a small secondary lookup at the bottom of the page for when you're after one thing in particular.

---

## Tech stack

| Layer        | Tech                                                            |
| ------------ | -------------------------------------------------------------- |
| Backend      | FastAPI (Python)                                              |
| Frontend     | React + Tailwind CSS + Recharts                              |
| AI           | Anthropic Claude Haiku (`anthropic` Python SDK)              |
| Database     | PostgreSQL (local now, Supabase-ready)                       |
| Location     | OpenStreetMap — Nominatim geocoding + Overpass (no API key)  |
| Data source  | [Flipp scraper](https://github.com/Kiizon/flippscrape) (integrated) |
| Deployment   | Render (backend) + Vercel (frontend)                         |

---

## Project structure

```
budgetplate/
├── backend/
│   ├── main.py                # FastAPI app + middleware + scheduler
│   ├── config.py              # env-driven settings
│   ├── database.py            # psycopg2 pool + schema init
│   ├── schema.sql             # flyer_items table
│   ├── queries.py             # shared DB reads
│   ├── rate_limit.py          # per-IP rate limiter middleware
│   ├── scheduler.py           # APScheduler weekly refresh
│   ├── scraper/
│   │   ├── flipp.py           # integrated Flipp scraper (no key needed)
│   │   ├── parser.py          # pure-Python parser: weights, categories, names
│   │   └── pipeline.py        # scrape → parse → store, with 24h cache
│   ├── ai/
│   │   ├── client.py          # shared Anthropic client + JSON parsing
│   │   ├── search.py          # fuzzy item match + substitutes (pure Python)
│   │   └── budget.py          # weekly basket generation (the only Claude call)
│   ├── location/
│   │   └── stores.py          # Nominatim + Overpass + distance scoring
│   └── routes/                # /api endpoints
├── frontend/
│   └── src/
│       ├── api.js             # API client (never sees the API key)
│       ├── App.jsx            # view router
│       └── components/        # Home, Results, BudgetShop, ItemSearch …
├── render.yaml                # Render blueprint
└── README.md
```

---

## Prerequisites

- **Python 3.10+**
- **Node.js 18+**
- **PostgreSQL** running locally (or a Supabase connection string)
- An **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com))

---

## Backend setup

```bash
cd backend

# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
#   then edit .env and set:
#     ANTHROPIC_API_KEY=sk-ant-...
#     DATABASE_URL=postgresql://localhost/budgetplate
#     RATE_LIMIT_PER_MINUTE=10

# 4. Create the database (local Postgres)
createdb budgetplate
#   The table schema is applied automatically on app startup.

# 5. Run the API
uvicorn main:app --reload
```

The API runs at **http://localhost:8000**. Interactive docs: **http://localhost:8000/docs**.

> On first request for a postal code, the backend scrapes Flipp and parses the
> items with a pure-Python parser (a few seconds, no AI calls). Results are
> cached for 24h, so subsequent requests are instant.

---

## Frontend setup

```bash
cd frontend

# 1. Install dependencies
npm install

# 2. (optional) configure the API base
cp .env.example .env
#   Leave VITE_API_BASE blank for local dev — Vite proxies /api to :8000.
#   In production, set it to your Render backend URL.

# 3. Run the dev server
npm run dev
```

The app runs at **http://localhost:5173**.

---

## API endpoints

| Method | Path                                                       | Description                                   |
| ------ | ---------------------------------------------------------- | --------------------------------------------- |
| GET    | `/api/health`                                              | Liveness check                                |
| POST   | `/api/scrape`                                              | Trigger scrape for a postal code (24h cache)  |
| GET    | `/api/items?postal_code=V7C4V9&query=chicken+breast`       | Search items, ranked by price-per-kg + distance |
| GET    | `/api/stores?postal_code=V7C4V9`                           | Nearby supported stores with distance + deals  |
| POST   | `/api/budget-plan`                                         | Build a weekly shop from a store's deals       |
| GET    | `/api/deals?postal_code=V7C4V9&merchant=No+Frills`         | All deals at a store this week                 |

Example:

```bash
curl "http://localhost:8000/api/stores?postal_code=M5V3L9"

curl -X POST http://localhost:8000/api/budget-plan \
  -H "Content-Type: application/json" \
  -d '{"postal_code":"M5V3L9","merchant":"No Frills","budget":60,"people":2,"frequency":"Weekly","restrictions":["Vegetarian"],"dislikes":["mushrooms"]}'
```

---

## How it works

### Data pipeline (`backend/scraper/pipeline.py`)

1. Runs the integrated Flipp scraper for a postal code (No Frills, FreshCo, Walmart, Loblaws grocery flyers only).
2. Filters out items with no price.
3. Parses every item with a **pure-Python parser** (`scraper/parser.py`) — no AI calls: regex weight extraction (g/kg/lb/ml/L, ranges like `252/336 g`, count packs like `8's`), per-kg / per-unit pricing, word-boundary keyword categorization, and brand/size-stripped display names. ~1200 items parse in well under a second; the whole pipeline finishes in a few seconds (network time only).
4. Stores cleaned rows in PostgreSQL.
5. **24h cache** — if a postal code was scraped less than a day ago, cached data is returned instead of re-scraping.

**Claude Haiku is only called in `/api/budget-plan`** — to build the weekly basket and meal ideas from the already-parsed deals. Scraping, parsing, and item search are all deterministic Python.

A background **APScheduler** job re-runs the pipeline every **Wednesday at 6am** (when Canadian flyers reset) for any postal code searched in the last 30 days.

### Store location engine (`backend/location/stores.py`)

- Geocodes the postal code with Nominatim (structured postal-code search), falling back to an FSA-level centroid via the free, keyless [Zippopotam.us](https://zippopotam.us) API — OSM's Canadian postal coverage is patchy, so many valid codes (e.g. much of BC) aren't in Nominatim.
- Queries Overpass for supermarkets, grocery, and convenience stores within 5km (widening to 10km if no supported chain is found), rotating across Overpass mirrors to ride out rate limits.
- Matches store names against the supported chains by case-insensitive substring (`no frills`, `freshco`, `walmart`, `loblaws`, plus `superstore` / `real canadian` → Loblaws).
- Computes walking distance/time (5 km/h) and a walkability band: walkable (<1km), short transit (1–3km), far (3km+).
- Ranks by a composite score blending a per-merchant price index with distance.
- If no supported chain is within 10km, returns **all** nearby grocery stores with a note that deal data is limited to the four supported chains.

### Security

- The **Anthropic API key lives only on the backend** — the frontend calls FastAPI, never Anthropic directly.
- `.env` is git-ignored; `.env.example` documents the required variables.
- A per-IP **rate limiter** caps requests at `RATE_LIMIT_PER_MINUTE`.

---

## Deployment

### Backend → Render

The included `render.yaml` provisions the web service **and** a free Postgres database. In the Render dashboard:

1. New → Blueprint → select this repo.
2. Set the `ANTHROPIC_API_KEY` secret.
3. Deploy. `DATABASE_URL` is wired automatically.

### Frontend → Vercel

1. Import the repo into Vercel, set the project root to `frontend/`.
2. Set `VITE_API_BASE` to your Render backend URL (e.g. `https://budgetplate-api.onrender.com`).
3. Deploy. `frontend/vercel.json` configures the Vite build.

### Supabase instead of local Postgres

Just point `DATABASE_URL` at your Supabase connection string — the schema in `schema.sql` applies on startup, no other changes needed.

---

## Notes & limits

- Supported stores: **No Frills, FreshCo, Walmart, Loblaws** (the chains the Flipp scraper covers).
- For educational use; flyer data belongs to Flipp and its partners. Use responsibly and respect rate limits.
- Phase 2 ideas (not built yet): accounts, saved lists, deal notifications, price-history tracking, more stores.
