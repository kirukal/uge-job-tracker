import json
import os
import signal
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import requests

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
LOCATION_FILTER = os.getenv("LOCATION_FILTER", "bna,nashville").lower()

# ADP CX career-site API (discovered 2026-06-11 by sniffing the SPA's XHR traffic):
# 1. The public career-site config endpoint returns a short-lived `myJobsToken`
#    with no cookies or auth required.
# 2. job-requisitions/apply-custom-filters on my.adp.com accepts that token as a
#    header (plus rolecode=manager, which is what the SPA itself sends) and
#    returns every published requisition for the tenant as JSON.
ADP_TENANT = os.getenv("ADP_TENANT", "unitedgroundexpressexternal")
ADP_CONFIG_URL = f"https://myjobs.adp.com/public/staffing/v1/career-site/{ADP_TENANT}"
ADP_SEARCH_URL = (
    "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1"
    "/job-requisitions/apply-custom-filters"
)
ADP_SEARCH_PARAMS = {
    "$select": (
        "reqId,jobTitle,publishedJobTitle,type,clientRequisitionID,"
        "postingDate,requisitionLocations"
    ),
    "$top": "200",
    "$filter": "",
    "tz": "America/Chicago",
}
ADP_JOB_URL = (
    f"https://myjobs.adp.com/{ADP_TENANT}/cx/job-listing?keyword={{req_number}}"
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US",
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


def fetch_with_retry(url: str, **kwargs: Any) -> requests.Response:
    last_error: requests.RequestException | None = None

    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            response = requests.get(url, timeout=25, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < FETCH_RETRIES:
                time.sleep(min(2**attempt, 10))

    raise last_error or requests.RequestException(f"Failed to fetch {url}")


def compact(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_date(value: str | None) -> date | None:
    value = compact(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def fetch_my_jobs_token() -> str:
    """Grab a fresh anonymous myJobsToken from the public career-site config."""
    resp = fetch_with_retry(ADP_CONFIG_URL, headers=HTTP_HEADERS)
    data = resp.json()
    token = compact(str(data.get("myJobsToken") or ""))
    if not token:
        raise requests.RequestException("career-site config had no myJobsToken")
    return token


def format_req_location(loc: dict[str, Any]) -> str:
    address = loc.get("address") or {}
    city = compact(address.get("cityName") or "")
    region = compact((address.get("countrySubdivisionLevel1") or {}).get("codeValue") or "")
    code = compact((loc.get("nameCode") or {}).get("codeValue") or "")
    parts = [p for p in [city, region] if p]
    label = ", ".join(parts)
    if code:
        label = f"{label} ({code})" if label else code
    return label


def infer_job_type(title: str, raw_type: Any) -> str:
    lower = title.lower()
    if "part-time" in lower or "part time" in lower:
        return "Part-Time"
    if "full-time" in lower or "full time" in lower:
        return "Full-Time"
    raw = compact(str(raw_type or ""))
    return "" if raw in {"", "None", "Normal"} else raw


def parse_adp_job(raw: dict[str, Any]) -> dict[str, str] | None:
    req_id = compact(str(raw.get("reqId") or ""))
    title = compact(str(raw.get("publishedJobTitle") or raw.get("jobTitle") or ""))

    if not req_id or not title:
        return None

    locations = raw.get("requisitionLocations") or []
    location_str = "; ".join(
        filter(None, (format_req_location(loc) for loc in locations))
    )

    if LOCATION_FILTER:
        haystack = f"{title} {location_str}".lower()
        if not any(term.strip() in haystack for term in LOCATION_FILTER.split(",")):
            return None

    req_number = compact(str(raw.get("clientRequisitionID") or ""))
    posted = compact(str(raw.get("postingDate") or ""))
    valid_through = compact(
        str(raw.get("validThrough") or raw.get("applicationsAcceptedThroughDate") or "")
    )

    vt_date = parse_date(valid_through)
    if vt_date is not None and vt_date < date.today():
        return None

    return {
        "title": title,
        "location": location_str,
        "type": infer_job_type(title, raw.get("type")),
        "req_number": req_number,
        "url": ADP_JOB_URL.format(req_number=req_number or req_id),
        "posted": posted[:10] if posted else "",
        "valid_through": valid_through[:10] if valid_through else "",
    }


def scrape_jobs() -> dict[str, dict[str, str]]:
    token = fetch_my_jobs_token()
    headers = {**HTTP_HEADERS, "rolecode": "manager", "myJobsToken": token}

    raw_jobs: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {**ADP_SEARCH_PARAMS, "$skip": str(offset)}
        resp = fetch_with_retry(ADP_SEARCH_URL, headers=headers, params=params)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise requests.RequestException(f"ADP search returned non-JSON: {exc}")

        page = data.get("jobRequisitions") or []
        raw_jobs.extend(page)
        total = int(data.get("count") or len(raw_jobs))
        offset += len(page)
        if not page or offset >= total:
            break

    log(f"ADP returned {len(raw_jobs)} total requisitions for {ADP_TENANT}")

    jobs: dict[str, dict[str, str]] = {}
    for raw in raw_jobs:
        parsed = parse_adp_job(raw)
        if parsed is None:
            continue
        key = compact(str(raw.get("reqId") or parsed["req_number"] or parsed["url"]))
        jobs[key] = parsed

    return jobs


def format_job(job: dict[str, str]) -> str:
    title = job.get("title", "Unknown")
    location = job.get("location", "")
    job_type = job.get("type", "")
    req_number = job.get("req_number", "")
    url = job.get("url", "")
    posted = job.get("posted", "")
    valid_through = job.get("valid_through", "")

    parts = [f"**{title}**"]
    if req_number:
        parts.append(f"Req: {req_number}")
    if location:
        parts.append(f"Location: {location}")
    if job_type:
        parts.append(f"Type: {job_type}")
    if posted:
        parts.append(f"Posted: {posted}")
    if valid_through:
        parts.append(f"Closes: {valid_through}")
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
        vt = parse_date(job.get("valid_through"))
        if vt is None or key in already_alerted:
            continue
        days_left = (vt - today).days
        if 0 <= days_left <= CLOSING_SOON_DAYS:
            soon[key] = job

    return soon


def check_jobs(current: dict[str, dict[str, str]]) -> None:
    if not current:
        log("No jobs found; keeping previous state unchanged.")
        maybe_send_failure_alert(
            "No BNA UGE jobs found from ADP. Keeping previous state to avoid false removals."
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
        f"(source=ADP CX API, tenant={ADP_TENANT}, interval={CHECK_INTERVAL}s, "
        f"removal_confirmations={REMOVAL_CONFIRMATIONS}, "
        f"closing_soon_days={CLOSING_SOON_DAYS}, location_filter={LOCATION_FILTER!r}, "
        f"state_file={STATE_FILE})"
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
