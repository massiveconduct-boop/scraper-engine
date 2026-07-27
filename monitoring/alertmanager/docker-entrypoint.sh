#!/bin/sh
# Alertmanager entrypoint wrapper — substitutes SLACK_WEBHOOK_URL env var
# into config at container start. Plan §3.3: webhook URL must come from
# .env / docker-compose environment, never committed.
# Passes "$@" through to alertmanager for flags like --log.level=debug.
set -e

TEMPLATE="/etc/alertmanager/alertmanager.yml"
RESOLVED="/tmp/alertmanager.yml"

if [ -n "${SLACK_WEBHOOK_URL}" ]; then
    sed "s|\${SLACK_WEBHOOK_URL}|${SLACK_WEBHOOK_URL}|g" \
        "${TEMPLATE}" > "${RESOLVED}"
    exec /bin/alertmanager --config.file="${RESOLVED}" "$@"
else
    echo "WARNING: SLACK_WEBHOOK_URL not set — alertmanager will fail to send notifications"
    exec /bin/alertmanager --config.file="${TEMPLATE}" "$@"
fi
