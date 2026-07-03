"""
Detect duplicates and fix malformed titles on game-compatibility repos.

- Fetches upstream compatibility_data.json release asset for duplicate lookup
- Checks new issues against that data; closes duplicates with a comment
- Normalizes titles to 'HEXID - Game Name' format
- Dry-run mode reports without modifying

Triggered via:
  issues: [opened, reopened]  — checks just that one issue (always live)
  workflow_dispatch           — batch checks all issues (dry-run toggle)

Requires GITHUB_TOKEN with issues: write scope.
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
UPSTREAM_DATA_URL = (
    "https://github.com/xenia-canary/game-compatibility/"
    "releases/download/game-compatibility/compatibility_data.json"
)
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
        logger.debug(f"Resolved owner/repo: {owner}/{repo}")
        return owner, repo
    owner = os.getenv("GITHUB_REPOSITORY_OWNER", "xenia-canary")
    repo = "game-compatibility"
    logger.debug(f"Resolved owner/repo (fallback): {owner}/{repo}")
    return owner, repo


def fetch_compatibility_data(url: str = UPSTREAM_DATA_URL) -> list:
    """Download and parse the pre-scraped compatibility_data.json from upstream."""
    logger.info(f"Fetching upstream compatibility data from {url}")
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                logger.info(f"Loaded {len(data)} entries from upstream data")
                return data
            logger.error(f"Unexpected format: {type(data).__name__}")
            return []
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt + 1}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
    logger.error("Failed to fetch compatibility data")
    return []


def get_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "issuefilter/1.0",
    }
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        logger.debug("Authorization token added to headers")
    else:
        logger.warning("No token — rate limits will be low (60 req/hour)")
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
    logger.debug(f"Pattern '{pat['name']}' found hex ID {id_} at {m.start()}-{m.end()}")
    before = title[:m.start()].strip()
    after = title[m.end():].strip()
    logger.debug(f"Before: '{before}', After: '{after}'")
    if before.endswith(("(", "[", "{")):
        before = before[:-1].strip()
    if after.startswith((")", "]", "}")):
        after = after[1:].strip()
    name = " ".join(filter(None, [
        re.sub(r"^\s*[-–—]?\s*|\s*[-–—]?\s*$", "", before),
        re.sub(r"^\s*[-–—]?\s*|\s*[-–—]?\s*$", "", after),
    ]))
    name = re.sub(r"\s+", " ", name).strip()
    if name:
        logger.debug(f"Extracted name: '{name}'")
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
        logger.debug("Empty title")
        return None

    logger.debug(f"Normalizing: '{title}'")

    for pat in TITLE_PATTERNS:
        extract = _extract_anywhere if pat.get("mode") == "anywhere" else _extract_id_name
        result = extract(pat, title)
        if not result:
            logger.debug(f"Pattern '{pat['name']}' — no match")
            continue
        id_, name = result
        normalized = f"{id_}{EXPECTED_SEPARATOR}{name}"
        logger.debug(f"Pattern '{pat['name']}' → '{normalized}'")
        if pat.get("check_unchanged") and normalized == title:
            logger.debug("Already correct")
            return None
        if normalized != title:
            logger.debug(f"Would change: '{title}' → '{normalized}'")
        return normalized

    logger.debug(f"No pattern matched: '{title}'")
    return None


# =========================
# GitHub API
# =========================


def should_skip(issue: dict) -> bool:
    """Return True if the issue is a PR or has an ignored label."""
    if "pull_request" in issue:
        logger.debug(f"#{issue.get('number')}: skipped — PR")
        return True
    for label in issue.get("labels", []):
        if isinstance(label, dict) and label.get("name") in IGNORED_LABELS:
            logger.debug(f"#{issue.get('number')}: skipped — label '{label.get('name')}'")
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
        logger.info(f"Page {page_num}")

        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(f"Attempt {attempt + 1}/{MAX_RETRIES}")
                response = requests.get(
                    url,
                    headers=get_headers(),
                    params={"per_page": PER_PAGE, "state": "open"},
                    timeout=TIMEOUT,
                )
                remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
                logger.debug(f"HTTP {response.status_code}, remaining: {remaining}")

                if response.status_code == 403:
                    reset = response.headers.get("X-RateLimit-Reset")
                    if reset:
                        wait = max(int(reset) - int(time.time()), RATE_LIMIT_MIN_WAIT)
                        logger.warning(f"Rate limited. Sleeping {wait}s...")
                        time.sleep(wait)
                        continue
                response.raise_for_status()
                data = response.json()
                page_size = len(data) if isinstance(data, list) else 0
                logger.debug(f"Page {page_num}: {page_size} items")

                if not isinstance(data, list) or not data:
                    logger.info(f"Page {page_num}: empty — done")
                    return all_issues

                logger.info(f"Page {page_num}: {page_size} issues")
                all_issues.extend(data)
                break

            except requests.RequestException as e:
                logger.error(f"Page {page_num}, attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    sleep_time = RETRY_BACKOFF_BASE * (attempt + 1)
                    logger.debug(f"Retry in {sleep_time}s...")
                    time.sleep(sleep_time)

        link = response.headers.get("Link", "")
        next_url = None
        if link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1:part.find(">")]
                    logger.debug("Next page found")
                    break
        else:
            logger.debug("No Link header — last page")

        url = next_url
        if url:
            time.sleep(PAGE_DELAY)

    logger.info(f"Fetched {len(all_issues)} issues from {owner}/{repo}")
    return all_issues


def update_title(owner: str, repo: str, number: int, new_title: str) -> bool:
    """PATCH a single issue's title. Returns True on success."""
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}"
    try:
        logger.debug(f"PATCH #{number}: '{new_title}'")
        response = requests.patch(
            url,
            headers=get_headers(),
            json={"title": new_title},
            timeout=TIMEOUT,
        )
        remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
        logger.debug(f"PATCH #{number} — {response.status_code}, remaining: {remaining}")
        response.raise_for_status()
        logger.info(f"Updated #{number}: '{new_title}'")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to update #{number}: {e}")
        return False


