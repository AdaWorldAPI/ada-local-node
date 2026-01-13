# Local API Test Environment

Development container for testing REST APIs and webhooks locally.

## Quick Start

```bash
chmod +x start.sh
./start.sh
```

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `http://localhost:8000/health` | Health check |
| `http://localhost:8000/mcp/tools` | List available test tools |
| `http://localhost:8000/mcp/invoke` | Invoke test tool |
| `http://localhost:8000/status` | Container status |

## Configuration

Environment variables in `docker-compose.yml`:

- `NODE_ID` - Identifier for this test instance
- `POLL_INTERVAL` - Sync interval in seconds

## Usage with VS Code

1. Install REST Client extension
2. Create `.http` files to test endpoints
3. Use the built-in terminal to view logs

## Logs

```bash
docker compose logs -f
```

## Stop

```bash
docker compose down
```
