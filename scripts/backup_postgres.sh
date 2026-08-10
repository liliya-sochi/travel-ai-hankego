#!/usr/bin/env bash

set -Eeuo pipefail

app_dir="${HANKEGO_APP_DIR:-/home/liliya/travel-ai-hankego}"
backup_dir="${HANKEGO_BACKUP_DIR:-/home/liliya/backups/hankego/automatic}"
retention_days="${HANKEGO_BACKUP_RETENTION_DAYS:-14}"

if ! [[ "$retention_days" =~ ^[0-9]+$ ]]; then
    echo "ERROR: backup retention must be a non-negative integer" >&2
    exit 1
fi

cd "$app_dir"

compose=(
    docker compose
    -f compose.yaml
    -f compose.prod.yaml
    --profile bot
)

postgres() {
    "${compose[@]}" exec -T postgres "$@"
}

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_name="hankego-${timestamp}.dump"
backup_file="${backup_dir}/${backup_name}"
partial_file="${backup_file}.partial"
checksum_file="${backup_file}.sha256"
checksum_partial="${checksum_file}.partial"
restore_database="hankego_restore_$(date -u +%Y%m%d_%H%M%S)_$$"
restore_created=0

cleanup() {
    exit_code=$?

    trap - EXIT INT TERM
    set +e

    if [ "$restore_created" -eq 1 ]; then
        postgres dropdb -U hankego --if-exists "$restore_database" \
            > /dev/null 2>&1
    fi

    rm -f -- "$partial_file" "$checksum_partial"
    exit "$exit_code"
}

trap cleanup EXIT INT TERM

mkdir -p -- "$backup_dir"
chmod 700 -- "$backup_dir"
umask 077

# Не допускаем одновременный запуск двух резервных копирований.
exec 9>"${backup_dir}/.backup.lock"

if ! flock -n 9; then
    echo "ERROR: another backup process is already running" >&2
    exit 1
fi

postgres_id="$("${compose[@]}" ps --quiet postgres)"

if [ -z "$postgres_id" ]; then
    echo "ERROR: PostgreSQL Compose container was not found" >&2
    exit 1
fi

if [ "$(docker inspect --format '{{.State.Running}}' "$postgres_id")" != "true" ]; then
    echo "ERROR: PostgreSQL container is not running" >&2
    exit 1
fi

if [ "$(docker inspect --format '{{.State.Health.Status}}' "$postgres_id")" != "healthy" ]; then
    echo "ERROR: PostgreSQL container is not healthy" >&2
    exit 1
fi

echo "Creating PostgreSQL backup: ${backup_name}"

postgres pg_dump -U hankego -d hankego \
    --format=custom \
    --no-owner \
    --no-privileges \
    > "$partial_file"

test -s "$partial_file"

postgres pg_restore --list \
    < "$partial_file" \
    > /dev/null

echo "Restoring backup into verification database: ${restore_database}"

postgres createdb -U hankego "$restore_database"
restore_created=1

postgres pg_restore -U hankego \
    --dbname="$restore_database" \
    --exit-on-error \
    --single-transaction \
    --no-owner \
    --no-privileges \
    < "$partial_file"

postgres psql -U hankego -d "$restore_database" -Atc "
SELECT 'alembic=' || version_num FROM alembic_version;
SELECT 'users=' || count(*) FROM users;
SELECT 'trips=' || count(*) FROM trips;
"

postgres dropdb -U hankego "$restore_database"
restore_created=0

mv -- "$partial_file" "$backup_file"

(
    cd "$backup_dir"
    sha256sum "$backup_name" > "$(basename "$checksum_partial")"
    sha256sum --check "$(basename "$checksum_partial")"
)

mv -- "$checksum_partial" "$checksum_file"

# Ротация затрагивает только автоматические backup в отдельном каталоге.
find "$backup_dir" -maxdepth 1 -type f \
    \( -name 'hankego-*.dump' -o -name 'hankego-*.dump.sha256' \) \
    -mtime "+$retention_days" \
    -delete

echo "PostgreSQL backup verified: ${backup_file}"