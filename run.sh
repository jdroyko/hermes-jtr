#!/bin/sh
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
export SECRET_KEY="${SECRET_KEY:-dev-only-change-me}"
exec .venv/bin/flask --app app run --debug "$@"
