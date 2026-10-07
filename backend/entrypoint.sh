#!/usr/bin/env sh
# Container entrypoint.
#
# A managed platform gives you one process and no shell during a deploy, so
# everything the application needs in order to be useful has to happen here.
#
# Seeding is guarded: `cashmatch generate` refuses to run against a populated
# database without --reset, so a restart or a redeploy never silently
# destroys data. A fresh database gets a demo dataset; an existing one is
# left exactly as it was.
set -eu

echo "--> migrations"
alembic upgrade head

echo "--> seeding (skipped if the database already holds data)"
if python -m cashmatch.cli generate 2>/dev/null; then
    python -m cashmatch.cli extract
    python -m cashmatch.cli apply
    echo "--> demo dataset ready"
else
    echo "--> database already populated, left untouched"
fi

echo "--> serving on ${PORT:-8000}"
exec uvicorn cashmatch.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
