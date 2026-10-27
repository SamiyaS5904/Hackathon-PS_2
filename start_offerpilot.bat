@echo off
REM ==================================================================================================
REM  Start OfferPilot on this laptop: double-click this file.
REM  Opens two windows (keep both open during the demo) and the dashboard in your browser:
REM    1. Dashboard + API + Autopilot (plans and runs the day's offers)  -> http://127.0.0.1:8010
REM    2. Telegram bots (sends each offer through its category bot)
REM  After changing .env (e.g. adding bot tokens), close both windows and double-click again.
REM ==================================================================================================
cd /d "%~dp0"
start "OfferPilot - Dashboard + Autopilot" cmd /k python -m uvicorn api.main:app --host 127.0.0.1 --port 8010
start "OfferPilot - Telegram bots" cmd /k python -u -m bot.telegram_bot
timeout /t 5 >nul
start "" http://127.0.0.1:8010
