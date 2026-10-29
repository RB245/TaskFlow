#!/bin/sh
# Belt-and-braces guard for environments without compose healthchecks (e.g.
# a PaaS that starts everything at once). Compose already gates on
# `service_healthy`, so this normally returns immediately.
set -eu

ATTEMPTS="${DB_WAIT_ATTEMPTS:-30}"

i=1
while [ "$i" -le "$ATTEMPTS" ]; do
  if python - <<'PY'
import sys

from sqlalchemy import create_engine, text

from app.core.config import settings

try:
    create_engine(settings.database_url, pool_pre_ping=True).connect().execute(text("SELECT 1"))
except Exception:
    sys.exit(1)
PY
  then
    echo "database is ready"
    exit 0
  fi
  echo "waiting for database ($i/$ATTEMPTS)..."
  i=$((i + 1))
  sleep 2
done

echo "database did not become ready in time" >&2
exit 1
