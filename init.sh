#!/usr/bin/env bash
#
# init.sh — apply MongoDB migrations/scripts against the dev container.
#
# Runs every *.js file in ./migrations (sorted by filename) through mongosh
# inside the running Mongo container, so no local mongosh install is needed.
#
# Env overrides:
#   MONGO_NAME  container name          (default: mongo-dev)
#   MONGO_DB    target database         (default: app)
#   MIGRATIONS  migrations directory    (default: ./migrations)

set -euo pipefail

MONGO_NAME="${MONGO_NAME:-mongo-dev}"
MONGO_DB="${MONGO_DB:-app}"
MIGRATIONS="${MIGRATIONS:-./migrations}"

if ! docker ps --format '{{.Names}}' | grep -qx "$MONGO_NAME"; then
  echo "error: container '$MONGO_NAME' is not running. Start it with 'make mongo'." >&2
  exit 1
fi

echo "waiting for MongoDB to accept connections..."
for i in $(seq 1 30); do
  if docker exec "$MONGO_NAME" mongosh --quiet --eval 'db.runCommand({ ping: 1 })' >/dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "error: MongoDB did not become ready in time." >&2
    exit 1
  fi
  sleep 1
done

shopt -s nullglob
scripts=("$MIGRATIONS"/*.js)
if [ ${#scripts[@]} -eq 0 ]; then
  echo "no migration scripts found in $MIGRATIONS"
  exit 0
fi

for script in "${scripts[@]}"; do
  echo "applying $(basename "$script")..."
  docker exec -i "$MONGO_NAME" mongosh --quiet "$MONGO_DB" < "$script"
done

echo "done: applied ${#scripts[@]} migration(s) to database '$MONGO_DB'."
