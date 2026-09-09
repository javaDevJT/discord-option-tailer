#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
    exit 2
fi

if [ "$(id -u)" -eq 0 ]; then
    exec su relay -s /bin/sh -c 'HOME=/home/relay exec /usr/local/bin/relay-browser "$1"' relay-browser "$1"
fi

chromium="$(find /ms-playwright -type f -name chrome -perm -0100 -print -quit 2>/dev/null || true)"
if [ -z "$chromium" ]; then
    echo "Playwright Chromium is not installed" >&2
    exit 127
fi

mkdir -p /data/oauth-browser
chmod 700 /data/oauth-browser

"$chromium" \
    --user-data-dir=/data/oauth-browser \
    --no-sandbox \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    --new-window "$1" >/dev/null 2>&1 &
exit 0
