#!/usr/bin/env sh
set -eu

uv run python manage.py migrate --no-input
uv run python manage.py seed_deployment_users
exec uv run python scripts/run_seenode.py
