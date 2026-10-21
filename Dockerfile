# syntax=docker/dockerfile:1.7
#
# Multi-stage build. Layers are ordered from least to most frequently changed
# so a code-only change rebuilds just the final `COPY . .` layer:
#   apt runtime deps -> python venv -> patchright chromium -> node_modules -> app
# Compilers live only in the builder stages and never reach the runtime image.

# ---------------------------------------------------------------------------
# Python dependencies, built into a relocatable venv at /opt/venv.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS pybuild

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.5.11 && python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv UV_LINK_MODE=copy

COPY requirements-linux.txt /tmp/requirements-linux.txt
# setuptools<78: pkg_resources was removed in v78+ and supervisor needs it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        "setuptools<78" wheel supervisor -r /tmp/requirements-linux.txt

# ---------------------------------------------------------------------------
# phalanx_db_hyperswarm node_modules. Built on the same base + Debian nodejs as
# the runtime image so any native addon ABI matches.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS nodebuild

RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm build-essential gyp && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY phalanx_db_hyperswarm/package.json phalanx_db_hyperswarm/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm config set fetch-retries 5 && \
    npm config set fetch-retry-factor 2 && \
    npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    for i in 1 2 3; do \
        echo "Attempt $i: Installing npm dependencies..." && \
        npm install --force --prefer-offline --no-audit --no-fund && break || \
        (echo "Attempt $i failed, waiting 10 seconds..." && sleep 10); \
    done && \
    if [ ! -d "node_modules" ]; then \
        echo "ERROR: npm install failed after all attempts" && exit 1; \
    fi

# ---------------------------------------------------------------------------
# Runtime image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

WORKDIR /app

# Runtime packages only (no compilers). --no-install-recommends keeps the image
# lean, so anything previously pulled in only as a "recommends" and still used
# at runtime is listed explicitly: psmisc (fuser) and procps (ps), both used by
# routes/program_operation_routes.py.
#
# xvfb + Chromium runtime deps are for the Cloudflare-challenge bypass browser
# (see utilities/cloudflare_bypass.py) — headless mode alone fails the
# challenge, so a real headed browser under a virtual display is required.
# Installed explicitly (not via `patchright install --with-deps`) because that
# command's OS detection doesn't recognize Debian trixie and falls back to
# Ubuntu 20.04 package names, two of which don't exist on trixie.
#
# fonts-dejavu-core: last-resort system-font fallback for poster overlay text
# (see overlays/font_manager.py's _LOCAL_FONT_MAP).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gosu nodejs npm ffmpeg psmisc procps \
        xvfb xauth fonts-liberation fonts-unifont libnss3 libnspr4 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
        libcairo2 libatspi2.0-0 \
        fonts-dejavu-core && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV PUID=0 \
    PGID=0 \
    TERM=xterm \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY --from=pybuild /opt/venv /opt/venv

# Chromium for patchright, installed to a fixed, UID-independent path instead
# of the default $HOME-based cache: the entrypoint runs the app as `appuser`
# (HOME=/app) whenever a custom PUID/PGID is set, which would otherwise look for
# the browser under /app/.cache/ms-playwright and fail with "Executable doesn't
# exist".
RUN patchright install chromium && \
    chmod -R a+rX /ms-playwright

RUN mkdir -p /user/db_content /user/config /user/logs /etc/supervisor/conf.d && \
    touch /user/logs/debug.log && \
    chmod -R 755 /user && \
    sed -i 's/^export LC_ALL=C.UTF-8/# export LC_ALL=C.UTF-8/' /etc/profile && \
    sed -i 's/^clear/# clear/' /etc/profile

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY --from=nodebuild /build/node_modules /app/phalanx_db_hyperswarm/node_modules

# App code last: this is the only layer a code-only change invalidates.
COPY . .
RUN chmod +x /app/entrypoint.sh

# Flask app and phalanx_db_hyperswarm
EXPOSE 5000 8888

CMD ["/app/entrypoint.sh"]
