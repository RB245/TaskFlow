#!/usr/bin/env bash
#
# Deploy TaskFlow to a single Linux VM using the repo's own docker-compose.yml.
# This is the "any VM" path; deploy/render.yaml is the managed-PaaS path.
#
# NOT EXECUTED - see the README. It is written to be runnable, but no live
# host has been provisioned.
#
# Usage:
#   ./deploy/deploy.sh user@host [git-ref]
#
# Prerequisites on the target host: docker with the compose plugin, and a
# checkout (or deploy key) for this repository.

set -euo pipefail

TARGET="${1:?usage: deploy.sh user@host [git-ref]}"
REF="${2:-main}"
APP_DIR="${APP_DIR:-/opt/taskflow}"
ENV_FILE="${ENV_FILE:-/etc/taskflow/taskflow.env}"

echo "==> Deploying ${REF} to ${TARGET}:${APP_DIR}"

ssh "${TARGET}" bash -euo pipefail <<REMOTE
  if [ ! -d "${APP_DIR}/.git" ]; then
    echo "--> First run: cloning"
    sudo mkdir -p "${APP_DIR}"
    sudo chown "\$(id -u):\$(id -g)" "${APP_DIR}"
    git clone https://github.com/your-org/taskflow.git "${APP_DIR}"
  fi

  cd "${APP_DIR}"
  git fetch --all --tags --prune
  git checkout "${REF}"
  git pull --ff-only origin "${REF}" || true

  # Secrets live outside the repo, root-owned and 0600. JWT_SECRET must be
  # set here; the compose default is a development placeholder and the app
  # will happily boot with it, which is exactly the mistake to avoid.
  if [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: ${ENV_FILE} is missing." >&2
    echo "Create it with at least:" >&2
    echo "  JWT_SECRET=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" >&2
    echo "  ENVIRONMENT=production" >&2
    exit 1
  fi

  echo "--> Building"
  docker compose --env-file "${ENV_FILE}" build

  # The migrate service is one-shot and everything else waits on it, so the
  # schema is always current before the new API accepts traffic.
  echo "--> Starting (migrations run first)"
  docker compose --env-file "${ENV_FILE}" up -d --wait --wait-timeout 180

  echo "--> Pruning old images"
  docker image prune -f
REMOTE

echo "==> Verifying health"
# Deliberately checks readiness, not just liveness: /health would pass even
# if the database were unreachable.
ssh "${TARGET}" 'curl -fsS http://localhost:8000/health/ready' && echo
echo "==> Done"
