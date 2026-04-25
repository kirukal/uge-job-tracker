import asyncio
import os
import time

import requests
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await stealth_async(page)

        await page.goto(JOB_URL, wait_until="networkidle", timeout=60000)

        try:
            await page.wait_for_selector("cx-result-field", timeout=25000)
        except Exception:
            pass

        while True:
            cards = await page.evaluate("""() => {
                const cards = document.querySelectorAll('cx-result-field');
                return Array.from(cards).map(card => {
                    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
                    const texts = [];
                    while (walker.nextNode()) {
                        const t = walker.currentNode.textContent.trim();
                        if (t) texts.push(t);
                    }
                    const h3 = card.querySelector('h3');
                    const a = card.querySelector('a');
                    return {
                        title: h3 ? h3.textContent.trim() : (texts[0] || ''),
                        href: a ? a.href : '',
                        texts: texts
                    };
                });
            }""")

            for card in cards:
                title = card.get("title", "")
                texts = card.get("texts", [])
                href = card.get("href", "") or JOB_URL
                if not title:
                    continue
                location = next((t for t in texts if "Nashville" in t or "Tennessee" in t or "BNA" in t), "")
                if not location:
                    continue
                job_type = "Part-Time" if "part-time" in title.lower() else "Full-Time" if "full-time" in title.lower() else ""
                req_id = next((t for t in texts if t.startswith("26-")), title)
                jobs[req_id] = {"title": title, "location": location, "type": job_type, "url": href}

            has_next = await page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.trim() === 'Next' && !b.disabled);
                if (btn) { btn.click(); return true; }
                return false;
            }""")

            if not has_next:
                break

            await page.wait_for_timeout(2000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

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