def post_comment(owner: str, repo: str, number: int, body: str) -> bool:
    """POST a comment on an issue. Returns True on success."""
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}/comments"
    try:
        logger.debug(f"Commenting on #{number}")
        response = requests.post(
            url,
            headers=get_headers(),
            json={"body": body},
            timeout=TIMEOUT,
        )
        remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
        logger.debug(f"POST comment #{number} — {response.status_code}, remaining: {remaining}")
        response.raise_for_status()
        logger.info(f"Commented on #{number}")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to comment on #{number}: {e}")
        return False


def close_issue(owner: str, repo: str, number: int) -> bool:
    """PATCH an issue to closed state. Returns True on success."""
    url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}"
    try:
        logger.debug(f"Closing #{number}")
        response = requests.patch(
            url,
            headers=get_headers(),
            json={"state": "closed", "state_reason": "duplicate"},
            timeout=TIMEOUT,
        )
        remaining = response.headers.get("X-RateLimit-Remaining", "unknown")
        logger.debug(f"Close #{number} — {response.status_code}, remaining: {remaining}")
        response.raise_for_status()
        logger.info(f"Closed #{number} as duplicate")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to close #{number}: {e}")
        return False


# =========================
# Duplicate detection
# =========================


def parse_title_simple(title: str) -> Optional[str]:
    """Extract hex ID from a title. Returns uppercase ID or None."""
    m = re.match(r"^\s*(?P<id>[0-9A-Fa-f]{8})\s*[-–—]?\s*(?P<name>.+)", title)
    if m:
        return m.group("id").upper()
    m = re.search(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{8})(?![0-9A-Fa-f])", title)
    if m:
        return m.group(1).upper()
    return None


def build_lookup_from_release(data: list) -> dict:
    """Build {hex_id: {issue, url, title}} from compatibility_data.json entries."""
    lookup = {}
    skipped = 0
    for entry in data:
        hex_id = entry.get("id", "").upper()
        if not hex_id:
            skipped += 1
            continue
        lookup[hex_id] = {
            "issue": entry["issue"],
            "url": entry["url"],
            "title": entry["title"],
        }
    logger.info(f"Lookup built: {len(lookup)} unique hex IDs from upstream data"
                f" ({skipped} entries skipped)")
    return lookup


def close_as_duplicate(owner: str, repo: str, number: int, original: dict) -> bool:
    """Post a comment and close the issue. Returns True on success."""
    comment = f"This game already has a compatibility report at xenia-canary#{original['issue']}."
    ok = post_comment(owner, repo, number, comment)
    if ok:
        ok = close_issue(owner, repo, number)
    return ok


