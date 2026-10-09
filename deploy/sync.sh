#!/usr/bin/env bash
# Push the repo from this Mac to the Pi.
set -euo pipefail
PI="${TACO_PI:-taco@192.168.4.239}"
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git --exclude .venv --exclude node_modules --exclude __pycache__ \
  ./ "$PI:~/taco/"
echo "Synced to $PI:~/taco"
