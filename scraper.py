"""
scraper.py — Scrape TV shows from Sflix and save results to JSON files.

Output files (written to the current working directory / repo root):
  sflix_tv_shows.json          — full merged dataset (all pages, all runs)
  sflix_tv_shows_latest.json   — only the shows fetched in this run
  sflix_tv_shows_YYYY-MM-DD.json — dated snapshot for this run

Dependencies: requests (+ stdlib only)
"""

import json
import os
import re
import sys
import time
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlencode

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://sflix.to"

# Sflix TV-shows listing endpoint (page=N gives page N, 1-indexed)
TV_SHOWS_URL = f"{BASE_URL}/tv-show"

# How many pages to scrape (overridden by env var SCRAPER_PAGES)
DEFAULT_PAGES = int(os.environ.get("SCRAPER_PAGES", "10"))

# Polite delay between requests (seconds)
REQUEST_DELAY = 1.5

# HTTP request timeout (seconds)
REQUEST_TIMEOUT = 20

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 3  # seconds, multiplied by attempt number

# Output filenames (all written to CWD = repo root when run by GitHub Actions)
OUT_FULL = "sflix_tv_shows.json"
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": BASE_URL,
    }
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch(url: str, params: dict | None = None, attempt: int = 1) -> requests.Response | None:
    """GET *url* with retry logic. Returns the Response or None on failure."""
    full_url = url + ("?" + urlencode(params) if params else "")
    try:
        log.debug("GET %s", full_url)
        resp = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        log.warning("HTTP %s for %s (attempt %d/%d)", status, full_url, attempt, MAX_RETRIES)
    except requests.exceptions.RequestException as exc:
        log.warning("Request error for %s: %s (attempt %d/%d)", full_url, exc, attempt, MAX_RETRIES)

    if attempt < MAX_RETRIES:
        wait = RETRY_BACKOFF * attempt
        log.info("Retrying in %ds …", wait)
        time.sleep(wait)
        return fetch(url, params, attempt + 1)

    log.error("Giving up on %s after %d attempts.", full_url, MAX_RETRIES)
    return None


# ---------------------------------------------------------------------------
# Minimal HTML parser (no BeautifulSoup — only stdlib + requests allowed)
# ---------------------------------------------------------------------------

# Patterns tuned for Sflix's rendered HTML structure.
# Sflix uses a card-based layout; each show card looks roughly like:
#
#   <div class="film-detail">
#     <h3 class="film-name">
#       <a href="/watch-tv-shows-free-online/..." title="Show Title">Show Title</a>
#     </h3>
#     <div class="fd-infor">
#       <span class="fdi-item">TV</span>
#       <span class="fdi-item fdi-duration">45m</span>
#       <span class="fdi-item fdi-lang">EN</span>
#       <span class="fdi-quality">HD</span>
#     </div>
#   </div>

# Matches an entire film-detail block
_RE_CARD = re.compile(
    r'<div[^>]+class="[^"]*film-detail[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.DOTALL,
)

# Within a card — the anchor with href and title
_RE_ANCHOR = re.compile(
    r'<a\s[^>]*href="([^"]+)"[^>]*title="([^"]+)"[^>]*>',
    re.DOTALL,
)

# Poster image (inside the parent .film-poster div, just before film-detail)
# We match the whole film_list item to capture poster + detail together.
_RE_ITEM = re.compile(
    r'<div[^>]+class="[^"]*flw-item[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>',
    re.DOTALL,
)

_RE_IMG = re.compile(r'<img[^>]+data-src="([^"]+)"', re.DOTALL)
_RE_ALT = re.compile(r'<img[^>]+alt="([^"]+)"', re.DOTALL)

# Info spans
_RE_TYPE = re.compile(r'<span[^>]+class="[^"]*fdi-item[^"]*"[^>]*>([^<]+)</span>')
_RE_QUALITY = re.compile(r'<div[^>]+class="[^"]*fdi-quality[^"]*"[^>]*>([^<]+)</div>')
_RE_DURATION = re.compile(r'<span[^>]+class="[^"]*fdi-duration[^"]*"[^>]*>([^<]+)</span>')

# Show ID from the URL slug  e.g. /watch-tv-shows-free-online/the-show-12345
_RE_ID = re.compile(r'-(\d+)(?:/|$)')

# Total pages from pagination
_RE_TOTAL_PAGES = re.compile(r'href="[^"]+\bpage=(\d+)"[^>]*>\s*(?:Last|&raquo;|»)', re.IGNORECASE)
_RE_PAGE_LINKS = re.compile(r'href="[^"]+\bpage=(\d+)"')


