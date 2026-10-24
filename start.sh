#!/bin/bash
# Start the echo-english local server: http://127.0.0.1:8000
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing edge-tts..."
  python3 -m venv .venv && .venv/bin/pip install -q edge-tts || exit 1
fi

exec .venv/bin/python server.py
