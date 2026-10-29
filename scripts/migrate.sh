#!/bin/sh
# Migrations run in their own one-shot container rather than in each service's
# entrypoint. If the API, the worker and beat all ran `alembic upgrade` on
# boot they would race for the same advisory lock, and two of them would
# either block or fail during every deploy.
set -eu

./scripts/wait-for-db.sh
exec alembic upgrade head
