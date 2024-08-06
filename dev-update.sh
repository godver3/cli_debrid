#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Read the current version
VERSION=$(cat version.txt)
echo "Current version: $VERSION"

# Increment the version (assuming semantic versioning)
NEW_VERSION=$(echo $VERSION | awk -F. '{$NF = $NF + 1;} 1' | sed 's/ /./g')
echo "New version: $NEW_VERSION"

# Update the version file
echo $NEW_VERSION > version.txt
echo "Version file updated"

# Build the Docker image
echo "Building Docker image..."
docker build -t godver3/cli_debrid:dev .

# Push the Docker image to the registry
# Uncomment and modify the next line if you want to push to a specific registry
# echo "Pushing Docker image..."
docker push godver3/cli_debrid:dev

# Take down the current Docker Compose setup
echo "Taking down current Docker Compose setup..."
docker compose -f dev-docker-compose.yml down

# Bring up the new Docker Compose setup as a daemon
echo "Bringing up new Docker Compose setup..."
docker compose -f dev-docker-compose.yml up -d

echo "Update complete! New version: $NEW_VERSION"
