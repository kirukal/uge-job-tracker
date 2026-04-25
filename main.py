import html
import json
import os
import re
import time
from datetime import date
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
ROLE_ID = os.environ["ROLE_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
FAILURE_ALERT_INTERVAL = int(os.getenv("FAILURE_ALERT_INTERVAL", "3600"))

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

seen_jobs: dict[str, dict[str, str]] | None = None
last_failure_alert_at = 0.0


def send(content: str) -> bool:
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
        print(f"Discord send failed: {exc}")
        return False

    return True


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HTTP_HEADERS, timeout=25)
    response.raise_for_status()
    return response.text


def compact_text(value: str) -> str:
    return " ".join(html.unescape(value or "").split())


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
    valid_through = compact_text(str(job_posting.get("validThrough") or ""))
    if not valid_through:
        return True

    try:
        return date.fromisoformat(valid_through[:10]) >= date.today()
    except ValueError:
        return True


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


def extract_job_posting(page_html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(page_html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue

        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
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
            print(f"Failed to fetch {url}: {exc}")
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
    tag = " - Alaska" if "alaska" in title.lower() else ""

    parts = [f"**{title}**{tag}"]
    if location:
        parts.append(f"Location: {location}")
    if job_type:
        parts.append(f"Type: {job_type}")
    if posted:
        parts.append(f"Posted: {posted}")
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


def maybe_send_failure_alert(message: str) -> None:
    global last_failure_alert_at

    now = time.time()
    if now - last_failure_alert_at < FAILURE_ALERT_INTERVAL:
        return

    send(f"<@&{ROLE_ID}>\nWarning: {message}")
    last_failure_alert_at = now


def check_jobs(current: dict[str, dict[str, str]]) -> None:
    global seen_jobs

    if not current:
        print("No jobs found; keeping previous state unchanged.")
        maybe_send_failure_alert(
            "No BNA jobs found from BeBee mirror. Keeping previous state to avoid false removals/spam."
        )
        return

    is_first_run = seen_jobs is None
    previous = seen_jobs or {}
    new_jobs = {key: value for key, value in current.items() if key not in previous}
    removed_jobs = {key: value for key, value in previous.items() if key not in current}

    parts: list[str] = []

    lines = "\n".join(format_job(job) for job in current.values())
    parts.append(f"CURRENT BNA JOBS ({len(current)}):\n{lines}")

    if new_jobs:
        lines = "\n".join(format_job(job) for job in new_jobs.values())
        parts.append(f"NEW ({len(new_jobs)}):\n{lines}")

    if removed_jobs:
        lines = "\n".join(format_job(job) for job in removed_jobs.values())
        parts.append(f"REMOVED ({len(removed_jobs)}):\n{lines}")

    if is_first_run or new_jobs or removed_jobs:
        if not send_chunks(parts):
            print("Notification failed; preserving previous state for retry.")
            return

    seen_jobs = current


def main() -> None:
    print("UGE Job Tracker starting...")

    while True:
        try:
            print("Checking jobs...")
            current = scrape_jobs()
            print(f"Found {len(current)} BNA jobs")
            check_jobs(current)
        except Exception as exc:
            print(f"Error: {exc}")
            maybe_send_failure_alert(f"Tracker error: {exc}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
