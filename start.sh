#!/bin/bash
# ============================================
# Local API Test Environment
# Quick start script
# ============================================

set -e

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Local API Test Environment                              ║"
echo "╚══════════════════════════════════════════════════════════╝"

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Please log out and back in, then run this script again."
    exit 1
fi

# Create data directories
mkdir -p data n8n-data

# Build and run
echo ""
echo "Starting local API test server..."
echo ""

# Basic (just the MCP bridge)
docker compose up -d --build

# Or with n8n: docker compose --profile full up -d --build

echo ""
echo "✓ Local API test server running"
echo ""
echo "  MCP Endpoint:  http://127.0.0.1:8000"
echo "  Health:        http://127.0.0.1:8000/health"
echo "  Tools:         http://127.0.0.1:8000/mcp/tools"
echo ""
echo "  Logs:          docker compose logs -f"
echo "  Stop:          docker compose down"
echo ""

# Quick health check
sleep 3
curl -s http://127.0.0.1:8000/health | python3 -m json.tool 2>/dev/null || echo "Waiting for startup..."
