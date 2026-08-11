#!/usr/bin/env bash

set -Eeuo pipefail

app_dir="${HANKEGO_APP_DIR:-/home/liliya/travel-ai-hankego}"
backup_dir="${HANKEGO_BACKUP_DIR:-/home/liliya/backups/hankego/automatic}"
state_dir="${STATE_DIRECTORY:-${HOME}/.local/state/hankego-monitor}"
max_disk_usage_percent="${HANKEGO_MAX_DISK_USAGE_PERCENT:-85}"
max_backup_age_seconds="${HANKEGO_MAX_BACKUP_AGE_SECONDS:-129600}"

if ! [[ "$max_disk_usage_percent" =~ ^[0-9]+$ ]] \
    || ! [[ "$max_backup_age_seconds" =~ ^[0-9]+$ ]]; then
    echo "ERROR: monitoring thresholds must be non-negative integers" >&2
    exit 2
fi

send_telegram_notification() {
    local message="$1"

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] \
        || [ -z "${MONITORING_TELEGRAM_CHAT_ID:-}" ]; then
        echo "ERROR: Telegram monitoring credentials are not configured" >&2
        return 1
    fi

    MONITORING_MESSAGE="$message" python3 - <<'PY'
import json
import os
import sys
import urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
payload = json.dumps(
    {
        "chat_id": os.environ["MONITORING_TELEGRAM_CHAT_ID"],
        "text": os.environ["MONITORING_MESSAGE"],
    }
).encode("utf-8")
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Telegram returned a non-200 status")
except Exception as error:
    # Не выводим URL запроса: внутри него находится токен бота.
    print(
        f"ERROR: Telegram notification failed: {type(error).__name__}",
        file=sys.stderr,
    )
    raise SystemExit(1) from error
PY
}

if [ "${1:-}" = "--test-notification" ]; then
    send_telegram_notification \
        "🟢 HankeGo: тестовое уведомление мониторинга успешно."
    exit 0
fi

cd "$app_dir"

compose=(
    docker compose
    -f compose.yaml
    -f compose.prod.yaml
    --profile bot
)
errors=()

add_error() {
    errors+=("$1")
}

for service in api bot postgres redis; do
    if ! container_id="$("${compose[@]}" ps --all --quiet "$service")"; then
        add_error "container ${service} could not be inspected"
        continue
    fi

    if [ -z "$container_id" ]; then
        add_error "container ${service} was not found"
        continue
    fi

    if ! container_status="$(
        docker inspect --format '{{.State.Status}}' "$container_id"
    )"; then
        add_error "container ${service} state could not be inspected"
        continue
    fi

    if [ "$container_status" != "running" ]; then
        add_error "container ${service} is ${container_status}"
    fi
done

for service in api postgres redis; do
    if ! container_id="$("${compose[@]}" ps --all --quiet "$service")"; then
        continue
    fi

    if [ -z "$container_id" ]; then
        continue
    fi

    if ! health_status="$(
        docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$container_id"
    )"; then
        add_error "container ${service} health could not be inspected"
        continue
    fi

    if [ "$health_status" != "healthy" ]; then
        add_error "container ${service} health is ${health_status}"
    fi
done

if ! curl --fail --silent --show-error \
    --connect-timeout 3 \
    --max-time 10 \
    http://127.0.0.1:8000/health/ready \
    > /dev/null; then
    add_error "API readiness check failed"
fi

disk_usage_percent="$(df -P / | awk 'NR == 2 {gsub("%", "", $5); print $5}')"

if [ "$disk_usage_percent" -ge "$max_disk_usage_percent" ]; then
    add_error "root disk usage is ${disk_usage_percent}%"
fi

if ! systemctl is-active --quiet hankego-postgres-backup.timer; then
    add_error "PostgreSQL backup timer is not active"
fi

if ! backup_result="$(
    systemctl show hankego-postgres-backup.service \
        --property=Result \
        --value
)"; then
    backup_result="unknown"
fi

if [ "$backup_result" != "success" ]; then
    add_error "last PostgreSQL backup result is ${backup_result:-unknown}"
fi

latest_backup=""

if [ -d "$backup_dir" ]; then
    latest_backup="$(
        find "$backup_dir" -maxdepth 1 -type f -name 'hankego-*.dump' \
            -printf '%f\n' \
            | sort \
            | tail -n 1
    )"
fi

if [ -z "$latest_backup" ]; then
    add_error "verified PostgreSQL backup was not found"
else
    backup_file="${backup_dir}/${latest_backup}"
    backup_age_seconds="$(( $(date +%s) - $(stat -c %Y "$backup_file") ))"

    if [ ! -s "$backup_file" ]; then
        add_error "latest PostgreSQL backup is empty"
    elif [ "$backup_age_seconds" -gt "$max_backup_age_seconds" ]; then
        add_error "latest PostgreSQL backup is older than 36 hours"
    fi

    if [ ! -s "${backup_file}.sha256" ]; then
        add_error "latest PostgreSQL backup checksum is missing"
    fi
fi

install -d -m 700 -- "$state_dir"
status_file="${state_dir}/status"
previous_status="$(cat "$status_file" 2> /dev/null || true)"
hostname_value="$(hostname)"
timestamp="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

if [ "${#errors[@]}" -eq 0 ]; then
    if [ "$previous_status" = "failed" ]; then
        printf -v recovery_message \
            '🟢 HankeGo recovered\nHost: %s\nTime: %s' \
            "$hostname_value" \
            "$timestamp"
        send_telegram_notification "$recovery_message"
    fi

    printf 'healthy\n' > "$status_file"
    echo "HankeGo production monitoring: healthy"
    exit 0
fi

printf -v error_details -- '• %s\n' "${errors[@]}"

if [ "$previous_status" != "failed" ]; then
    printf -v alert_message \
        '🔴 HankeGo problem\nHost: %s\nTime: %s\n%s' \
        "$hostname_value" \
        "$timestamp" \
        "$error_details"
    send_telegram_notification "$alert_message"
fi

printf 'failed\n' > "$status_file"
printf 'HankeGo production monitoring failed:\n%s' "$error_details" >&2
exit 1