# =========================
# Entry point
# =========================


def main():
    """Fetch issues, detect duplicates, normalize titles."""
    start_time = time.time()
    owner, repo = get_owner_repo()
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    event_path = os.getenv("GITHUB_EVENT_PATH", "")
    dry_run = os.getenv("DRY_RUN", "").lower() == "true"

    if event_name == "issues":
        dry_run = False

    logger.info("=" * 60)
    logger.info("issuefilter")
    logger.info("=" * 60)
    logger.info(f"Repository: {owner}/{repo}")
    logger.info(f"Event: {event_name}")
    logger.info(f"Mode: {'dry-run' if dry_run else 'live'}")

    if os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"):
        logger.info("Token provided")
    else:
        logger.warning("No token — rate limits will be low (60 req/hour)")

    # Build duplicate lookup from upstream compatibility data
    logger.info("-" * 60)
    logger.info("Building duplicate lookup from upstream release data...")
    raw_data = fetch_compatibility_data()
    lookup = build_lookup_from_release(raw_data)

    # Fetch target issues
    logger.info("-" * 60)
    issues = []

    if event_name == "issues" and event_path:
        logger.info("Reading issue from event payload...")
        with open(event_path) as f:
            payload = json.load(f)
        issue = payload.get("issue")
        if issue:
            issues = [issue]
            logger.info(f"Triggered by #{issue.get('number')}: \"{issue.get('title', '')}\"")
        else:
            logger.error("No issue data in payload")
            sys.exit(1)
    else:
        logger.info("Processing all open issues...")
        issues = fetch_all_issues(owner, repo)

    logger.info(f"Total: {len(issues)} issue(s)")

    logger.info("-" * 60)
    logger.info("Processing...")

    duplicates = []
    fixed = []
    unfixable = []
    unchanged = 0
    skipped = 0

    for idx, issue in enumerate(issues, 1):
        number = issue.get("number")
        title = issue.get("title", "")
        logger.debug(f"[{idx}/{len(issues)}] #{number}: \"{title}\"")

        if should_skip(issue):
            skipped += 1
            continue

        if not title.strip():
            logger.debug(f"#{number}: empty title")
            unfixable.append({"number": number, "old": title, "reason": "empty"})
            continue

        hex_id = parse_title_simple(title)
        if hex_id and hex_id in lookup and lookup[hex_id]["issue"] != number:
            dup = {"number": number, "old": title, "original": lookup[hex_id]}
            duplicates.append(dup)
            logger.info(f"#{number}: DUPLICATE of #{lookup[hex_id]['issue']} ({lookup[hex_id]['title']})")
            continue

        new_title = normalize_title(title)
        if new_title is None:
            unchanged += 1
            continue

        logger.info(f"#{number}: \"{title}\" → \"{new_title}\"")
        fixed.append({"number": number, "old": title, "new": new_title})

    # Report
    logger.info("-" * 60)
    logger.info(f"Results: {len(duplicates)} duplicates, {len(fixed)} to fix, "
                f"{len(unfixable)} unfixable, {unchanged} correct, {skipped} skipped")

    if unfixable:
        logger.warning("Unfixable (need manual review):")
        for f in unfixable:
            logger.warning(f"  #{f['number']}: \"{f['old']}\" ({f.get('reason', 'no ID/name')})")

    if duplicates:
        logger.info(f"Duplicates found: {len(duplicates)}")
        for d in duplicates:
            logger.info(f"  #{d['number']}: \"{d['old']}\" → "
                        f"duplicate of #{d['original']['issue']} ({d['original']['title']})")
        if not dry_run:
            logger.info("Closing duplicates...")
            results = []
            for d in duplicates:
                ok = close_as_duplicate(owner, repo, d["number"], d["original"])
                results.append((d["number"], ok))
            success = sum(1 for _, s in results if s)
            logger.info(f"Closed: {success}/{len(results)}")

    if fixed and not dry_run:
        logger.info(f"Applying {len(fixed)} title fix(es)...")
        results = []
        for f in fixed:
            ok = update_title(owner, repo, f["number"], f["new"])
            results.append((f["number"], ok))
        success = sum(1 for _, s in results if s)
        logger.info(f"Fixed: {success}/{len(results)}")

    elapsed = time.time() - start_time
    logger.info("-" * 60)
    logger.info(f"Completed in {elapsed:.2f}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
