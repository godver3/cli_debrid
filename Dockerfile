# Use Python 3 as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install build dependencies and Node.js
RUN apt-get update && \
    apt-get install -y gcc gosu nodejs npm ffmpeg \
    build-essential gyp \
    # xvfb + Chromium runtime deps for the Cloudflare-challenge bypass browser
    # (see utilities/cloudflare_bypass.py) — headless mode alone fails the
    # challenge, so a real headed browser under a virtual display is required.
    # Installed explicitly (not via `patchright install --with-deps`) because
    # that command's OS detection doesn't recognize Debian trixie and falls
    # back to Ubuntu 20.04 package names, two of which (ttf-ubuntu-font-family,
    # ttf-unifont) don't exist on trixie and fail the build.
    xvfb fonts-liberation fonts-unifont libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 \
    # DejaVu Sans — last-resort system-font fallback for poster overlay text
    # (see overlays/font_manager.py's _LOCAL_FONT_MAP). Without this package
    # the fallback path was silently unreachable: the font_manager fell
    # through to trying a Google Fonts download for "DejaVuSans-Bold", which
    # isn't a real Google Fonts family name and always failed.
    fonts-dejavu-core && \
    # Cleanup
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set default environment variables for PUID/PGID
ENV PUID=0
ENV PGID=0

# Copy only the requirements file first to leverage Docker cache
COPY requirements-linux.txt .

# Upgrade pip and install necessary build tools (including supervisor)
# Pin setuptools<78 because pkg_resources was removed in v78+ and supervisor needs it
RUN pip install --upgrade pip "setuptools<78" wheel supervisor

# Install the requirements
RUN pip install --no-cache-dir -r requirements-linux.txt

# Install Chromium for patchright (Cloudflare-challenge bypass browser, see
# utilities/cloudflare_bypass.py). OS-level deps are installed explicitly above
# (not via --with-deps, which fails on this base image — see comment above).
# Installed to a fixed, UID-independent path instead of the default $HOME-based
# cache: this RUN executes as root at build time ($HOME=/root), but the
# entrypoint below runs the app as a separate `appuser` (HOME=/app) whenever a
# custom PUID/PGID is set, which would otherwise look for the browser at
# /app/.cache/ms-playwright — a path that was never populated — and fail with
# "Executable doesn't exist".
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN patchright install chromium && \
    chmod -R a+rX /ms-playwright

# Copy the current directory contents into the container at /app
COPY . .

# Install phalanx_db_hyperswarm dependencies with retry logic
RUN cd /app/phalanx_db_hyperswarm && \
    # Configure npm for better reliability
    npm config set fetch-retries 5 && \
    npm config set fetch-retry-factor 2 && \
    npm config set fetch-retry-mintimeout 20000 && \
    npm config set fetch-retry-maxtimeout 120000 && \
    # Attempt installation with retries, forcing engine compatibility
    for i in 1 2 3; do \
        echo "Attempt $i: Installing npm dependencies..." && \
        npm install --force --prefer-offline --no-audit --no-fund --loglevel=info && break || \
        (echo "Attempt $i failed, waiting 10 seconds..." && sleep 10); \
    done && \
    # Verify installation succeeded
    if [ ! -d "node_modules" ]; then \
        echo "ERROR: npm install failed after all attempts" && exit 1; \
    fi && \
    echo "Successfully installed npm dependencies"

# Create necessary directories and files with proper permissions
RUN mkdir -p /user/db_content /user/config /user/logs && \
    touch /user/logs/debug.log && \
    chmod -R 755 /user

# Set the TERM environment variable for proper terminal attachment
ENV TERM=xterm

# Comment out unwanted commands in shell initialization files
RUN sed -i 's/^export LC_ALL=C.UTF-8/# export LC_ALL=C.UTF-8/' /etc/profile && \
    sed -i 's/^clear/# clear/' /etc/profile

# Expose ports for Flask app and phalanx_db_hyperswarm
EXPOSE 5000 8888

# Copy supervisord configuration
RUN mkdir -p /etc/supervisor/conf.d
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Keep the runtime entrypoint in a normal source file so its root-only setup
# (including X11 socket-directory ownership) cannot drift from the image.
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Use the entrypoint script
CMD ["/app/entrypoint.sh"]
