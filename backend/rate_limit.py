"""Simple in-memory per-IP rate limiter middleware.

Caps requests per client IP to RATE_LIMIT_PER_MINUTE. This is intentionally
lightweight (a sliding window in memory) — fine for a single-instance
deployment. For multi-instance scaling, swap the store for Redis.
"""

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import settings

WINDOW_SECONDS = 60

# Endpoints exempt from rate limiting. The scrape-status endpoint is a cheap
# DB read that the frontend polls every 5s while a scrape runs, so throttling it
# would break the loading UI with 429s.
EXEMPT_PATHS = {"/api/scrape/status"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter keyed by client IP."""

    def __init__(self, app, limit: int | None = None):
        super().__init__(app)
        self.limit = limit or settings.RATE_LIMIT_PER_MINUTE
        self._hits: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        # Only throttle the API surface; let health/docs/static and exempt
        # polling endpoints through.
        path = request.url.path
        if not path.startswith("/api/") or path in EXEMPT_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        hits = self._hits[client_ip]

        # Drop timestamps outside the window.
        while hits and hits[0] <= now - WINDOW_SECONDS:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = int(WINDOW_SECONDS - (now - hits[0])) + 1
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded. Please slow down.",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)
