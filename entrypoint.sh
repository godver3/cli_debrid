#!/bin/sh

# Loop to continuously run main.py
while true; do
    python3 /app/main.py
    echo "main.py exited. Restarting..."
    sleep 1
done
