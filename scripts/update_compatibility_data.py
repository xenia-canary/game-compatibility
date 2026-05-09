"""
Fetch open issues from the Xenia Canary compatibility repo and save them to JSON.

- Uses GitHub REST API
- Handles pagination
- Filters only open issues
- Optional TOKEN env variable for higher rate limits
"""

import json
import requests
import os
import time
import re
import logging
from typing import Dict, Tuple, Any, Optional

# =========================
# Configuration
# =========================
# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Logging level
logging.getLogger().setLevel(logging.DEBUG)

# Repo settings
OWNER = "xenia-canary"
REPO = "game-compatibility"
STATE = "open"

# API settings
API_BASE = "https://api.github.com"
BASE_URL = f"{API_BASE}/repos/{OWNER}/{REPO}/issues"
PER_PAGE = 100
TIMEOUT = 20

# Auth
GITHUB_TOKEN = os.getenv("COMPATIBILITY_TOKEN")  # Optional token for higher rate limits

# Output
OUTPUT_FILE = "compatibility_data.json"

# Retry / rate limiting
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
RATE_LIMIT_MIN_WAIT = 5
PAGE_DELAY = 1

# Parsing config

# Title parsing rules
TITLE_ID_LENGTH = 8

# Regex: strict 8-character hex Title ID parsing
TITLE_REGEX = re.compile(
    r"""
    ^\s*
    (?P<id>[0-9A-Fa-f]{8})      # 8-char hex Title ID
    (?:\s*[-–—]?\s*)?           # Separator (-, en-dash, em-dash, or space)
    (?P<title>.+)?              # Title
    \s*$
    """,
    re.VERBOSE,
)

# Label -> compatibility mapping
STATE_MAPPINGS = {
    "state-nothing": "Unplayable",
    "state-crash": "Unplayable",
    "state-crash-guest": "Unplayable",
    "state-crash-host": "Unplayable",
    "state-crash-xna-WONTFIX": "Unplayable",
    "state-intro": "Loads",
    "state-hang": "Loads",
    "state-load": "Loads",
    "state-title": "Loads",
    "state-menus": "Loads",
    "state-gameplay": "Gameplay",
    "state-playable": "Playable",
}

# Priority for state selection (lower = worse)
STATE_PRIORITY = {
    "Unplayable": 0,
    "Loads": 1,
    "Gameplay": 2,
    "Playable": 3,
}

# Ignored issue labels
IGNORED_LABELS = {
    "issue-cluttered",
    "issue-duplicate",
    "issue-invalid",
    "issue-superseded",
}

# Runtime data
COMPATIBILITY_DATA = []


