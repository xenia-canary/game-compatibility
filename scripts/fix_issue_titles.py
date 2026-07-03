"""
Fix issue titles to the canonical format: HEXID - Game Name.

- Reads the event payload or fetches all open issues via GitHub REST API
- Normalizes non-conforming titles (missing separator, name-first, embedded ID)
- Skips PRs and ignored labels (issue-cluttered, issue-duplicate, etc.)
- Dry-run mode reports without modifying; live mode patches titles

Triggered via:
  issues: [opened, reopened]  — patches just that one issue (always live)
  workflow_dispatch           — batch checks all issues (dry-run toggle)
"""

import json
import os
import re
import sys
import time
import logging
from typing import Optional
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.DEBUG)

API_BASE = "https://api.github.com"
PER_PAGE = 100
TIMEOUT = 20
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
RATE_LIMIT_MIN_WAIT = 5
PAGE_DELAY = 1
EXPECTED_SEPARATOR = " - "

# Title patterns tried in order. To add a new format, append an entry.
TITLE_PATTERNS = [
    {
        "name": "id_first",
        "regex": re.compile(r"^\s*(?P<id>[0-9A-Fa-f]{8})\s*[-–—]?\s*(?P<name>.+)$"),
        "check_unchanged": True,
    },
    {
        "name": "name_first",
        "regex": re.compile(r"^\s*(?P<name>.+)\s*[-–—]?\s*(?P<id>[0-9A-Fa-f]{8})\s*$"),
    },
    {
        "name": "id_anywhere",
        "regex": re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{8})(?![0-9A-Fa-f])"),
        "mode": "anywhere",
    },
]

IGNORED_LABELS = {
    "issue-cluttered",
    "issue-duplicate",
    "issue-invalid",
    "issue-superseded",
}


# =========================
# Helpers
# =========================


def get_owner_repo() -> tuple:
    """Extract owner and repo name from GITHUB_REPOSITORY env var."""
    full = os.getenv("GITHUB_REPOSITORY", "")
    if full and "/" in full:
        parts = full.split("/", 1)
        owner, repo = parts[0], parts[1]
        logger.debug(f"Resolved owner/repo from env: {owner}/{repo}")
        return owner, repo
    owner = os.getenv("GITHUB_REPOSITORY_OWNER", "xenia-canary")
    repo = "game-compatibility"
    logger.debug(f"Resolved owner/repo from fallback: {owner}/{repo}")
    return owner, repo


def get_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "title-fixer/1.0",
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        logger.debug("Authorization token added to headers")
    else:
        logger.warning("No token found — rate limits will be low (60 req/hour)")
    return headers


def _extract_id_name(pat, title):
    """Extract (id, name) from named capture groups at the title boundary."""
    m = pat["regex"].match(title)
    if not m:
        logger.debug(f"Pattern '{pat['name']}' did not match: '{title}'")
        return None
    id_ = m.group("id").upper()
    name = m.group("name").strip()
    logger.debug(f"Pattern '{pat['name']}' matched: ID={id_}, Name='{name}'")
    return id_, name


def _extract_anywhere(pat, title):
    """Extract (id, name) when the hex ID is embedded anywhere in the title.

    The hex ID is removed and surrounding text becomes the game name.
    """
    m = pat["regex"].search(title)
    if not m:
        logger.debug(f"Pattern '{pat['name']}' did not find hex ID in: '{title}'")
        return None
    id_ = m.group(1).upper()
    logger.debug(
        f"Pattern '{pat['name']}' found hex ID {id_} at position {m.start()}-{m.end()}"
    )
    before = title[: m.start()].strip()
    after = title[m.end() :].strip()
    logger.debug(f"Before hex ID: '{before}', After: '{after}'")
    name = " ".join(
        filter(
            None,
            [
                re.sub(r"^\s*[-–—]?\s*|\s*[-–—]?\s*$", "", before),
                re.sub(r"^\s*[-–—]?\s*|\s*[-–—]?\s*$", "", after),
            ],
        )
    )
    name = re.sub(r"\s+", " ", name).strip()
    if name:
        logger.debug(f"Extracted name from surroundings: '{name}'")
        return id_, name
    logger.debug(f"Pattern '{pat['name']}' found ID but no name remains")
    return None


