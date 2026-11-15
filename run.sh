#!/usr/bin/env bash
# Linux / macOS üçün lokal işə salma
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo "Server: http://localhost:8000"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