# =========================
# Headers
# =========================
def get_headers() -> Dict[str, str]:
    """
    Build request headers for GitHub API.
    Adds authorization if a token is available.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xenia-data-sync/1.0",
    }

    # Add auth header only if token exists
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        logger.debug("Authorization token added to headers")
    else:
        logger.debug("No authorization token - using unauthenticated requests")

    return headers


# =========================
# Parsing logic
# =========================
def parse_title(issue_title: str) -> Dict[str, Optional[str]]:
    """
    Parses titles in formats like:
    - "4D5307E6 - Halo 3"
    - "4D5307E6 Halo 3"
    - "454109F8 FIFA18"
    """

    title = issue_title.strip()
    logger.debug(f"Parsing title: '{title}'")

    match = TITLE_REGEX.match(title)

    if match:
        game_id = match.group("id").upper()
        game_title = match.group("title")

        # In case there are entries that don't have proper title id
        if len(game_id) != 8:
            logger.debug(f"Invalid title ID length: '{game_id}' (expected 8 chars)")
            return {"id": None, "title": title, "valid": False}

        logger.debug(f"Parsed: ID={game_id}, Title={game_title}")
        return {
            "id": game_id,
            "title": game_title.strip() if game_title else "",
            "valid": True,
        }

    logger.debug(f"No match found for title: '{title}'")
    return {"id": None, "title": title, "valid": False}


def parse_labels(labels: list) -> Dict[str, Any]:
    """
    Splits labels into a structured object:

    labels = {
        "state": [...raw state-* labels...],
        "others": [...everything else...],
        "state_parsed": "Playable/Loads/etc"
    }

    When multiple state- labels exist, selects the worst state based on priority:
    Unplayable < Loads < Gameplay < Playable
    """

    state_parsed = "Unknown"
    state = []
    others = []
    found_states = []

    logger.debug(f"Processing {len(labels)} labels")

    for label in labels:
        name = label.get("name", "")

        if name.startswith("state-"):
            state.append(name)
            logger.debug(f"Found state label: {name}")

            if name in STATE_MAPPINGS:
                mapped_state = STATE_MAPPINGS[name]
                found_states.append(mapped_state)
                logger.debug(f"Mapped to compatibility state: {mapped_state}")
        else:
            others.append(name)
            logger.debug(f"Found other label: {name}")

    if found_states:
        state_parsed = min(
            found_states, key=lambda s: STATE_PRIORITY.get(s, float("inf"))
        )
        logger.debug(f"Selected worst state from multiple: {state_parsed}")

    logger.debug(
        f"Final state: {state_parsed}, State labels: {len(state)}, Other labels: {len(others)}"
    )

    return {
        "state": state,
        "others": others,
        "parsed": state_parsed,
    }


# =========================
# Request logic
# =========================
def fetch_issues():
    """
    Fetch all issues using GitHub cursor-based pagination (Link headers only).
    Fixes GitHub 422 error caused by page-based pagination on large datasets.
    """

    url = BASE_URL
    all_data = []
    page_num = 0

    while url:
        page_num += 1
        logger.info(f"Fetching page {page_num}: {url}")

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Attempt {attempt + 1}/{MAX_RETRIES} for page {page_num}")
                response = requests.get(
                    url,
                    headers=get_headers(),
                    params={
                        "per_page": PER_PAGE,
                        "state": STATE,
                    },
                    timeout=TIMEOUT,
                )

                logger.debug(f"Response status: {response.status_code}")

                # Handle rate limiting
                if response.status_code == 403:
                    reset = response.headers.get("X-RateLimit-Reset")
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    logger.warning(f"Rate limit remaining: {remaining}")
                    if reset:
                        wait_time = max(
                            int(reset) - int(time.time()), RATE_LIMIT_MIN_WAIT
                        )
                        logger.warning(f"Rate limited. Sleeping {wait_time}s...")
                        time.sleep(wait_time)
                        continue

                response.raise_for_status()
                data = response.json()

                if not isinstance(data, list) or not data:
                    logger.info(f"Page {page_num}: No more data or invalid response")
                    return all_data

                logger.info(f"Page {page_num}: Fetched {len(data)} issues")
                all_data.extend(data)
                break

            except requests.RequestException as e:
                logger.error(f"Attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep_time = RETRY_BACKOFF_BASE * (attempt + 1)
                    logger.debug(f"Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)

        # Cursor pagination
        link_header = response.headers.get("Link", "")
        next_url = None

        if link_header:
            parts = link_header.split(",")
            for part in parts:
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1 : part.find(">")]
                    logger.debug(f"Next page URL found: {next_url}")
                    break
        else:
            logger.debug("No Link header found - last page reached")

        url = next_url

        if url:
            logger.debug(f"Waiting {PAGE_DELAY}s before next page...")
            time.sleep(PAGE_DELAY)

    logger.info(
        f"Finished fetching. Total pages: {page_num}, Total issues: {len(all_data)}"
    )
    return all_data


# =========================
# Main function
# =========================
def main():
    """
    Main execution loop:
    - Iterates through all pages
    - Stops when no more pages exist
    - Saves results to JSON file
    """

    start_time = time.time()

    # Log configuration
    logger.info("=" * 60)
    logger.info("Xenia Canary Game Compatibility Data Fetcher")
    logger.info("=" * 60)
    logger.info(f"Repository: {OWNER}/{REPO}")
    logger.info(f"State filter: {STATE}")
    logger.info(f"Output file: {OUTPUT_FILE}")
    logger.info(f"Per page: {PER_PAGE}")
    logger.info(f"Max retries: {MAX_RETRIES}")

    # Warn user if no token is provided (rate limits will be low)
    if GITHUB_TOKEN:
        logger.info("Authentication token provided - using authenticated requests")
    else:
        logger.warning(
            "No COMPATIBILITY_TOKEN set — rate limits will be low (60 req/hour)"
        )

    # Fetch issues
    logger.info("-" * 60)
    logger.info("Starting compatibility data update...")
    issues = fetch_issues()
    logger.info(f"Fetched {len(issues)} total issues from GitHub")

    if not issues:
        logger.error("No issues fetched. Exiting.")
        return

    # Process issues
    logger.info("-" * 60)
    logger.info("Processing issues...")
    skipped_pr = 0
    skipped_invalid = 0
    processed_states = {}

    for idx, issue in enumerate(issues, 1):
        issue_num = issue.get("number", "unknown")
        issue_title = issue.get("title", "")

        if "pull_request" in issue:
            skipped_pr += 1
            logger.debug(f"[{idx}/{len(issues)}] Skipping PR #{issue_num}")
            continue

        logger.debug(
            f"[{idx}/{len(issues)}] Processing issue #{issue_num}: {issue_title}"
        )

        title_data = parse_title(issue.get("title", ""))
        if not title_data.get("valid", True):
            skipped_invalid += 1
            logger.debug(f"Skipping invalid title: {issue.get('title', '')}")
            continue

        label_data = parse_labels(issue.get("labels", []))
        # If any ignored label is present, skip this issue
        if any(lbl in IGNORED_LABELS for lbl in label_data["others"]):
            logger.debug(f"Skipping issue #{issue_num} due to ignored label")
            continue
        state = label_data["parsed"]
        processed_states[state] = processed_states.get(state, 0) + 1

        logger.debug(
            f"Added entry: ID={title_data['id']}, Title={title_data['title']}, State={state}"
        )

        COMPATIBILITY_DATA.append(
            {
                "issue": issue.get("number"),
                "id": title_data["id"],
                "title": title_data["title"],
                "updated": issue.get("updated_at", ""),
                "state": state,
                "labels": {
                    "state": label_data["state"],
                    "others": label_data["others"],
                },
                "url": issue.get("html_url", ""),
            }
        )

    logger.info(f"Processed {len(COMPATIBILITY_DATA)} valid entries")
    logger.info(f"Skipped: {skipped_pr} PRs, {skipped_invalid} invalid titles")
    logger.info(f"State breakdown: {processed_states}")

    # Deduplication
    logger.info("-" * 60)
    logger.info("Deduplicating entries by URL...")
    seen = set()
    unique = []
    duplicates = 0

    for item in COMPATIBILITY_DATA:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
        else:
            duplicates += 1
            logger.debug(f"Duplicate found: {item['id']} - {item['title']}")

    logger.info(
        f"Removed {duplicates} duplicates, {len(unique)} unique entries remaining"
    )

    # Sort newest first
    logger.info("Sorting entries by issue number (newest first)...")
    unique.sort(key=lambda x: x["issue"], reverse=True)
    logger.info(f"Sorted {len(unique)} entries")

    # Save collected data to file
    logger.info("-" * 60)
    logger.info(f"Saving data to {OUTPUT_FILE}...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(unique, f, indent=2)

    file_size = os.path.getsize(OUTPUT_FILE) if os.path.exists(OUTPUT_FILE) else 0
    logger.info(f"Saved {len(unique)} entries ({file_size} bytes) to {OUTPUT_FILE}")

    # Summary
    elapsed_time = time.time() - start_time
    logger.info("-" * 60)
    logger.info("SUMMARY")
    logger.info("-" * 60)
    logger.info(f"Total issues fetched: {len(issues)}")
    logger.info(f"Valid entries processed: {len(COMPATIBILITY_DATA)}")
    logger.info(f"Unique entries saved: {len(unique)}")
    logger.info(f"PRs skipped: {skipped_pr}")
    logger.info(f"Invalid titles skipped: {skipped_invalid}")
    logger.info(f"Duplicates removed: {duplicates}")
    logger.info(f"Execution time: {elapsed_time:.2f}s")
    logger.info("=" * 60)


# Entry point
if __name__ == "__main__":
    main()
