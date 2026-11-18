#!/usr/bin/env bash
# Entrypoint of the NarSat Airflow image: `entrypoint.sh <airflow sub-command> [args]`.
#
# Airflow 3's simple auth manager reads user passwords from a JSON file and, for a user
# missing from it, generates a random password and prints it to the log. Writing the
# file from AIRFLOW_ADMIN_PASSWORD (.env) gives a known password instead.
set -euo pipefail

if [[ -n "${AIRFLOW_ADMIN_PASSWORD:-}" ]]; then
  python - <<'PY'
import json
import os

path = os.environ["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE"]
user = os.environ.get("AIRFLOW_ADMIN_USER", "admin")
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump({user: os.environ["AIRFLOW_ADMIN_PASSWORD"]}, handle)
PY
fi

exec airflow "$@"
