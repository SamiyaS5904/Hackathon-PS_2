#!/bin/bash
# Starts both OfferPilot programs inside the container (Fly.io). If either stops, the container exits and Fly
# restarts it, so the dashboard/autopilot and the bots are always up together.
set -e

# First start only: put the database on the persistent volume (later deploys keep the live data there)
if [ ! -f "${DB_PATH:-/data/offerpilot.db}" ]; then
  mkdir -p "$(dirname "${DB_PATH:-/data/offerpilot.db}")"
  cp /app/data/offerpilot.db "${DB_PATH:-/data/offerpilot.db}"
  echo "Copied the database to ${DB_PATH:-/data/offerpilot.db}"
fi

python -m uvicorn api.main:app --host 0.0.0.0 --port 8080 &
python -u -m bot.telegram_bot &
wait -n
exit $?
