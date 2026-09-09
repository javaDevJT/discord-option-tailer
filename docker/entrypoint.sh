#!/bin/sh
set -eu

export BROWSER="${BROWSER:-/usr/local/bin/relay-browser}"

/opt/venv/bin/python /opt/relay-docker/bootstrap.py
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/relay.conf
