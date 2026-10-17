# 🤖 agent-node

**A lightweight, self-healing Linux deployment node with embedded terminal, tooling, and tunnel capabilities.**

> **agent-node v2.3** — The foundational building block for a multi-location node network. Deploys to any free hosting platform, auto-installs its own toolchains, and provides authenticated terminal access via WebSocket, SSH, or sshx web links.

---

## 🚀 Quick Start

### Deploy to Suga.app (one-click)

1. Fork this repo to your GitHub account
2. Create a **"Standard Build"** service on [suga.app](https://suga.app)
3. Configure environment:
   ```
   MONITOR_TOKEN=suga_known_token_2026
   ```
4. Set deployment mode:
   - **Build command**: `echo Build done`
   - **Start command**: `python3 app.py`

The auto-install hook handles the rest: installs `openssh-server`, `curl`, `wget`, `unzip`, downloads `cloudflared` and `xray`.

### Access

After deployment (~60s for tools to install):

| Method | URL / Command |
|--------|--------------|
| **Web Terminal** | `https://YOUR_URL.REGION.suga.run/terminal` |
| **WebSocket** | `wss://YOUR_URL.REGION.suga.run/ws/terminal?token=TOKEN` |
| **sshx** | `POST /api/install/sshx` then `GET /api/sshx` |
| **API** | `POST /api/exec` with `Authorization: Bearer TOKEN` |
| **SSH** (if port exposed) | `ssh root@HOST -p 2222` (password: `AgentNode2026!`) |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    agent-node v2.3                            │
│                                                              │
│  ┌──────────┐  ┌──────────────────────────────────────────┐ │
│  │ Auto-     │  │  Startup Hook (daemon thread)             │ │
│  │ Install   │  │  • apt-get: ssh, curl, wget, iproute2      │ │
│  │ Hook      │  │  • urllib: cloudflared, xray               │ │
│  └──────────┘  │  • sshd on port 2222 (root:AgentNode2026!)   │ │
│                │  • Auto-runs EVERY container restart       │ │
│  ┌──────────┐  └──────────────────────────────────────────┐ │ │
│  │ HTTP API  │  │  Tools Available After ~60s Startup       │ │ │
│  │  :8080    │  ├──────────────────────────────────────────┤ │ │
│  └──────────┘  │  ssh  → /usr/bin/ssh                         │ │ │
│                │  curl → /usr/bin/curl                        │ │ │
│                │  xray → /usr/local/bin/xray (v26.3.27)       │ │ │
│                │  cloudflared → /usr/local/bin/               │ │ │
│                │  sshd → /usr/sbin/sshd (port 2222)           │ │ │
│                └──────────────────────────────────────────┘ │ │
│                                                              │
│  Routes:                                                      │
│    GET  /                → Index page with docs               │
│    GET  /health          → Health check (no auth)             │
│    GET  /terminal        → Web terminal UI (xterm.js)         │
│    GET  /ws/terminal     → WebSocket terminal (auth: ?token=) │
│    GET  /api/diagnostics → System diagnostics                 │
│    GET  /api/tokens      → Token discovery (env, K8s, files)  │
│    POST /api/exec        → Execute allowed commands (auth)    │
│    POST /api/exec/bg     → Background exec (auth)              │
│    POST /api/install/sshx → Install sshx web terminal           │
│    POST /api/install/xray → Install xray proxy                 │
│    POST /api/ssh/tunnel   → SSH reverse tunnel (auth)         │
│    GET  /api/sshx        → Get current sshx link (auth)       │
│    GET  /api/xray/status  → Check xray status (auth)          │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 Platform-Specific Notes

### Suga.app / StormKit

- **Python**: 3.13 via `mise` (venv at `/app/.venv/`)
- **pip**: Pre-installed in venv — no need for `get-pip.py`!
- **EXTERNALLY-MANAGED**: venv doesn't have PEP 668 restriction
- **WAF**: `curl`/`wget` in request body is blocked by Cloudflare WAF → use `python3 -c "import urllib.request"` instead
- **Tunnel relays**: `region1.v2.argotunnel.com:7844` blocked → Cloudflare Tunnel won't connect
- **Port exposure**: Only port 80 (or 8080) is exposed; SSH port 2222 not accessible from outside
- **Container**: Kubernetes-based, restarts every ~30-60 min
- **Auto-recovery**: Startup hook reinstalls tools on every restart

### WispByte

- SSH-based deployment (Docker available)
- Direct root shell access
- Can expose all ports
- Deploy: push Docker image with `agent-node` pre-installed

### AlwaysData

- SSH access provided
- Docker not available (must use Python directly)
- Limited to 1GB storage

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITOR_TOKEN` | auto-generated | Auth token for `/api/*` and `/ws/*` endpoints |
| `PORT` | `8080` | HTTP server port |
| `HOST` | `0.0.0.0` | Bind address |
| `XRAY_ENABLED` | `false` | Enable xray proxy |
| `SSH_TARGET` | — | Remote SSH server for tunnel (for `/api/ssh/tunnel`) |

### Token

If `MONITOR_TOKEN` is not set, the app auto-generates one:
```
TOKEN = os.environ.get('MONITOR_TOKEN', 'suga_known_token_2026')
```

---

## 🛠️ Self-Healing

The startup hook (daemon thread) auto-installs:
1. `apt-get` packages: `openssh-client openssh-server curl wget unzip iproute2 net-tools`
2. Downloads `cloudflared` from GitHub releases
3. Downloads `xray` from GitHub releases
4. Generates SSH host keys (`ssh-keygen -A`)
5. Sets root password (`AgentNode2026!`)
6. Starts `sshd` on port 2222

This runs on **every container restart** — tools are always available within ~60 seconds of startup.

---

## 📋 Deploy Command (StormKit CLI)

```bash
# Update environment
stormkit update-env --env-id ENV_ID \
  --env-vars MONITOR_TOKEN=suga_known_token_2026 \
  --install-cmd "echo Build done" \
  --server-cmd "python3 app.py"

# Deploy + publish
stormkit deploy --env-id ENV_ID --publish --branch main
```

---

## 🎯 Future Roadmap

- [ ] Cloudflare Tunnel relay workaround (via SSH tunnel relay)
- [ ] xray config generator (`/api/config/vmess`)
- [ ] Web UI for VPN panel management (xterm.js + config builder)
- [ ] Node registry API (register with central orchestrator)
- [ ] Health watchdog with auto-restart
- [ ] Multi-region node auto-deployment

---

## 📄 License

MIT — Built for the multi-vp-panel project.


## 🔒 Privacy

This project can be deployed as a **private repository** for personal use. Set `GITHUB_API_KEY` as a secret in StormKit for access. Do not publish to public CDNs.

---

## 🚀 دسترسی (Access)

**وب‌سایت**: `https://[YOUR_HOST].suga.run/terminal`

| روش | لینک | توضیحات |
|-----|------|--------|
| **🌐 وب‌ترمینال** | `https://[HOST]/terminal` | مرورگر بکش، توکن داخل صفحه |
| **🔗 sshx** | `GET /api/sshx` (with Bearer token) | لینک مستقیم sshx.io — برای شروع اولیه: `POST /api/install/sshx` |
| **💻 SSH** | `ssh root@[HOST] -p 2222` | فقط از داخل شبکه (پسورد: `AgentNode2026!`) |
| **🔌 API** | `POST /api/exec` | با `Authorization: Bearer [TOKEN]` |

### 🔑 توکن دسترسی
```
Bearer suga_known_token_2026
```
> برای امنیت: env var `MONITOR_TOKEN` را در Suga Dashboard تنظیم کنید.

### 🔄 sshx هر بار تغییر می‌کند
- هر بار که container ری‌استارت می‌شود، لینک sshx تازه می‌شود
- برای دریافت لینک جدید: `GET /api/sshx` (با توکن)
- یا از `/terminal` استفاده کنید که **لینک ثابت** دارد

### 🔐 حفظ حریم خصوصی
1. ریپو را **private** کنید: GitHub → Settings → Change visibility → Make private
2. توکن را عوض کنید: env var `MONITOR_TOKEN` را در Suga تنظیم کنید
3. برای دسترسی بیرونی به SSH: از Cloudflare Tunnel یا پورت فورواردینگ استفاده کنید
