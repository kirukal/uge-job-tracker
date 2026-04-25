import asyncio
import os
import time

import requests
from playwright.async_api import async_playwright

WEBHOOK_URL = os.environ["WEBHOOK_URL"]
ROLE_ID = os.environ["ROLE_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "90"))

JOB_URL = "https://myjobs.adp.com/unitedgroundexpressexternal/cx/job-listing"

seen_jobs: dict[str, dict] = {}


def send(content: str):
    requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)


def format_job(job: dict) -> str:
    title = job.get("title", "Unknown")
    location = job.get("location", "")
    job_type = job.get("type", "")
    url = job.get("url", "")
    tag = " — Alaska" if "alaska" in title.lower() else ""
    parts = [f"**{title}**{tag}"]
    if location:
        parts.append(f"📍 {location}")
    if job_type:
        parts.append(f"⏱ {job_type}")
    if url:
        parts.append(f"🔗 {url}")
    return " | ".join(parts)


async def scrape_jobs() -> dict[str, dict]:
    jobs = {}
    api_data: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Intercept API responses to get clean JSON job data
        async def handle_response(response):
            url = response.url
            if "job-postings" in url or "jobRequisitions" in url or "positions" in url:
                try:
                    data = await response.json()
                    if isinstance(data, dict):
                        items = (
                            data.get("value")
                            or data.get("items")
                            or data.get("jobs")
                            or data.get("jobRequisitions")
                            or []
                        )
                        api_data.extend(items)
                    elif isinstance(data, list):
                        api_data.extend(data)
                except Exception:
                    pass

        page.on("response", handle_response)

        await page.goto(JOB_URL, wait_until="networkidle", timeout=60000)

        # Wait for job cards to appear
        try:
            await page.wait_for_selector(
                "[class*='job'], [data-job], .cx-job, .job-card, [class*='position']",
                timeout=20000,
            )
        except Exception:
            pass

        # If we caught API data, use it
        if api_data:
            for item in api_data:
                title = (
                    item.get("jobTitle")
                    or item.get("title")
                    or item.get("positionTitle")
                    or item.get("name")
                    or ""
                )
                location = (
                    item.get("primaryLocation")
                    or item.get("location")
                    or item.get("locationName")
                    or ""
                )
                if isinstance(location, dict):
                    location = location.get("nameCode", {}).get("longName", "") or location.get("city", "")
                job_type = item.get("workSchedule") or item.get("jobType") or item.get("employmentType") or ""
                if isinstance(job_type, dict):
                    job_type = job_type.get("longName", "") or job_type.get("codeValue", "")
                job_id = str(item.get("jobRequisitionID") or item.get("id") or item.get("requisitionId") or title)
                job_url = item.get("applyUrl") or item.get("url") or ""

                loc_str = str(location).upper()
                if "NASHVILLE" in loc_str or "BNA" in loc_str or "TN" in loc_str:
                    jobs[job_id] = {
                        "title": title,
                        "location": str(location),
                        "type": str(job_type),
                        "url": job_url,
                    }
        else:
            # Fallback: parse rendered DOM
            cards = await page.query_selector_all(
                "[class*='job-card'], [class*='job-item'], [class*='position-card'], .cx-job-listing-item"
            )
            for card in cards:
                text = (await card.inner_text()).strip()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if not lines:
                    continue
                title = lines[0]
                location = next((l for l in lines if "Nashville" in l or "BNA" in l), "")
                if not location:
                    continue
                job_type = next(
                    (l for l in lines if any(w in l.lower() for w in ["full", "part", "time"])), ""
                )
                href = await card.get_attribute("href") or ""
                if not href:
                    a = await card.query_selector("a")
                    if a:
                        href = await a.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = "https://myjobs.adp.com" + href
                job_id = title + "|" + location
                jobs[job_id] = {
                    "title": title,
                    "location": location,
                    "type": job_type,
                    "url": href,
                }

        await browser.close()

    return jobs


def check_jobs(current: dict[str, dict]):
    global seen_jobs

    new_jobs = {k: v for k, v in current.items() if k not in seen_jobs}
    removed_jobs = {k: v for k, v in seen_jobs.items() if k not in current}
    is_first_run = not seen_jobs

    if not current:
        if is_first_run or removed_jobs:
            send(f"<@&{ROLE_ID}>\n⚠️ No BNA jobs found. Site may have changed structure or blocked scraper.")
        seen_jobs = current
        return

    parts = []

    lines = "\n".join(format_job(v) for v in current.values())
    parts.append(f"📋 **CURRENT BNA JOBS ({len(current)}):**\n{lines}")

    if new_jobs:
        lines = "\n".join(format_job(v) for v in new_jobs.values())
        parts.append(f"✈️ **NEW ({len(new_jobs)}):**\n{lines}")

    if removed_jobs:
        lines = "\n".join(format_job(v) for v in removed_jobs.values())
        parts.append(f"❌ **REMOVED ({len(removed_jobs)}):**\n{lines}")

    if new_jobs or removed_jobs or is_first_run:
        message = f"<@&{ROLE_ID}>\n\n" + "\n\n".join(parts)
        # Discord max 2000 chars — chunk if needed
        if len(message) > 1900:
            chunks = []
            current_chunk = f"<@&{ROLE_ID}>\n\n"
            for part in parts:
                if len(current_chunk) + len(part) + 2 > 1900:
                    chunks.append(current_chunk.strip())
                    current_chunk = part + "\n\n"
                else:
                    current_chunk += part + "\n\n"
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            for chunk in chunks:
                send(chunk)
                time.sleep(1)
        else:
            send(message)

    seen_jobs = current


async def main():
    print("UGE Job Tracker starting...")
    first = True
    while True:
        try:
            print("Checking jobs...")
            current = await scrape_jobs()
            print(f"Found {len(current)} BNA jobs")
            check_jobs(current)
        except Exception as e:
            print(f"Error: {e}")
            if first:
                send(f"<@&{ROLE_ID}>\n⚠️ Tracker error on startup: {e}")
        first = False
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
