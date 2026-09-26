#!/usr/bin/env sh
# Single-image entrypoint: the first argument selects which process this container runs.
# The only difference between self-host and SaaS is configuration.
set -eu

case "${1:-api}" in
  api)
    exec uvicorn api.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec python -m worker.main
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    echo "unknown entrypoint command: ${1:-}" >&2
    echo "expected one of: api | worker | migrate" >&2
    exit 1
    ;;
esac
