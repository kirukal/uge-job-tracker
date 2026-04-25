import html
import json
import os
import re
import signal
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
ROLE_ID = os.environ["ROLE_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
FAILURE_ALERT_INTERVAL = int(os.getenv("FAILURE_ALERT_INTERVAL", "3600"))
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/uge-job-tracker-state.json"))
REMOVAL_CONFIRMATIONS = int(os.getenv("REMOVAL_CONFIRMATIONS", "2"))
CLOSING_SOON_DAYS = int(os.getenv("CLOSING_SOON_DAYS", "3"))
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
ANNOUNCE_FIRST_RUN = os.getenv("ANNOUNCE_FIRST_RUN", "").lower() in {"1", "true", "yes"}
RUN_ONCE = os.getenv("RUN_ONCE", "").lower() in {"1", "true", "yes"}
DRY_RUN = os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes"}

BEEBEE_BASE_URL = "https://bebee.com"
BEEBEE_SEARCH_QUERIES = [
    "BNA United Ground Express",
    "United Ground Express Customer Service Agent BNA",
    "United Ground Express Airport Ramp Agent BNA",
    "United Ground Express Airport Supervisor BNA",
    "United Ground Express GSE Mechanic BNA",
    "United Ground Express Aircraft Fueling BNA",
    "United Ground Express Quality Control BNA",
]

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

state: dict[str, Any] = {}
last_failure_alert_at = 0.0
should_stop = False


def empty_state() -> dict[str, Any]:
    return {
        "known_jobs": {},
        "missing_counts": {},
        "closing_alerted": [],
        "last_success_at": "",
    }


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def send(content: str) -> bool:
    if DRY_RUN:
        log(f"DRY_RUN Discord message suppressed: {content[:500]}")
        return True

    try:
        response = requests.post(
            WEBHOOK_URL,
            json={
                "content": content,
                "allowed_mentions": {"roles": [ROLE_ID]},
            },
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log(f"Discord send failed: {exc}")
        return False

    return True


def fetch_html(url: str) -> str:
    last_error: requests.RequestException | None = None

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            response = requests.get(url, headers=HTTP_HEADERS, timeout=25)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < FETCH_RETRIES:
                time.sleep(min(2**attempt, 10))

    raise last_error or requests.RequestException(f"Failed to fetch {url}")


def compact_text(value: str) -> str:
    return " ".join(html.unescape(value or "").split())


def parse_date(value: str) -> date | None:
    value = compact_text(value)
    if not value:
        return None

    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def get_org_name(job_posting: dict[str, Any]) -> str:
    organization = job_posting.get("hiringOrganization")
    if isinstance(organization, dict):
        return compact_text(str(organization.get("name") or ""))
    return compact_text(str(organization or ""))


def get_location(job_posting: dict[str, Any]) -> str:
    location = job_posting.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else {}

    if isinstance(location, dict):
        address = location.get("address")
        if isinstance(address, dict):
            locality = compact_text(str(address.get("addressLocality") or ""))
            region = compact_text(str(address.get("addressRegion") or ""))
            country = compact_text(str(address.get("addressCountry") or ""))
            if locality and ("," in locality or region.lower() in {"davidson"}):
                return locality
            parts = [part for part in [locality, region, country] if part]
            return ", ".join(parts)
        return compact_text(str(location.get("name") or ""))

    return compact_text(str(location or ""))


def normalize_job_type(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value)

    raw = compact_text(str(value or "")).replace("_", " ").title()
    return raw if raw and raw != "None" else ""


def infer_job_type(title: str, value: Any) -> str:
    lower_title = title.lower()
    if "part-time" in lower_title or "part time" in lower_title:
        return "Part-Time"
    if "full-time" in lower_title or "full time" in lower_title:
        return "Full-Time"

    normalized = normalize_job_type(value)
    return "" if normalized == "Contractor" else normalized


def get_job_key(job_posting: dict[str, Any], fallback_url: str) -> str:
    identifier = job_posting.get("identifier")
    if isinstance(identifier, dict) and identifier.get("value"):
        return compact_text(str(identifier["value"]))

    match = re.search(r"--([a-z]+-\d+)", fallback_url)
    if match:
        return match.group(1)

    return fallback_url.rstrip("/")


def is_active(job_posting: dict[str, Any]) -> bool:
    valid_through = parse_date(str(job_posting.get("validThrough") or ""))
    if valid_through is None:
        return True

    return valid_through >= date.today()


def is_bna_uge_job(job_posting: dict[str, Any], url: str) -> bool:
    title = compact_text(str(job_posting.get("title") or ""))
    organization = get_org_name(job_posting)
    location = get_location(job_posting)
    description = compact_text(str(job_posting.get("description") or ""))

    haystack = " ".join([title, organization, location, description, url]).lower()
    return (
        "united ground express" in organization.lower()
        and ("bna" in haystack or "nashville" in haystack)
        and is_active(job_posting)
    )


def iter_jsonld_items(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        item_type = payload.get("@type")
        if item_type == "JobPosting" or (
            isinstance(item_type, list) and "JobPosting" in item_type
        ):
            yield payload

        for key, value in payload.items():
            if key in {"@context"}:
                continue
            if isinstance(value, (dict, list)):
                yield from iter_jsonld_items(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from iter_jsonld_items(item)


def extract_job_posting(page_html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(page_html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue

        for item in iter_jsonld_items(payload):
            return item

    return None


def discover_bebee_job_urls() -> list[str]:
    urls: dict[str, None] = {}

    for query in BEEBEE_SEARCH_QUERIES:
        search_url = (
            f"{BEEBEE_BASE_URL}/us/jobs"
            f"?q={quote(query)}&location=Nashville%2C+TN"
        )
        page_html = fetch_html(search_url)
        soup = BeautifulSoup(page_html, "html.parser")

        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            text = compact_text(link.get_text(" ", strip=True))
            candidate = f"{text} {href}".lower()

            if (
                "/us/jobs/" in href
                and "united-ground-express" in href
                and ("bna" in candidate or "nashville" in candidate)
            ):
                urls[urljoin(BEEBEE_BASE_URL, href)] = None

    return list(urls.keys())


def scrape_jobs() -> dict[str, dict[str, str]]:
    jobs: dict[str, dict[str, str]] = {}

    for url in discover_bebee_job_urls():
        try:
            page_html = fetch_html(url)
            job_posting = extract_job_posting(page_html)
        except requests.RequestException as exc:
            log(f"Failed to fetch {url}: {exc}")
            continue

        if not job_posting or not is_bna_uge_job(job_posting, url):
            continue

        key = get_job_key(job_posting, url)
        title = compact_text(str(job_posting.get("title") or "Unknown"))
        jobs[key] = {
            "title": title,
            "location": get_location(job_posting),
            "type": infer_job_type(title, job_posting.get("employmentType")),
            "url": compact_text(str(job_posting.get("url") or url)),
            "posted": compact_text(str(job_posting.get("datePosted") or "")),
            "valid_through": compact_text(str(job_posting.get("validThrough") or "")),
        }

    return jobs


def format_job(job: dict[str, str]) -> str:
    title = job.get("title", "Unknown")
    location = job.get("location", "")
    job_type = job.get("type", "")
    url = job.get("url", "")
    posted = job.get("posted", "")
    valid_through = job.get("valid_through", "")
    tag = " - Alaska" if "alaska" in title.lower() else ""

    parts = [f"**{title}**{tag}"]
    if location:
        parts.append(f"Location: {location}")
    if job_type:
        parts.append(f"Type: {job_type}")
    if posted:
        parts.append(f"Posted: {posted[:10]}")
    if valid_through:
        parts.append(f"Closes: {valid_through[:10]}")
    if url:
        parts.append(f"Apply: {url}")
    return " | ".join(parts)


def send_chunks(parts: list[str]) -> bool:
    message = f"<@&{ROLE_ID}>\n\n" + "\n\n".join(parts)
    if len(message) <= 1900:
        return send(message)

    all_sent = True
    current_chunk = f"<@&{ROLE_ID}>\n\n"
    for part in parts:
        if len(current_chunk) + len(part) + 2 > 1900:
            all_sent = send(current_chunk.strip()) and all_sent
            time.sleep(1)
            current_chunk = f"<@&{ROLE_ID}>\n\n{part}\n\n"
        else:
            current_chunk += part + "\n\n"

    if current_chunk.strip():
        all_sent = send(current_chunk.strip()) and all_sent

    return all_sent


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return empty_state()

    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"State file unreadable; starting fresh: {exc}")
        return empty_state()

    merged = empty_state()
    if isinstance(loaded, dict):
        merged.update(loaded)
    if not isinstance(merged.get("known_jobs"), dict):
        merged["known_jobs"] = {}
    if not isinstance(merged.get("missing_counts"), dict):
        merged["missing_counts"] = {}
    if not isinstance(merged.get("closing_alerted"), list):
        merged["closing_alerted"] = []
    return merged


def save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")
    tmp_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_file.replace(STATE_FILE)


def maybe_send_failure_alert(message: str) -> None:
    global last_failure_alert_at

    now = time.time()
    if now - last_failure_alert_at < FAILURE_ALERT_INTERVAL:
        return

    send(f"<@&{ROLE_ID}>\nWarning: {message}")
    last_failure_alert_at = now


def get_closing_soon_jobs(current: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    if CLOSING_SOON_DAYS <= 0:
        return {}

    already_alerted = set(state.get("closing_alerted", []))
    today = date.today()
    soon: dict[str, dict[str, str]] = {}

    for key, job in current.items():
        valid_through = parse_date(job.get("valid_through", ""))
        if valid_through is None or key in already_alerted:
            continue

        days_left = (valid_through - today).days
        if 0 <= days_left <= CLOSING_SOON_DAYS:
            soon[key] = job

    return soon


def check_jobs(current: dict[str, dict[str, str]]) -> None:
    if not current:
        log("No jobs found; keeping previous state unchanged.")
        maybe_send_failure_alert(
            "No BNA jobs found from BeBee mirror. Keeping previous state to avoid false removals/spam."
        )
        return

    previous: dict[str, dict[str, str]] = state.get("known_jobs", {})
    missing_counts: dict[str, int] = {
        key: int(value) for key, value in state.get("missing_counts", {}).items()
    }
    is_first_run = not previous
    new_jobs = (
        {}
        if is_first_run
        else {key: value for key, value in current.items() if key not in previous}
    )

    confirmed_removed: dict[str, dict[str, str]] = {}
    for key, job in previous.items():
        if key in current:
            missing_counts.pop(key, None)
            continue

        missing_counts[key] = missing_counts.get(key, 0) + 1
        if missing_counts[key] >= REMOVAL_CONFIRMATIONS:
            confirmed_removed[key] = job

    for key in confirmed_removed:
        previous.pop(key, None)
        missing_counts.pop(key, None)

    parts: list[str] = []

    if is_first_run and ANNOUNCE_FIRST_RUN:
        lines = "\n".join(format_job(job) for job in current.values())
        parts.append(f"TRACKER ONLINE - CURRENT BNA JOBS ({len(current)}):\n{lines}")

    if new_jobs:
        lines = "\n".join(format_job(job) for job in new_jobs.values())
        parts.append(f"NEW ({len(new_jobs)}):\n{lines}")

    if confirmed_removed:
        lines = "\n".join(format_job(job) for job in confirmed_removed.values())
        parts.append(f"REMOVED ({len(confirmed_removed)}):\n{lines}")

    closing_soon = get_closing_soon_jobs(current)
    if closing_soon:
        lines = "\n".join(format_job(job) for job in closing_soon.values())
        parts.append(f"CLOSING SOON ({len(closing_soon)}):\n{lines}")

    if parts and not send_chunks(parts):
        log("Notification failed; preserving previous state for retry.")
        return

    if not parts:
        log("No Discord-worthy job changes.")

    for key in closing_soon:
        if key not in state["closing_alerted"]:
            state["closing_alerted"].append(key)

    state["known_jobs"] = {**previous, **current}
    state["missing_counts"] = missing_counts
    state["last_success_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    save_state()


def handle_shutdown(signum: int, _frame: Any) -> None:
    global should_stop

    should_stop = True
    log(f"Received signal {signum}; shutting down after current check.")


def main() -> None:
    global state

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    state = load_state()
    log(
        "UGE Job Tracker starting "
        f"(interval={CHECK_INTERVAL}s, removal_confirmations={REMOVAL_CONFIRMATIONS}, "
        f"closing_soon_days={CLOSING_SOON_DAYS}, state_file={STATE_FILE})"
    )

    while not should_stop:
        try:
            log("Checking jobs...")
            current = scrape_jobs()
            log(f"Found {len(current)} BNA jobs")
            check_jobs(current)
        except Exception as exc:
            log(f"Error: {exc}")
            maybe_send_failure_alert(f"Tracker error: {exc}")

        if RUN_ONCE:
            break

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
