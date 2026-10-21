#!/bin/sh
set -eu

if [ "${ECOSCAN_DEMO_SEEDED_SQLITE:-false}" = "true" ]; then
    seed_path="${ECOSCAN_DEMO_SEED_PATH:-/app/demo-data.sqlite3}"
    database_path="${SQLITE_DB_PATH:-/tmp/ecoscan-demo.sqlite3}"
    if [ ! -f "$seed_path" ]; then
        echo "Demo SQLite seed database is missing: $seed_path" >&2
        exit 1
    fi
    cp "$seed_path" "$database_path"
    export SQLITE_DB_PATH="$database_path"
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "${PROVISION_DEMO_ACCOUNTS:-false}" = "true" ]; then
    python manage.py provisionner_comptes_demo
fi

if [ "${PROVISION_DEMO_READER:-false}" = "true" ]; then
    python manage.py provisionner_comptes_demo --lecture-only
fi

exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --timeout 120
