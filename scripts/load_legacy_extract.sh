#!/usr/bin/env bash
# Load the extracted legacy SQL files into a transient Postgres on port 5433.
#
# Pairs with extract_legacy_dump.py and migrate_legacy management command.
# The transient DB is read-only fodder for the H17 migrate_legacy command;
# it never holds v2 data.
#
# Usage:
#   ./scripts/load_legacy_extract.sh                 # default --extract-dir .legacy_extract
#   ./scripts/load_legacy_extract.sh /path/to/extract
#
# After it finishes, the DB is reachable at:
#   postgres://postgres:legacypw@localhost:5433/sprycer_legacy
set -euo pipefail

EXTRACT_DIR="${1:-.legacy_extract}"
CONTAINER="sprycer-pg-legacy"
PORT="5433"
DB="sprycer_legacy"
PASSWORD="legacypw"

if [[ ! -d "$EXTRACT_DIR" ]]; then
  echo "ERROR: extract dir not found: $EXTRACT_DIR" >&2
  echo "Run scripts/extract_legacy_dump.py first." >&2
  exit 2
fi
if [[ ! -f "$EXTRACT_DIR/schema.sql" ]]; then
  echo "ERROR: $EXTRACT_DIR/schema.sql missing — extract incomplete?" >&2
  exit 2
fi

# 1. Spin up (or replace) the transient container.
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}\$"; then
  echo "Removing previous ${CONTAINER}..."
  docker rm -f "$CONTAINER" >/dev/null
fi
echo "Starting Postgres 16 on port ${PORT} as ${CONTAINER}..."
docker run --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PASSWORD" \
  -e POSTGRES_DB="$DB" \
  -p "${PORT}:5432" \
  -d postgres:16 >/dev/null

# 2. Wait for it to accept connections.
for i in {1..30}; do
  if docker exec "$CONTAINER" pg_isready -U postgres -d "$DB" >/dev/null 2>&1; then
    echo "Postgres ready."
    break
  fi
  sleep 1
done

PSQL="docker exec -i $CONTAINER psql -U postgres -d $DB -v ON_ERROR_STOP=1"

# 3. Load schema (CREATE TABLE statements from the extractor).
echo "Loading schema..."
$PSQL < "$EXTRACT_DIR/schema.sql"

# 4. Load each table's COPY block. Order matches FK dependencies for
#    defensiveness, even though the extracted schema has no FK constraints.
TABLES=(
  retailers brands websites users channels main_competitions
  pages offers offers_pages matchings price_points reviews versions
)
for t in "${TABLES[@]}"; do
  f="$EXTRACT_DIR/${t}.sql"
  if [[ ! -f "$f" ]]; then
    echo "  ${t}: (no extract; skipping)"
    continue
  fi
  echo -n "  ${t}: loading... "
  $PSQL < "$f"
  count=$(docker exec "$CONTAINER" psql -U postgres -d "$DB" -tAc "SELECT count(*) FROM ${t};")
  echo "${count} rows"
done

echo
echo "Legacy DB ready:"
echo "  postgres://postgres:${PASSWORD}@localhost:${PORT}/${DB}"
echo
echo "Next: dry-run the v2 migration first (rolls back at the end)"
echo "  uv run python manage.py migrate_legacy --dry-run --legacy-url postgres://postgres:${PASSWORD}@localhost:${PORT}/${DB}"
echo
echo "If the dry-run counters look right, drop --dry-run to commit:"
echo "  uv run python manage.py migrate_legacy --legacy-url postgres://postgres:${PASSWORD}@localhost:${PORT}/${DB}"
