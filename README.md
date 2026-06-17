# UGE Job Tracker

A lightweight job-posting watcher for [United Ground Express](https://myjobs.adp.com/unitedgroundexpressexternal/cx) (and any other career site hosted on the **ADP CX** platform). It polls the career site on a schedule, diffs the results against saved state, and posts **new**, **removed**, and **closing-soon** roles to a Discord channel with a role ping.

No login, no headless browser, no scraping HTML — it talks to the same JSON API the career-site SPA uses.

## How it works

ADP's CX career sites expose a public config endpoint that hands out a short-lived, anonymous `myJobsToken` with no cookies or auth. That token is enough to call the `job-requisitions/apply-custom-filters` endpoint and pull **every published requisition** for the tenant as JSON:

1. `GET /public/staffing/v1/career-site/<tenant>` → grab a fresh `myJobsToken`.
2. `GET /myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters` with that token (paginated) → all open requisitions.
3. Filter by location, diff against the last run, and notify.

Because the source is a clean JSON API rather than rendered HTML, it's fast and doesn't break every time the front end changes.

## Features

- **Change detection** — alerts on newly posted and removed jobs, plus a **closing-soon** warning (default: within 3 days of the application deadline).
- **No false removals** — a job must be missing for N consecutive checks (`REMOVAL_CONFIRMATIONS`, default 2) before it's reported as removed.
- **Location filter** — only notify on jobs matching a comma-separated list of terms (default `bna,nashville`).
- **Discord delivery** — role-mention pings, messages auto-chunked under Discord's 2000-character limit.
- **Resilient** — retries with backoff, throttled failure alerts, atomic state writes, graceful shutdown on SIGTERM/SIGINT.
- **Works for any ADP CX tenant** — point `ADP_TENANT` at a different employer's career site and it just works.

## Setup

Requires Python 3.12+.

```bash
pip install -r requirements.txt
```

You need a [Discord webhook URL](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks) and the role ID you want pinged.

### Run locally (Windows)

Copy the template, fill in your secrets, and run it:

```bat
copy run_local.example.bat run_local.bat
:: edit run_local.bat — set WEBHOOK_URL and ROLE_ID
run_local.bat
```

`run_local.bat` is gitignored so your webhook never gets committed. On Windows, point Task Scheduler at it to run a few times a day.

### Run anywhere

```bash
WEBHOOK_URL="https://discord.com/api/webhooks/..." \
ROLE_ID="123456789012345678" \
python main.py
```

### Docker / Railway

A `Dockerfile` and `railway.toml` are included for always-on hosting:

```bash
docker build -t uge-job-tracker .
docker run -e WEBHOOK_URL=... -e ROLE_ID=... uge-job-tracker
```

## Configuration

All configuration is via environment variables.

| Variable | Required | Default | Description |
|---|---|---|---|
| `WEBHOOK_URL` | **yes** | — | Discord webhook URL to post to. |
| `ROLE_ID` | **yes** | — | Discord role ID to mention on alerts. |
| `ADP_TENANT` | no | `unitedgroundexpressexternal` | ADP CX tenant slug (the employer). |
| `LOCATION_FILTER` | no | `bna,nashville` | Comma-separated location/title terms; empty = no filter. |
| `CHECK_INTERVAL` | no | `300` | Seconds between checks (ignored if `RUN_ONCE`). |
| `CLOSING_SOON_DAYS` | no | `3` | Warn when a job closes within this many days; `0` disables. |
| `REMOVAL_CONFIRMATIONS` | no | `2` | Consecutive misses before a job is reported removed. |
| `FAILURE_ALERT_INTERVAL` | no | `3600` | Min seconds between repeated failure alerts. |
| `FETCH_RETRIES` | no | `3` | HTTP retry attempts per request. |
| `STATE_FILE` | no | `/tmp/uge-job-tracker-state.json` | Where run state is persisted. |
| `ANNOUNCE_FIRST_RUN` | no | `false` | Post the full current job list on first run. |
| `RUN_ONCE` | no | `false` | Run a single check and exit (for cron/Task Scheduler). |
| `DRY_RUN` | no | `false` | Log messages instead of sending to Discord. |

## License

[MIT](LICENSE)
