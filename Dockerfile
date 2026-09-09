FROM node:22.23.2-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

ARG CODEX_VERSION=0.153.4
ARG PLAYWRIGHT_VERSION=1.62.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        apache2-utils \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        nginx \
        novnc \
        openbox \
        openssl \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        supervisor \
        websockify \
        x11vnc \
        x11-xserver-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade 'setuptools>=68' \
    && npm install --global --no-fund --no-audit "@openai/codex@${CODEX_VERSION}" \
    && test -x /usr/local/bin/codex

RUN groupadd --gid 1001 relay \
    && useradd --uid 1001 --gid 1001 --create-home --home-dir /home/relay --shell /usr/sbin/nologin relay \
    && mkdir -p /app /ms-playwright /opt/relay-docker /run/nginx /var/log/supervisor \
    && chown relay:relay /home/relay

WORKDIR /app
COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -c 'import tomllib; p=tomllib.load(open("pyproject.toml", "rb"))["project"]; print("\n".join(p.get("dependencies", []) + p["optional-dependencies"]["browser"] + p["optional-dependencies"]["robinhood"]))' > /tmp/relay-requirements.txt \
    && printf 'playwright==%s\n' "${PLAYWRIGHT_VERSION}" > /tmp/relay-constraints.txt \
    && /opt/venv/bin/pip install --timeout 120 --constraint /tmp/relay-constraints.txt -r /tmp/relay-requirements.txt \
    && /opt/venv/bin/python -m playwright install --with-deps chromium \
    && rm -f /tmp/relay-constraints.txt /tmp/relay-requirements.txt \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright \
    && install -d -o relay -g relay -m 0700 /data

COPY README.md config.example.json ./
COPY relay ./relay
COPY docker ./docker

RUN /opt/venv/bin/pip install --no-cache-dir --no-deps . \
    && cp -a /app/docker/. /opt/relay-docker/

COPY docker/nginx.conf /etc/nginx/nginx.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/relay.conf
COPY docker/chromium-browser.sh /usr/local/bin/relay-browser

RUN chmod 0755 /opt/relay-docker/*.sh /opt/relay-docker/*.py \
    && chmod 0755 /usr/local/bin/relay-browser \
    && chmod 0644 /etc/nginx/nginx.conf /etc/supervisor/conf.d/relay.conf \
    && chown -R root:root /app /opt/relay-docker

ENV HOME=/home/relay \
    CODEX_HOME=/data/codex \
    DISPLAY=:99 \
    BROWSER=/usr/local/bin/relay-browser

EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/relay-docker/entrypoint.sh"]
