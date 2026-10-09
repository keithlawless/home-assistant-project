#!/usr/bin/env bash
# Apply Taco's Hermes settings (idempotent). Called by deploy/install-user.sh.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")"

# Persona (back up the original once).
[[ -f ~/.hermes/SOUL.md.orig ]] || cp ~/.hermes/SOUL.md ~/.hermes/SOUL.md.orig
cp SOUL.md ~/.hermes/SOUL.md

# Taco's tools, served over MCP from taco-core.
hermes config set mcp_servers.taco.command "$HOME/taco/.venv/bin/taco-mcp" >/dev/null
hermes config set mcp_servers.taco.timeout 330 >/dev/null  # Matter setup can take minutes

# The voice endpoint gets only what a kitchen assistant needs: web search,
# memory of household preferences, skills, and Taco's tools. No terminal,
# file or browser access, and a much smaller prompt.
hermes config set platform_toolsets.api_server "[web, memory, skills, taco]" >/dev/null
echo "Hermes configured for Taco."
