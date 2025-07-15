# Use Python 3 as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install build dependencies, supervisor, and Node.js
RUN apt-get update && \
    apt-get install -y gcc supervisor gosu nodejs npm ffmpeg \
    python3-pip python3-setuptools build-essential gyp && \
    # Cleanup
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set default environment variables for PUID/PGID
ENV PUID=0
ENV PGID=0

# Copy only the requirements file first to leverage Docker cache
COPY requirements-linux.txt .

# Upgrade pip and install necessary build tools
RUN pip install --upgrade pip setuptools wheel

# Install the requirements
RUN pip install --no-cache-dir -r requirements-linux.txt

# Copy the current directory contents into the container at /app
COPY . .

# Install phalanx_db_hyperswarm dependencies
RUN cd /app/phalanx_db_hyperswarm && npm install

# Create necessary directories and files with proper permissions
RUN mkdir -p /user/db_content /user/config /user/logs && \
    touch /user/logs/debug.log && \
    chmod -R 755 /user

# Set the TERM environment variable for proper terminal attachment
ENV TERM=xterm

# Comment out unwanted commands in shell initialization files
RUN sed -i 's/^export LC_ALL=C.UTF-8/# export LC_ALL=C.UTF-8/' /etc/profile && \
    sed -i 's/^clear/# clear/' /etc/profile

# Expose ports for both Flask apps and phalanx_db_hyperswarm
EXPOSE 5000 5001 8888

# Copy supervisord configuration
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
