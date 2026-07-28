"""Health-check endpoint."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """Liveness probe. Returns a static OK payload for uptime checks."""
    return {"status": "ok", "service": "budgetplate"}
