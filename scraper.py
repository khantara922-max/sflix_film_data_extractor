import requests
import json
import os
import sys

API_URL = "https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/filter"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-request-lang": "en",
    "Origin": "https://sflix.film",
    "Referer": "https://sflix.film/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
}
BASE_URL = "https://sflix.film/detail/"
OUTPUT_FILE = "sflix_tv_shows.json"
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB in bytes


def fetch_page(page: int) -> list[dict]:
    payload = {
        "page": str(page),
        "perPage": 28,
        "channelId": 2,
        "sort": "ForYou"
    }
    try:
        response = requests.post(API_URL, headers=HEADERS, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            print(f"  API error on page {page}: {data.get('message')}")
            return []
        items = data["data"]["items"]
        print(f"  Page {page}: {len(items)} shows fetched")
        return items
    except requests.RequestException as e:
        print(f"  Request failed on page {page}: {e}")
        return []


def load_existing_urls() -> set[str]:
    """Load all existing URLs from sflix_tv_shows.json and any split files."""
    existing_urls = set()

    # Load from base file
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    url = item.get("full_url", "")
                    if url:
                        existing_urls.add(url)
        except (json.JSONDecodeError, KeyError):
            pass

    # Load from split files: sflix_tv_shows_part2.json, part3, ...
    part = 2
    while True:
        split_file = OUTPUT_FILE.replace(".json", f"_part{part}.json")
        if not os.path.exists(split_file):
            break
        try:
            with open(split_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    url = item.get("full_url", "")
                    if url:
                        existing_urls.add(url)
        except (json.JSONDecodeError, KeyError):
            pass
        part += 1

    print(f"  Found {len(existing_urls)} existing URLs across all files")
    return existing_urls


def load_existing_shows() -> list[dict]:
    """Load all existing shows from base file only (for appending)."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            return []
    return []


def split_and_save(shows: list[dict]):
    """Save shows into files, splitting at 2MB boundaries."""
    current_chunk = []
    current_size = 0
    file_index = 1
    saved_files = []

    for show in shows:
        item_json = json.dumps(show, ensure_ascii=False)
        item_size = len(item_json.encode("utf-8"))

        # Estimate total size if we add this item (with commas + brackets overhead)
        projected_size = current_size + item_size + 2  # +2 for ", "

        if current_size > 0 and projected_size > MAX_FILE_SIZE:
            # Save current chunk
            filename = OUTPUT_FILE if file_index == 1 else OUTPUT_FILE.replace(".json", f"_part{file_index}.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(current_chunk, f, ensure_ascii=False, indent=2)
            saved_files.append((filename, len(current_chunk)))
            current_chunk = []
            current_size = 0
            file_index += 1

        current_chunk.append(show)
        current_size += item_size + 2

    # Save the last chunk
    if current_chunk:
        filename = OUTPUT_FILE if file_index == 1 else OUTPUT_FILE.replace(".json", f"_part{file_index}.json")
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(current_chunk, f, ensure_ascii=False, indent=2)
        saved_files.append((filename, len(current_chunk)))

    return saved_files


def main():
    total_pages = int(os.environ.get("SCRAPER_PAGES", 10))

    print("Loading existing data...")
    existing_urls = load_existing_urls()
    existing_shows = load_existing_shows()

    new_shows = []
    duplicate_count = 0

    for page in range(1, total_pages + 1):
        print(f"Fetching page {page}...")
        items = fetch_page(page)

        for show in items:
            detail_path = show.get("detailPath", "")
            full_url = BASE_URL + detail_path
            show["full_url"] = full_url

            if full_url in existing_urls:
                duplicate_count += 1
            else:
                existing_urls.add(full_url)
                new_shows.append(show)

    print(f"\nNew shows found:      {len(new_shows)}")
    print(f"Duplicates skipped:   {duplicate_count}")

    if not new_shows:
        print("No new shows to add. Exiting.")
        sys.exit(0)

    all_shows = existing_shows + new_shows
    print(f"Total shows in store: {len(all_shows)}")

    saved_files = split_and_save(all_shows)

    print(f"\nSaved files:")
    for filename, count in saved_files:
        size_kb = os.path.getsize(filename) / 1024
        print(f"  {filename}: {count} shows ({size_kb:.1f} KB)")

    print(f"\n{'#':<4} {'Title':<40} {'Rating':<8} {'Full URL'}")
    print("-" * 120)
    for i, show in enumerate(new_shows, 1):
        print(f"{i:<4} {show.get('title', '')[:39]:<40} {show.get('imdbRatingValue', 'N/A'):<8} {show['full_url']}")


if __name__ == "__main__":
    main()
