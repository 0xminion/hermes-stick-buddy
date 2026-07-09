# Hermes Stick Buddy

A desk pet for the M5StickC Plus that monitors your AI agent stack —
Claude Code, Codex, Ollama, and Hermes — running on a remote VPS.

Built on [Anthropic's claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy)
firmware. The stick runs **stock firmware** — no modifications needed.
A Python bridge replaces Claude Desktop's native BLE bridge with your
own, aggregating data from multiple sources into the same wire protocol.

## Architecture

```
Windows (Bluetooth)                    VPS (all AI tools run here)
┌─────────────────────────────┐        ┌──────────────────────────┐
│ BLE Central Daemon          │        │ Aggregation Server       │
│ (ble_central.py)            │ Tails-│ (app.py — FastAPI)        │
│                             │ scale │                           │
│  • Polls VPS every 5s      │◄──────►│  • Claude Code sessions   │
│  • Sends JSON over BLE     │ HTTPS │  • Codex sessions         │
│  • Relays stick commands   │       │  • Hermes state.db        │
└──────────┬──────────────────┘        │  • Ollama /api/ps         │
           │ BLE                        └──────────────────────────┘
           ▼
    M5StickC Plus
    (stock firmware)
    ┌──────────────────┐
    │ Screen: Normal   │ → Token usage, per-source breakdown
    │ Screen: Pet      │ → Animated buddy (idle/busy/sleep based on Hermes)
    │ Screen: Info     │ → 5h + weekly token totals
    │ Screen: Approval │ → (future: Hermes approval bridge)
    └──────────────────┘
```

## What it displays

| Screen | Content |
|--------|---------|
| **Normal** | 5h output tokens, weekly output, per-source breakdown (Hermes/Claude/Ollama/Codex), full token count (incl. cache reads), Hermes cost, Ollama loaded models, active session count |
| **Pet** | Animated buddy: sleeps when no Hermes sessions, idle when session open, busy when agent generating, celebrates every 50K output tokens |
| **Info** | 5h token total (drives the level counter), approval stats |
| **Approval** | (Future — not wired yet, shows nothing) |

## Setup

### 1. Flash the stick (one-time)

Install [PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/),
then from the repo root:

```bash
pio run -t upload
```

If upgrading from a previous flash, wipe first:
```bash
pio run -t erase && pio run -t upload
```

The stick advertises as `Claude*` over BLE — your daemon finds it automatically.

### 2. VPS-side: Start the aggregation server

```bash
# Create a venv and install deps
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pyyaml

# Generate an auth token
export STICK_BUDDY_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")

# Start the server (binds to 127.0.0.1:9120)
cd server
python3 app.py
```

The server runs on `127.0.0.1:9120`. Use Tailscale serve to expose it
over HTTPS:

```bash
tailscale serve --https=9120 http://127.0.0.1:9120
```

Verify:
```bash
curl https://your-vps.tailnet:9120/health
curl https://your-vps.tailnet:9120/heartbeat -H "Authorization: Bearer $STICK_BUDDY_TOKEN"
```

### 3. Windows-side: Run the BLE central daemon

```bash
# Install deps
pip install bleak requests pyyaml

# Run
python ble_central.py \
    --url https://your-vps.tailnet:9120 \
    --token YOUR_TOKEN_HERE
```

The daemon will:
1. Connect to the VPS server over Tailscale HTTPS
2. Scan for BLE devices starting with "Claude"
3. Pair and connect to the stick
4. Poll the VPS every 5s and send heartbeats over BLE
5. The stick's pet wakes up, starts displaying your token usage

### 4. (Optional) Ollama token tracking

Ollama doesn't expose cumulative token stats. To track Ollama usage,
add the reporting snippet from `server/ollama_patch.py` to your
existing Ollama proxy. See the file for integration instructions.

Without the patch, Ollama tracking shows loaded models only (no token counts).

## Configuration

### server/config.yaml

```yaml
server:
  host: "127.0.0.1"
  port: 9120

sources:
  claude_code_dir: "~/.claude/projects"
  codex_sessions_dir: "~/.codex/sessions"
  codex_logs_db: "~/.codex/logs_2.sqlite"
  hermes_state_db: "~/.hermes/state.db"
  hermes_logs_dir: "~/.hermes/logs"
  ollama_url: "http://localhost:11434"
  ollama_proxy_url: "http://localhost:11435"

windows:
  five_hour_seconds: 18000
  weekly_seconds: 604800

polling:
  hermes_sessions: 5
  claude_code: 30
  codex: 30
  ollama: 15

auth:
  token_env: "STICK_BUDDY_TOKEN"
```

### .env

```
STICK_BUDDY_TOKEN=your-generated-token-here
```

## API Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | No | Health check |
| `GET /heartbeat` | Yes | Returns the stick-compatible JSON heartbeat |
| `GET /stats/detailed` | Yes | Detailed per-source breakdown (for debugging) |
| `POST /ollama/record` | Yes | Record Ollama token usage (from proxy patch) |

## Data Sources

| Source | What's tracked | How |
|--------|---------------|-----|
| **Claude Code** | Input, output, cache read/write tokens per session | Parse `~/.claude/projects/**/*.jsonl` — assistant messages contain `usage` objects |
| **Codex** | Session count, recent interactions | Parse `~/.codex/sessions/**/*.jsonl` — Codex doesn't store token counts, so we track activity as a proxy |
| **Hermes** | Per-session tokens, cost, active sessions | Query `~/.hermes/state.db` — sessions table has `input_tokens`, `output_tokens`, `estimated_cost_usd` |
| **Ollama** | Loaded models, token usage (with patch) | Poll `/api/ps` for loaded models; token counts require the proxy patch |

## Limitations

- **Codex tokens**: Codex CLI doesn't store token counts in session files.
  We estimate ~4K tokens per interaction as a rough proxy.
- **Ollama tokens**: Without the proxy patch, only loaded models are shown
  (no token counts). The patch adds a POST to `/ollama/record` after each
  proxy response.
- **Approvals**: The approval screen is not wired — the stick shows nothing
  for approval prompts. Adding this requires a Hermes-side approval state
  endpoint + forwarding stick decisions back to Hermes.
- **BLE range**: The stick must be within ~10m of the Windows machine's
  Bluetooth radio.

## Wire Protocol

The stick uses the [Nordic UART Service](https://www.nordicsemi.com/products/nrf-connect-sdk)
with newline-delimited JSON. See `REFERENCE.md` in the firmware repo for the
full protocol spec. The key heartbeat fields:

```json
{
  "total": 1,
  "running": 0,
  "waiting": 0,
  "tokens": 750434,
  "tokens_today": 750434,
  "msg": "H:621K C:128K",
  "entries": ["5h: 750,434 out", "week: 2,244,981 out", ...]
}
```

## License

Firmware: Anthropic's claude-desktop-buddy (see LICENSE in repo root).
Server + BLE daemon: MIT.