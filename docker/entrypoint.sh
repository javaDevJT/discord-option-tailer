#!/bin/sh
set -eu

export BROWSER="${BROWSER:-/usr/local/bin/relay-browser}"

env -u RELAY_T3_WEBHOOK_URL -u RELAY_T3_WEBHOOK_SIGNING_SECRET \
    /opt/venv/bin/python /opt/relay-docker/bootstrap.py
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/relay.conf
