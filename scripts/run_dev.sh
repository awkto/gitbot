#!/bin/bash
# Start the dev server with auto-reload
set -e
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env file found. Copying from .env.example ..."
    cp .env.example .env
    echo "Edit .env with your settings, then re-run."
    exit 1
fi

source .venv/bin/activate
exec uvicorn gitbot.server:app --reload --host 0.0.0.0 --port 8042
