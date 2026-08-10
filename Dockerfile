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
    libcairo2 libatspi2.0-0 && \
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

# Create an entrypoint script
RUN echo '#!/bin/bash\n\
\n\
# Function to set permissions\n\
set_permissions() {\n\
    echo "Setting permissions for /user directory..."\n\
    chmod -R 755 /user\n\
    find /user -type f -exec chmod 644 {} +\n\
    chown -R $PUID:$PGID /user\n\
    echo "Permissions set successfully"\n\
}\n\
\n\
# Create user with specified PUID/PGID or use root\n\
if [ $PUID != 0 ] || [ $PGID != 0 ]; then\n\
    echo "Starting with custom user - PUID: $PUID, PGID: $PGID"\n\
    groupadd -g $PGID appuser\n\
    useradd -u $PUID -g $PGID -d /app appuser\n\
    set_permissions\n\
    echo "Created user appuser with UID: $PUID and GID: $PGID"\n\
    # Update supervisord config to use the new user\n\
    sed -i "s/user=root/user=appuser/" /etc/supervisor/conf.d/supervisord.conf\n\
    echo "Updated supervisord configuration to use appuser"\n\
else\n\
    echo "Starting with root user (PUID=0, PGID=0)"\n\
    set_permissions\n\
fi\n\
\n\
# Start supervisord and tail logs\n\
if [ $PUID != 0 ] || [ $PGID != 0 ]; then\n\
    echo "Starting supervisord as appuser"\n\
    gosu appuser supervisord -n -c /etc/supervisor/conf.d/supervisord.conf & \n\
else\n\
    echo "Starting supervisord as root"\n\
    supervisord -n -c /etc/supervisor/conf.d/supervisord.conf & \n\
fi\n\
\n\
sleep 2\n\
exec tail -F /user/logs/debug.log' > /app/entrypoint.sh && \
chmod +x /app/entrypoint.sh

# Use the entrypoint script
CMD ["/app/entrypoint.sh"]
