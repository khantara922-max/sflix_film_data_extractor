"""
scraper.py — Scrape TV shows from sflix.film and save only full URLs to JSON.

API:  POST https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter
Site: https://sflix.film

Output files (written to CWD = repo root when run by GitHub Actions):
  sflix_tv_shows.json             — full merged list of URLs (grows across runs)
  sflix_tv_shows_latest.json      — only URLs fetched in this run
  sflix_tv_shows_YYYY-MM-DD.json  — dated snapshot for this run

Dependencies: requests  (+ stdlib only)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL     = "https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter"
BASE_URL    = "https://sflix.film"
DETAIL_BASE = f"{BASE_URL}/detail/"

CHANNEL_ID  = 2
SORT_METHOD = "ForYou"
PER_PAGE    = 28

DEFAULT_PAGES = int(os.environ.get("SCRAPER_PAGES", "10"))

REQUEST_DELAY   = 1.2
REQUEST_TIMEOUT = 20
MAX_RETRIES     = 3
RETRY_BACKOFF   = 3

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
SESSION.headers.update({
    "Content-Type":    "application/json",
    "Accept":          "application/json",
    "x-request-lang":  "en",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
})

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def fetch_page(page: int, attempt: int = 1) -> list[str]:
    """Fetch one page from the API and return a list of full URLs."""
    payload = {
        "page":      str(page),
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
        return _retry(page, attempt)
    except requests.exceptions.RequestException as exc:
        log.warning("Request error on page %d: %s (attempt %d/%d)", page, exc, attempt, MAX_RETRIES)
        return _retry(page, attempt)
    except ValueError as exc:
        log.warning("JSON decode error on page %d: %s", page, exc)
        return []

    if data.get("code") != 0:
        log.warning("API error on page %d: %s", page, data.get("message", "unknown"))
        return []

    items = data.get("data", {}).get("items", [])
    urls = [DETAIL_BASE + item["detailPath"] for item in items if item.get("detailPath")]
    log.info("Page %d → %d URL(s)", page, len(urls))
    return urls


def _retry(page: int, attempt: int) -> list[str]:
    if attempt < MAX_RETRIES:
        wait = RETRY_BACKOFF * attempt
        log.info("Retrying page %d in %ds …", page, wait)
        time.sleep(wait)
        return fetch_page(page, attempt + 1)
    log.error("Giving up on page %d after %d attempts.", page, MAX_RETRIES)
    return []


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------

def scrape_all(num_pages: int) -> list[str]:
    """Fetch all pages and return a deduplicated list of full URLs."""
    seen: dict[str, None] = {}  # ordered dedup via insertion-order dict

    for page in range(1, num_pages + 1):
        urls = fetch_page(page)

        if not urls:
            log.info("Page %d returned 0 URLs — stopping early.", page)
            break

        for url in urls:
            seen[url] = None

        if page < num_pages:
            time.sleep(REQUEST_DELAY)

    return list(seen.keys())


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

def load_existing(path: str) -> list[str]:
    """Load previously saved URLs from *path*, or [] if missing/corrupt."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "urls" in data:
            return data["urls"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s (%s) — starting fresh.", path, exc)
    return []


def merge(existing: list[str], fresh: list[str]) -> list[str]:
    """Merge fresh URLs into existing, preserving order, deduplicating."""
    seen: dict[str, None] = {url: None for url in existing}
    for url in fresh:
        seen[url] = None
    result = sorted(seen.keys())  # alphabetical sort = stable git diffs
    return result


def save(path: str, urls: list[str], meta: dict) -> None:
    """Write {meta, total, urls} to *path* as pretty JSON."""
    payload = {"meta": meta, "total": len(urls), "urls": urls}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Wrote %d URL(s) → %s", len(urls), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    num_pages = DEFAULT_PAGES
    log.info("Starting sflix.film scrape — %d page(s) requested.", num_pages)

    run_ts   = datetime.now(timezone.utc)
    run_meta = {
        "scraped_at":      run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages_requested": num_pages,
        "per_page":        PER_PAGE,
        "source":          API_URL,
    }

    fresh = scrape_all(num_pages)

    if not fresh:
        log.error("No URLs scraped. Exiting without writing files.")
        sys.exit(1)

    run_meta["urls_this_run"] = len(fresh)

    # Dated snapshot
    dated_file = f"sflix_tv_shows_{run_ts.strftime('%Y-%m-%d')}.json"
    save(dated_file, fresh, run_meta)

    # Latest (fixed name)
    save(OUT_LATEST, fresh, run_meta)

    # Full merged
    existing = load_existing(OUT_FULL)
    merged   = merge(existing, fresh)
    run_meta["urls_total_merged"] = len(merged)
    save(OUT_FULL, merged, run_meta)

    log.info("Done. This run: %d | Merged total: %d", len(fresh), len(merged))

    # Print to GitHub Actions log
    print(f"\nTotal URLs this run: {len(fresh)}")
    for i, url in enumerate(fresh, 1):
        print(f"{i:<4} {url}")


if __name__ == "__main__":
    main()