def normalize_title(issue_title: str) -> Optional[str]:
    """
    Normalize an issue title to 'HEXID - Name' format.

    Tries each pattern in TITLE_PATTERNS in order. Returns None when
    the title is already correct or no pattern matches.
    """
    title = issue_title.strip()
    if not title:
        logger.debug("Empty title after stripping")
        return None

    logger.debug(f"Normalizing title: '{title}'")

    for pat in TITLE_PATTERNS:
        extract = (
            _extract_anywhere if pat.get("mode") == "anywhere" else _extract_id_name
        )
        result = extract(pat, title)
        if not result:
            logger.debug(f"Pattern '{pat['name']}' — no match")
            continue
        id_, name = result
        normalized = f"{id_}{EXPECTED_SEPARATOR}{name}"
        logger.debug(f"Pattern '{pat['name']}' produced: '{normalized}'")
        if pat.get("check_unchanged") and normalized == title:
            logger.debug(f"Title is already correct — skipping")
            return None
        if normalized != title:
            logger.debug(f"Would change: '{title}' → '{normalized}'")
        return normalized

    logger.debug(f"No pattern matched title: '{title}' — unfixable")
    return None


# =========================
# GitHub API
# =========================


def should_skip(issue: dict) -> bool:
    """Return True if the issue is a PR or has an ignored label."""
    if "pull_request" in issue:
        logger.debug(f"#{issue.get('number')}: skipped — pull request")
        return True
    for label in issue.get("labels", []):
        if isinstance(label, dict) and label.get("name") in IGNORED_LABELS:
            logger.debug(
                f"#{issue.get('number')}: skipped — ignored label '{label.get('name')}'"
            )
            return True
    return False


def fetch_all_issues(owner: str, repo: str) -> list:
    """Fetch all open issues with Link-header pagination."""
    url = f"{API_BASE}/repos/{owner}/{repo}/issues"
    all_issues = []
    page_num = 0

    logger.info(f"Fetching open issues from {owner}/{repo}")

    while url:
        page_num += 1
        logger.info(f"Page {page_num}: {url}")

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Attempt {attempt + 1}/{MAX_RETRIES} for page {page_num}")
                response = requests.get(
                    url,
                    headers=get_headers(),
                    params={"per_page": PER_PAGE, "state": "open"},
                    timeout=TIMEOUT,
                )
                remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
                logger.debug(
                    f"Response {response.status_code}, rate limit remaining: {remaining}"
                )

                if response.status_code == 403:
                    reset = response.headers.get("X-RateLimit-Reset")
                    if reset:
                        wait = max(int(reset) - int(time.time()), RATE_LIMIT_MIN_WAIT)
                        logger.warning(
                            f"Rate limited (remaining: {remaining}). Sleeping {wait}s..."
                        )
                        time.sleep(wait)
                        continue
                response.raise_for_status()
                data = response.json()
                page_size = len(data) if isinstance(data, list) else 0
                logger.debug(f"Page {page_num}: received {page_size} items")

                if not isinstance(data, list) or not data:
                    logger.info(f"Page {page_num}: empty — end of results")
                    return all_issues

                logger.info(f"Page {page_num}: {page_size} issues fetched")
                all_issues.extend(data)
                break

            except requests.RequestException as e:
                logger.error(f"Page {page_num}, attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep_time = RETRY_BACKOFF_BASE * (attempt + 1)
                    logger.debug(f"Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)

        link = response.headers.get("Link", "")
        next_url = None
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1 : part.find(">")]
                    logger.debug(f"Next page URL found")
                    break
        else:
            logger.debug("No Link header — last page reached")

        url = next_url
        if url:
            logger.debug(f"Waiting {PAGE_DELAY}s before next page...")
            time.sleep(PAGE_DELAY)

    logger.info(
        f"Finished fetching. Total pages: {page_num}, Total issues: {len(all_issues)}"
    )
    return all_issues


def update_title(owner: str, repo: str, number: int, new_title: str) -> bool:
    """PATCH a single issue's title. Returns True on success."""
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}"
    try:
        logger.debug(f"Updating #{number}: '{new_title}'")
        response = requests.patch(
            url,
            headers=get_headers(),
            json={"title": new_title},
            timeout=TIMEOUT,
        )
        remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
        logger.debug(
            f"PATCH #{number} — {response.status_code}, remaining: {remaining}"
        )
        response.raise_for_status()
        logger.info(f"Updated #{number}: '{new_title}'")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to update #{number}: {e}")
        return False


