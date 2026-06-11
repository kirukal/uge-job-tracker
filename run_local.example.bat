@echo off
rem Template for local runs — copy to run_local.bat and fill in secrets.
set "WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN"
set "ROLE_ID=YOUR_ROLE_ID"
set "RUN_ONCE=1"
set "STATE_FILE=%LOCALAPPDATA%\uge-job-tracker\state.json"
python "%~dp0main.py" >> "%LOCALAPPDATA%\uge-job-tracker\run.log" 2>&1
