FROM ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib:0.163.2@sha256:7f22d2a6748110ffb6a59bc0df448bccbf501cdb667d610ede4c61d3863d5849 AS otel-collector

FROM node:26.11.1-trixie-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS codex-runtime
ARG CODEX_VERSION=0.153.4
RUN npm install --global --no-fund --no-audit "@openai/codex@${CODEX_VERSION}"

FROM node:26.11.1-trixie-slim@sha256:193fe51b64e77981119c98c2002c9e32a70e2f006fb4d25068ce0558998917f0 AS novnc-assets
ARG NOVNC_VERSION=1:1.6.0-2
WORKDIR /tmp
# Only the static client is served by nginx; Python websockify handles sockets.
RUN apt-get update \
    && apt-get download "novnc=${NOVNC_VERSION}" \
    && mkdir /novnc \
    && dpkg-deb --extract /tmp/novnc_*.deb /novnc \
    && dpkg-deb --field /tmp/novnc_*.deb Package Version Source \
        > /novnc/usr/share/novnc/package-provenance.txt

FROM ubuntu:26.04@sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7 AS runtime
COPY --from=otel-collector /otelcol-contrib /usr/local/bin/otelcol-contrib
COPY --from=codex-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-runtime /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
COPY --from=novnc-assets /novnc/usr/share/novnc /usr/share/novnc
COPY --from=novnc-assets /novnc/usr/share/doc/novnc /usr/share/doc/novnc

ARG CODEX_VERSION=0.153.4
ARG PLAYWRIGHT_VERSION=1.63.0
ARG CHROME_VERSION=154.0.8037.97-1
ARG CHROME_SHA256=a4edbe95e9b01db6c9b97d7a1323121eda18362b5620df06abac1b59bee80053

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RELAY_BROWSER_CHANNEL=chrome \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /app

# Keep filesystem steps few: the isolated builder copies snapshots slowly.
# Startup uses supervisord; discard the unused Pebble server from the base.
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/relay-project.toml,readonly \
    --mount=type=cache,target=/root/.cache/pip \
    apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        nginx \
        openbox \
        openssl \
        procps \
        python3 \
        python3-venv \
        supervisor \
        websockify \
        x11vnc \
        x11-xserver-utils \
        xvfb \
    && curl --fail --silent --show-error --location \
        "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb" \
        --output /tmp/google-chrome.deb \
    && printf '%s  %s\n' "${CHROME_SHA256}" /tmp/google-chrome.deb | sha256sum --check \
    && apt-get install -y --no-install-recommends /tmp/google-chrome.deb \
    && rm -f /tmp/google-chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade 'setuptools>=68' \
    && ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && test -x /usr/local/bin/codex \
    && codex --version \
    && groupadd --gid 1001 relay \
    && useradd --uid 1001 --gid 1001 --create-home --home-dir /home/relay --shell /usr/sbin/nologin relay \
    && mkdir -p /app /opt/relay-docker /run/nginx /var/log/supervisor \
    && chown relay:relay /home/relay \
    && python3 -c 'import tomllib; p=tomllib.load(open("/tmp/relay-project.toml", "rb"))["project"]; print("\n".join(p.get("dependencies", []) + p["optional-dependencies"]["browser"] + p["optional-dependencies"]["robinhood"] + p["optional-dependencies"]["discord"] + p["optional-dependencies"]["telemetry"]))' > /tmp/relay-requirements.txt \
    && printf 'playwright==%s\n' "${PLAYWRIGHT_VERSION}" > /tmp/relay-constraints.txt \
    && /opt/venv/bin/pip install --timeout 120 --constraint /tmp/relay-constraints.txt -r /tmp/relay-requirements.txt \
    && rm -f /tmp/relay-constraints.txt /tmp/relay-requirements.txt \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/bin/pebble \
    && install -d -o relay -g relay -m 0700 /data

RUN --mount=type=bind,source=.,target=/src,readonly \
    cp /src/pyproject.toml /src/README.md /src/config.example.json /app/ \
    && cp -a /src/relay /src/docker /app/ \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps . \
    && RELAY_OTEL_ENABLED=0 /opt/venv/bin/python -m relay.cli --help >/dev/null \
    && RELAY_OTEL_ENABLED=0 /opt/venv/bin/python -c 'from relay.episodes import resolve_episode; from relay.management_rules import deterministic_management' \
    && cp -a /app/docker/. /opt/relay-docker/ \
    && cp /app/docker/nginx.conf /etc/nginx/nginx.conf \
    && cp /app/docker/supervisord.conf /etc/supervisor/conf.d/relay.conf \
    && cp /app/docker/chromium-browser.sh /usr/local/bin/relay-browser \
    && chmod 0755 /opt/relay-docker/*.sh /opt/relay-docker/*.py \
    && chmod 0755 /usr/local/bin/relay-browser \
    && chmod 0644 /etc/nginx/nginx.conf /etc/supervisor/conf.d/relay.conf \
    && printf '%s\n' 'build-validation-not-a-key' > /tmp/collector-validation-key \
    && chmod 0600 /tmp/collector-validation-key \
    && OPENWOOF_INGEST_KEY_FILE=/tmp/collector-validation-key RELAY_SOURCE_REVISION=build-validation \
       /usr/local/bin/otelcol-contrib validate --config=/opt/relay-docker/otelcol.yaml \
    && rm -f /tmp/collector-validation-key \
    && chown -R root:root /app /opt/relay-docker

ARG QB_SOURCE_COMMIT=unknown
ENV QB_SOURCE_COMMIT=${QB_SOURCE_COMMIT} \
    RELAY_SOURCE_REVISION=${QB_SOURCE_COMMIT}

ENV HOME=/home/relay \
    CODEX_HOME=/data/codex \
    DISPLAY=:99 \
    BROWSER=/usr/local/bin/relay-browser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8080/healthz >/dev/null || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/relay-docker/entrypoint.sh"]
