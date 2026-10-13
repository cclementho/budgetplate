"""Application configuration loaded from environment variables.

All secrets and connection strings live in ``backend/.env`` (never committed).
Import ``settings`` anywhere in the backend to read configuration.
"""

import os
from dotenv import load_dotenv

# Load backend/.env regardless of the current working directory.
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_PATH)


class Settings:
    """Typed accessors for environment-driven configuration."""

    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://localhost/budgetplate")
    RATE_LIMIT_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))

    # Claude Haiku is only called for /api/budget-plan (weekly basket + meal
    # ideas). Scraping/parsing is pure Python — no AI involved.
    CLAUDE_MODEL: str = "claude-haiku-4-5"

    # Cache window: re-use scraped data younger than this many hours.
    CACHE_HOURS: int = 24

    # Run the in-process weekly refresh (APScheduler). For local dev only:
    # in prod the GitHub Actions workflow is the source of truth, so this
    # defaults to off and render.yaml keeps it off.
    ENABLE_SCHEDULER: bool = os.getenv("ENABLE_SCHEDULER", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }

    # Stores the Flipp scraper supports.
    SUPPORTED_MERCHANTS = ["No Frills", "FreshCo", "Walmart", "Loblaws"]


settings = Settings()