def _text(s: str) -> str:
    """Strip HTML tags and decode common entities."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&#039;", "'").replace("&quot;", '"').replace("&nbsp;", " ")
    return s.strip()


def parse_show_cards(html: str) -> list[dict[str, Any]]:
    """Extract TV-show records from a listing page's HTML."""
    shows: list[dict[str, Any]] = []

    for item_m in _RE_ITEM.finditer(html):
        block = item_m.group(1)

        # --- anchor / title / url ---
        a_m = _RE_ANCHOR.search(block)
        if not a_m:
            continue
        href, title = a_m.group(1), _text(a_m.group(2))
        url = urljoin(BASE_URL, href)

        # --- show id ---
        id_m = _RE_ID.search(href)
        show_id = id_m.group(1) if id_m else None

        # --- poster ---
        img_m = _RE_IMG.search(block)
        poster = img_m.group(1) if img_m else None

        # --- quality ---
        q_m = _RE_QUALITY.search(block)
        quality = _text(q_m.group(1)) if q_m else None

        # --- duration ---
        dur_m = _RE_DURATION.search(block)
        duration = _text(dur_m.group(1)) if dur_m else None

        # --- type / genre info spans (first two usually type + language) ---
        info_spans = [_text(m.group(1)) for m in _RE_TYPE.finditer(block)]
        content_type = info_spans[0] if len(info_spans) > 0 else None
        language = info_spans[1] if len(info_spans) > 1 else None

        shows.append(
            {
                "id": show_id,
                "title": title,
                "url": url,
                "poster": poster,
                "quality": quality,
                "type": content_type,
                "language": language,
                "duration": duration,
            }
        )

    return shows


def detect_max_pages(html: str, requested: int) -> int:
    """Read pagination from HTML to avoid fetching pages that don't exist."""
    # Try to find the highest page number linked
    nums = [int(m.group(1)) for m in _RE_PAGE_LINKS.finditer(html)]
    if nums:
        detected = max(nums)
        if detected < requested:
            log.info("Pagination shows only %d pages; capping at that.", detected)
            return detected
    return requested


# ---------------------------------------------------------------------------
# Scraper core
# ---------------------------------------------------------------------------


def scrape_page(page: int) -> list[dict[str, Any]]:
    """Scrape a single listing page and return show dicts."""
    params = {"page": page} if page > 1 else {}
    resp = fetch(TV_SHOWS_URL, params=params)
    if resp is None:
        return []

    shows = parse_show_cards(resp.text)
    log.info("Page %d → %d show(s) found.", page, len(shows))
    return shows


def scrape_all(num_pages: int) -> list[dict[str, Any]]:
    """Scrape *num_pages* listing pages and return deduplicated show list."""
    all_shows: dict[str, dict[str, Any]] = {}  # keyed by show id (dedup)
    effective_pages = num_pages

    for page in range(1, num_pages + 1):
        shows = scrape_page(page)

        # On the first page, re-check pagination ceiling
        if page == 1 and shows:
            resp = fetch(TV_SHOWS_URL)
            if resp:
                effective_pages = detect_max_pages(resp.text, num_pages)

        for show in shows:
            key = show["id"] or show["url"]
            all_shows[key] = show  # later pages overwrite (same data, fresher)

        if page < effective_pages:
            time.sleep(REQUEST_DELAY)

        if page >= effective_pages:
            break

    return list(all_shows.values())


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------


def load_json(path: str) -> list[dict[str, Any]]:
    """Load an existing JSON list from *path*, or return [] if missing/corrupt."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and "shows" in data:
            return data["shows"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s — starting fresh.", path, exc)
    return []


def merge_shows(
    existing: list[dict[str, Any]], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge *fresh* into *existing*, using id/url as the dedup key."""
    index: dict[str, dict[str, Any]] = {}
    for show in existing:
        key = show.get("id") or show.get("url", "")
        index[key] = show
    for show in fresh:
        key = show.get("id") or show.get("url", "")
        index[key] = show  # fresh data wins
    merged = list(index.values())
    # Sort alphabetically by title for stable diffs
    merged.sort(key=lambda s: s.get("title", "").lower())
    return merged


def save_json(path: str, shows: list[dict[str, Any]], run_meta: dict[str, Any]) -> None:
    """Write shows + metadata to *path* as pretty-printed JSON."""
    payload = {
        "meta": run_meta,
        "total": len(shows),
        "shows": shows,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("Wrote %d show(s) → %s", len(shows), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    num_pages = DEFAULT_PAGES
    log.info("Starting Sflix TV-show scrape — %d page(s) requested.", num_pages)

    run_ts = datetime.now(timezone.utc)
    run_meta = {
        "scraped_at": run_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages_requested": num_pages,
        "source": TV_SHOWS_URL,
    }

    # --- Scrape ---
    fresh_shows = scrape_all(num_pages)
    if not fresh_shows:
        log.error("No shows were scraped. Exiting without writing files.")
        sys.exit(1)

    run_meta["shows_this_run"] = len(fresh_shows)

    # --- Dated snapshot (only this run's data) ---
    dated_filename = f"sflix_tv_shows_{run_ts.strftime('%Y-%m-%d')}.json"
    save_json(dated_filename, fresh_shows, run_meta)

    # --- Latest (only this run's data, fixed filename) ---
    save_json(OUT_LATEST, fresh_shows, run_meta)

    # --- Full merged dataset ---
    existing = load_json(OUT_FULL)
    merged = merge_shows(existing, fresh_shows)
    run_meta["shows_total_merged"] = len(merged)
    save_json(OUT_FULL, merged, run_meta)

    log.info(
        "Done. This run: %d shows | Merged total: %d shows.",
        len(fresh_shows),
        len(merged),
    )


if __name__ == "__main__":
    main()
