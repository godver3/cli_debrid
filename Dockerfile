# Use Python 3 as the base image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install build dependencies and supervisor
RUN apt-get update && apt-get install -y gcc supervisor ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker cache
COPY requirements-linux.txt .

# Install the requirements
RUN pip install --no-cache-dir -r requirements-linux.txt

# Copy the current directory contents into the container at /app
COPY . .

# Create necessary directories and files
RUN mkdir -p /user/db_content /user/config /user/logs && \
    touch /user/logs/debug.log /user/logs/info.log /user/logs/queue.log

# Set the TERM environment variable for proper terminal attachment
ENV TERM=xterm

# Comment out unwanted commands in shell initialization files
RUN sed -i 's/^export LC_ALL=C.UTF-8/# export LC_ALL=C.UTF-8/' /etc/profile && \
    sed -i 's/^clear/# clear/' /etc/profile

# Expose ports for both Flask apps
EXPOSE 5000 5001

# Copy supervisord configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create a startup script
RUN echo '#!/bin/bash\n\
supervisord -n -c /etc/supervisor/conf.d/supervisord.conf & \n\
sleep 2\n\
exec tail -f /user/logs/debug.log' > /app/start.sh && \
chmod +x /app/start.sh

# Start supervisord and tail the log file
CMD ["/app/start.sh"]
