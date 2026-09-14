#!/usr/bin/env sh
# Backend container entrypoint.
#
# Applies outstanding database migrations before serving any API traffic
# (Req 19.3), then launches Uvicorn on the version pinned in requirements.txt
# (Req 19.5). Migrations run first so the schema is always current before the
# server accepts requests.
#
# Task 1.3 finalizes the seed step; this script is intentionally structured so
# that step can be inserted between the migration and the server launch.
set -eu

# 1. Apply database migrations. `alembic upgrade head` is idempotent: on an
#    already-current database it is a no-op, so restarts are safe.
echo "[entrypoint] Applying database migrations (alembic upgrade head)..."
alembic upgrade head

# 2. Seed the default Super_Admin and sample Workspace. The seed is idempotent
#    (existence-checked), so it is safe to run on every container start.
echo "[entrypoint] Seeding default super admin and sample workspace..."
python -m app.db.seed

# 3. Launch the API server. `exec` replaces this shell so Uvicorn receives
#    container signals (SIGTERM) directly for graceful shutdown.
echo "[entrypoint] Starting Uvicorn..."
exec uvicorn app.main:app \
  --host "${UVICORN_HOST:-0.0.0.0}" \
  --port "${UVICORN_PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips "*"
