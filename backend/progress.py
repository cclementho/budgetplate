"""In-memory scrape-progress tracking, keyed by postal code.

The pipeline records how many items it has cleaned so far during a scrape, and
the /api/scrape/status endpoint reads it to show "Processing X of Y items…".
This is a simple process-local dict — fine for a single instance; use a shared
store (e.g. Redis) if you scale horizontally.
"""

import threading

_lock = threading.Lock()
_progress: dict[str, dict] = {}  # postal -> {"processed": int, "total": int}


def _norm(postal_code: str) -> str:
    return postal_code.replace(" ", "").upper()


def start(postal_code: str, total: int) -> None:
    """Begin tracking a scrape with a known total item count."""
    with _lock:
        _progress[_norm(postal_code)] = {"processed": 0, "total": total}


def update(postal_code: str, processed: int) -> None:
    """Record how many items have been processed so far."""
    with _lock:
        entry = _progress.get(_norm(postal_code))
        if entry is not None:
            entry["processed"] = processed


def get(postal_code: str) -> dict | None:
    """Return {"processed", "total"} for a postal code, or None if not tracked."""
    with _lock:
        entry = _progress.get(_norm(postal_code))
        return dict(entry) if entry is not None else None


def clear(postal_code: str) -> None:
    """Stop tracking a postal code (call when the scrape finishes)."""
    with _lock:
        _progress.pop(_norm(postal_code), None)
