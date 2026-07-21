"""
scraper.py — Scrape TV shows from sflix.film and save results to JSON files.

API:  POST https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter
Site: https://sflix.film

Output files (written to CWD = repo root when run by GitHub Actions):
  sflix_tv_shows.json             — full merged dataset (grows across runs)
  sflix_tv_shows_latest.json      — only shows fetched in this run
  sflix_tv_shows_YYYY-MM-DD.json  — dated snapshot for this run

Dependencies: requests  (+ stdlib only)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL  = "https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter"
BASE_URL = "https://sflix.film"
DETAIL_BASE = f"{BASE_URL}/detail/"

# channelId=2 → TV Shows  |  sort=ForYou gives the default ranked feed
CHANNEL_ID  = 2
SORT_METHOD = "ForYou"
PER_PAGE    = 28          # matches what the real site sends

# Number of pages (overridden by env var SCRAPER_PAGES set in the workflow)
DEFAULT_PAGES = int(os.environ.get("SCRAPER_PAGES", "10"))

# Polite delay between API calls (seconds)
REQUEST_DELAY   = 1.2
REQUEST_TIMEOUT = 20

# Retry settings
MAX_RETRIES   = 3
RETRY_BACKOFF = 3   # seconds × attempt number

# Output filenames
OUT_FULL   = "sflix_tv_shows.json"
OUT_LATEST = "sflix_tv_shows_latest.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(
    {
        "Content-Type":   "application/json",
        "Accept":         "application/json",
        "x-request-lang": "en",
        "Origin":         BASE_URL,
        "Referer":        f"{BASE_URL}/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
    }
)

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def fetch_page(page: int, attempt: int = 1) -> list[dict[str, Any]]:
    """
    POST to the filter API for *page* and return the list of raw show dicts.
    Returns [] on unrecoverable error.
    """
    payload = {
        "page":      str(page),   # API expects a string, not an int
        "perPage":   PER_PAGE,
        "channelId": CHANNEL_ID,
        "sort":      SORT_METHOD,
    }

    try:
        resp = SESSION.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.warning("HTTP %s on page %d (attempt %d/%d)", status, page, attempt, MAX_RETRIES)
        return _retry(fetch_page, page, attempt)
    except requests.exceptions.RequestException as exc:
        log.warning("Request error on page %d: %s (attempt %d/%d)", page, exc, attempt, MAX_RETRIES)
        return _retry(fetch_page, page, attempt)
    except ValueError as exc:
        log.warning("JSON decode error on page %d: %s", page, exc)
        return []

    # API-level error (code != 0)
    if data.get("code") != 0:
        log.warning("API error on page %d: %s", page, data.get("message", "unknown"))
        return []

    items = data.get("data", {}).get("items", [])
    log.info("Page %d → %d show(s)", page, len(items))
    return items


def _retry(fn, page: int, attempt: int) -> list[dict[str, Any]]:
    """Back-off and retry *fn(page)* if attempts remain, else return []."""
    if attempt < MAX_RETRIES:
        wait = RETRY_BACKOFF * attempt
        log.info("Retrying page %d in %ds …", page, wait)
        time.sleep(wait)
        return fn(page, attempt + 1)
    log.error("Giving up on page %d after %d attempts.", page, MAX_RETRIES)
    return []


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def enrich(show: dict[str, Any]) -> dict[str, Any]:
    """
    Add derived fields to a raw API item so consumers don't have to.
    Non-destructive: original keys are preserved unchanged.
    """
    show["full_url"] = DETAIL_BASE + show.get("detailPath", "")
    return show


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------


def scrape_all(num_pages: int) -> list[dict[str, Any]]:
    """
    Fetch *num_pages* pages from the API.
    Stops early if a page returns 0 items (end of catalogue reached).
    Returns a deduplicated list of enriched show dicts.
    """
    # Dedup by detailPath (stable unique key from the API)
    seen: dict[str, dict[str, Any]] = {}

    for page in range(1, num_pages + 1):
        items = fetch_page(page)

        if not items:
            log.info("Page %d returned 0 items — stopping early.", page)
            break

        for raw in items:
            key = raw.get("detailPath") or raw.get("subjectId") or raw.get("title", "")
            seen[key] = enrich(raw)

        if page < num_pages:
            time.sleep(REQUEST_DELAY)

    return list(seen.values())


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------


def load_existing(path: str) -> list[dict[str, Any]]:
    """Load previously saved shows from *path*, or [] if missing/corrupt."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        # Support both bare list and our wrapped {meta, shows} format
        if isinstance(data, dict) and "shows" in data:
            return data["shows"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s) — starting fresh.", path, exc)
    return []


def merge(existing: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge *fresh* into *existing*.
    Fresh data wins on conflict; result is sorted by title for stable diffs.
    """
    index: dict[str, dict[str, Any]] = {}
    for show in existing:
        key = show.get("detailPath") or show.get("full_url", "")
        index[key] = show
    for show in fresh:
        key = show.get("detailPath") or show.get("full_url", "")
        index[key] = show
    result = list(index.values())
    result.sort(key=lambda s: (s.get("title") or "").lower())
    return result


def save(path: str, shows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    """Write a {meta, total, shows} envelope to *path* as pretty JSON."""
    payload = {"meta": meta, "total": len(shows), "shows": shows}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Wrote %d show(s) → %s", len(shows), path)


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------


def print_summary(shows: list[dict[str, Any]]) -> None:
    header = f"{'#':<4} {'Title':<42} {'Rating':<8} {'Full URL'}"
    print(f"\n{header}")
    print("-" * 120)
    for i, show in enumerate(shows, 1):
        title  = (show.get("title") or "")[:41]
        rating = show.get("imdbRatingValue", "N/A")
        url    = show.get("full_url", "")
        print(f"{i:<4} {title:<42} {str(rating):<8} {url}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    num_pages = DEFAULT_PAGES
    log.info("Starting sflix.film TV-show scrape — %d page(s) requested.", num_pages)

    run_ts   = datetime.now(timezone.utc)
    run_meta = {
        "scraped_at":      run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages_requested": num_pages,
        "per_page":        PER_PAGE,
        "channel_id":      CHANNEL_ID,
        "sort":            SORT_METHOD,
        "api_url":         API_URL,
    }

    # ── Scrape ──────────────────────────────────────────────────────────────
    fresh = scrape_all(num_pages)

    if not fresh:
        log.error("No shows were scraped. Exiting without writing files.")
        sys.exit(1)

    run_meta["shows_this_run"] = len(fresh)
    log.info("Total fetched this run: %d", len(fresh))

    # ── Dated snapshot (this run only) ──────────────────────────────────────
    dated_file = f"sflix_tv_shows_{run_ts.strftime('%Y-%m-%d')}.json"
    save(dated_file, fresh, run_meta)

    # ── Latest (this run only, fixed name) ──────────────────────────────────
    save(OUT_LATEST, fresh, run_meta)

    # ── Full merged dataset ──────────────────────────────────────────────────
    existing = load_existing(OUT_FULL)
    merged   = merge(existing, fresh)
    run_meta["shows_total_merged"] = len(merged)
    save(OUT_FULL, merged, run_meta)

    log.info("Done. This run: %d | Merged total: %d", len(fresh), len(merged))

    # Console table (visible in GitHub Actions logs)
    print_summary(fresh)


if __name__ == "__main__":
    main()