# =========================
# Entry point
# =========================


def main():
    """Fetch issues, normalize titles, and optionally patch mismatches."""
    start_time = time.time()
    owner, repo = get_owner_repo()
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    event_path = os.getenv("GITHUB_EVENT_PATH", "")
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"

    if event_name == "issues":
        dry_run = False

    logger.info("=" * 60)
    logger.info("Issue Title Fixer")
    logger.info("=" * 60)
    logger.info(f"Repository: {owner}/{repo}")
    logger.info(f"Event: {event_name}")
    logger.info(f"Mode: {'dry-run' if dry_run else 'live'}")

    if os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"):
        logger.info("Authentication token provided")
    else:
        logger.warning("No token set — rate limits will be low (60 req/hour)")

    logger.info("-" * 60)
    issues = []

    if event_name == "issues" and event_path:
        logger.info("Reading issue from event payload...")
        with open(event_path) as f:
            payload = json.load(f)
        issue = payload.get("issue")
        if issue:
            issues = [issue]
            logger.info(
                f"Triggered by issue #{issue.get('number')}: \"{issue.get('title', '')}\""
            )
        else:
            logger.error("No issue data in event payload")
            sys.exit(1)
    else:
        logger.info("Processing all open issues...")
        issues = fetch_all_issues(owner, repo)

    logger.info(f"Total issues fetched: {len(issues)}")

    logger.info("-" * 60)
    logger.info("Analyzing titles...")

    fixed = []
    unfixable = []
    unchanged = 0
    skipped = 0

    for idx, issue in enumerate(issues, 1):
        number = issue.get("number")
        title = issue.get("title", "")
        logger.debug(f'[{idx}/{len(issues)}] #{number}: "{title}"')

        if should_skip(issue):
            skipped += 1
            continue

        if not title.strip():
            logger.debug(f"#{number}: empty title")
            unfixable.append({"number": number, "old": title, "reason": "empty title"})
            continue

        new_title = normalize_title(title)

        if new_title is None:
            unchanged += 1
            continue

        logger.info(f'#{number}: "{title}" → "{new_title}"')
        fixed.append({"number": number, "old": title, "new": new_title})

    logger.info("-" * 60)
    logger.info(
        f"Results: {len(fixed)} to fix, {len(unfixable)} unfixable, {unchanged} correct, {skipped} skipped"
    )

    if unfixable:
        logger.warning(f"{len(unfixable)} unfixable title(s) need manual review:")
        for f in unfixable:
            logger.warning(
                f"  #{f['number']}: \"{f['old']}\" ({f.get('reason', 'no hex ID or name found')})"
            )

    if fixed and not dry_run:
        logger.info(f"Applying {len(fixed)} fix(es)...")
        results = []
        for f in fixed:
            success = update_title(owner, repo, f["number"], f["new"])
            results.append((f["number"], success))

        success_count = sum(1 for _, s in results if s)
        failure_count = len(results) - success_count
        logger.info(f"Fixed: {success_count}, Failed: {failure_count}")

    elapsed = time.time() - start_time
    logger.info("-" * 60)
    logger.info(f"Completed in {elapsed:.2f}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
