#!/bin/bash
# Run this once to install dependencies and register the MCP server with Claude Code.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
SERVER="$SCRIPT_DIR/server.py"

# Check .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env file not found."
    echo "Copy .env.example to .env and fill in your Salesforce credentials first."
    exit 1
fi

echo "==> Creating virtual environment..."
python3 -m venv "$VENV"

echo "==> Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"

echo "==> Registering with Claude Code..."
claude mcp add salesforce-org "$VENV/bin/python3" "$SERVER" --env-file "$SCRIPT_DIR/.env"

echo ""
echo "Done! Restart Claude Code, then try:"
echo "  'Show me the top 5 accounts in my Salesforce org'"
echo "  'Find contacts with email john@acme.com'"
echo "  'List all Closed Won opportunities'"
echo "  'What org am I connected to?'"
