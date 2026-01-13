#!/bin/bash
# Auto-deploy script - checks GitHub and rebuilds if changes
# Add to crontab: */5 * * * * /home/user/ada-local-node/auto-deploy.sh

REPO_DIR="${REPO_DIR:-$HOME/ada-local-node}"
LOG_FILE="/tmp/ada-deploy.log"

cd "$REPO_DIR" || exit 1

# Fetch latest
git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$(date): Deploying $REMOTE" >> "$LOG_FILE"
    git pull origin main --quiet
    docker compose down
    docker compose up -d --build
    echo "$(date): Deploy complete" >> "$LOG_FILE"
fi
