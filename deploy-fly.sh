#!/usr/bin/env bash
# Deploys todos MCP Server to Fly.io (~$2/mo, shared-cpu-1x 256mb)
# Requirements: flyctl installed (https://fly.io/docs/flyctl/install/)
#               Fly.io account (flyctl auth login)
set -euo pipefail

APP_NAME="${FLY_APP_NAME:-todos-mcp}"

# First-time setup: create the app
if ! flyctl status --app "$APP_NAME" &>/dev/null; then
    echo "Creating Fly.io app: $APP_NAME"
    flyctl launch --no-deploy --name "$APP_NAME" --copy-config --yes

    echo "Setting required secrets..."
    flyctl secrets set \
        "JWT_SECRET=$(openssl rand -hex 32)" \
        "BASE_URL=https://${APP_NAME}.fly.dev" \
        --app "$APP_NAME"

    echo ""
    echo "App URL will be: https://${APP_NAME}.fly.dev"
    echo "Use this URL as the MCP server in Claude Desktop/claude.ai:"
    echo "  https://${APP_NAME}.fly.dev/mcp"
fi

echo "Deploying..."
flyctl deploy --app "$APP_NAME"
echo "Done. MCP endpoint: https://${APP_NAME}.fly.dev/mcp"
