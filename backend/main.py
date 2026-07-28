"""BudgetPlate FastAPI application entry point.

Wires together the API routes, rate limiting, CORS for the React frontend,
database initialisation, and the weekly background refresh job.

Run locally with:  uvicorn main:app --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db, init_pool
from rate_limit import RateLimitMiddleware
from scheduler import start_scheduler
from routes import budget, deals, health, items, scrape, stores

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budgetplate")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB + scheduler on startup; shut the pool down on exit."""
    init_pool()
    try:
        init_db()
    except Exception as exc:  # don't crash if DB isn't reachable at boot
        logger.warning("Database init skipped/failed: %s", exc)
    start_scheduler()
    yield


app = FastAPI(
    title="BudgetPlate API",
    description="Canadian grocery price comparison: search deals and plan a "
    "weekly shop around what's cheap this week, factoring in distance.",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow the React dev server and deployed frontend to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your Vercel domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

# Register API routes under /api.
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(scrape.router, prefix="/api", tags=["scrape"])
app.include_router(items.router, prefix="/api", tags=["search"])
app.include_router(stores.router, prefix="/api", tags=["stores"])
app.include_router(budget.router, prefix="/api", tags=["budget"])
app.include_router(deals.router, prefix="/api", tags=["deals"])


@app.get("/")
def root() -> dict:
    """Root pointer to the interactive API docs."""
    return {"service": "BudgetPlate API", "docs": "/docs"}
