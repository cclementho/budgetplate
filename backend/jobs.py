"""Background scrape jobs.

The first search for a postal code needs a full scrape+clean (30-60s), which is
too slow to do inside a request. This module runs that work on a daemon thread
and tracks which postal codes are currently being scraped, so repeated searches
while a scrape is in flight don't spawn duplicate jobs.
"""

import threading

from scraper.pipeline import run_pipeline

_lock = threading.Lock()
_in_progress: set[str] = set()


def _normalize(postal_code: str) -> str:
    return postal_code.replace(" ", "").upper()


def is_scraping(postal_code: str) -> bool:
    """True if a background scrape for this postal code is currently running."""
    with _lock:
        return _normalize(postal_code) in _in_progress


def start_background_scrape(postal_code: str) -> bool:
    """Kick off a background scrape for the postal code.

    Returns True if a new job was started, False if one was already running.
    """
    pc = _normalize(postal_code)
    with _lock:
        if pc in _in_progress:
            return False
        _in_progress.add(pc)

    def _worker() -> None:
        try:
            run_pipeline(pc)
        finally:
            with _lock:
                _in_progress.discard(pc)

    threading.Thread(target=_worker, daemon=True).start()
    return True
