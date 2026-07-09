"""
Hermes Stick Buddy — VPS-side aggregation server.

Runs on the VPS, aggregates token usage from Claude Code, Codex, Ollama,
and Hermes, then exposes a single /heartbeat endpoint that the Windows
BLE central daemon polls over Tailscale HTTPS.

The JSON schema matches the claude-desktop-buddy wire protocol from
REFERENCE.md so the stock M5StickC Plus firmware works unchanged.
"""

import os
import sys
import time
import json
import yaml
import logging
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
import uvicorn

# Ensure we can import collectors from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collectors import ClaudeCodeCollector, CodexCollector, HermesCollector, OllamaCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stick-buddy")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


config = load_config()
sources_cfg = config.get("sources", {})
windows_cfg = config.get("windows", {})
polling_cfg = config.get("polling", {})
server_cfg = config.get("server", {})
auth_cfg = config.get("auth", {})

# Initialize collectors
claude_collector = ClaudeCodeCollector(
    sessions_dir=sources_cfg.get("claude_code_dir", "~/.claude/projects"),
    cache_ttl=polling_cfg.get("claude_code", 30),
)
codex_collector = CodexCollector(
    sessions_dir=sources_cfg.get("codex_sessions_dir", "~/.codex/sessions"),
    logs_db=sources_cfg.get("codex_logs_db", "~/.codex/logs_2.sqlite"),
    cache_ttl=polling_cfg.get("codex", 30),
)
hermes_collector = HermesCollector(
    db_path=sources_cfg.get("hermes_state_db", "~/.hermes/state.db"),
    cache_ttl=polling_cfg.get("hermes_sessions", 5),
)
ollama_collector = OllamaCollector(
    ollama_url=sources_cfg.get("ollama_url", "http://localhost:11434"),
    proxy_url=sources_cfg.get("ollama_proxy_url", "http://localhost:11435"),
    cache_ttl=polling_cfg.get("ollama", 15),
)

FIVE_HOURS = windows_cfg.get("five_hour_seconds", 18000)
ONE_WEEK = windows_cfg.get("weekly_seconds", 604800)

# --- Auth ---
_auth_token = os.environ.get(auth_cfg.get("token_env", "STICK_BUDDY_TOKEN"), "")


def _check_auth(authorization: Optional[str]) -> bool:
    """Validate bearer token. If no token is configured, allow all (local-only mode)."""
    if not _auth_token:
        return True  # No auth configured = local dev mode
    if not authorization:
        return False
    if authorization.startswith("Bearer "):
        token = authorization[7:]
        return token == _auth_token
    return False


def _truncate(s: str, maxlen: int) -> str:
    if len(s) <= maxlen:
        return s
    return s[: maxlen - 3] + "..."


def build_heartbeat() -> dict:
    """Build the unified heartbeat JSON matching the stick's wire protocol.

    Fields consumed by firmware (from data.h):
    - total (uint8): session count → Hermes active sessions
    - running (uint8): sessions actively generating → Hermes running sessions
    - waiting (uint8): sessions blocked on approval → 0 (approvals not wired yet)
    - tokens (uint32): cumulative tokens → 5h window total (drives level-up)
    - tokens_today (uint32): today's tokens → 5h window total
    - msg (char[24]): one-line summary
    - entries[] (max 8, char[92]): activity lines
    - prompt: {id, tool, hint} → not used yet (no approval bridge)
    """
    now = time.time()

    # Collect from all sources
    claude_5h = claude_collector.get_tokens(FIVE_HOURS)
    codex_5h = codex_collector.get_activity(FIVE_HOURS)
    hermes_5h = hermes_collector.get_tokens(FIVE_HOURS)
    ollama_5h = ollama_collector.get_stats(FIVE_HOURS)

    # Weekly totals
    claude_week = claude_collector.get_tokens(ONE_WEEK)
    hermes_week = hermes_collector.get_tokens(ONE_WEEK)
    ollama_week = ollama_collector.get_stats(ONE_WEEK)

    # Hermes session state for buddy/pet
    hermes_sessions = hermes_collector.get_active_sessions()

    # Aggregate 5h tokens — use output tokens for display (cache reads are
    # essentially free, including them inflates the number and triggers
    # constant level-up celebrations on the stick)
    output_5h = (
        claude_5h["output_tokens"]
        + hermes_5h["output_tokens"]
        + codex_5h.get("token_estimate", 0)
        + ollama_5h["total_tokens"]
    )
    # Full total (including cache) for the detailed view
    total_5h = (
        claude_5h["total_tokens"]
        + hermes_5h["total_tokens"]
        + codex_5h.get("token_estimate", 0)
        + ollama_5h["total_tokens"]
    )

    # Weekly totals
    output_week = (
        claude_week["output_tokens"]
        + hermes_week["output_tokens"]
        + ollama_week["total_tokens"]
    )
    total_week = (
        claude_week["total_tokens"]
        + hermes_week["total_tokens"]
        + ollama_week["total_tokens"]
    )

    # Build msg (max 24 chars) — use output tokens
    msg_parts = []
    if hermes_5h["output_tokens"] > 0:
        msg_parts.append(f"H:{hermes_5h['output_tokens']//1000}K")
    if claude_5h["output_tokens"] > 0:
        msg_parts.append(f"C:{claude_5h['output_tokens']//1000}K")
    if ollama_5h["total_tokens"] > 0:
        msg_parts.append(f"O:{ollama_5h['total_tokens']//1000}K")
    if codex_5h["recent_interactions"] > 0:
        msg_parts.append(f"X:{codex_5h['recent_interactions']}")
    msg = " ".join(msg_parts) if msg_parts else "idle"
    msg = _truncate(msg, 24)

    # Build entries[] (max 8 lines, each max 92 chars)
    entries = []

    # Line 1: 5h output tokens (what the stick counts for level-up)
    entries.append(_truncate(f"5h: {output_5h:,} out", 92))

    # Line 2: Weekly output
    entries.append(_truncate(f"week: {output_week:,} out", 92))

    # Line 3: Per-source 5h breakdown (output only)
    src_parts = []
    if hermes_5h["output_tokens"] > 0:
        src_parts.append(f"Hermes {hermes_5h['output_tokens']//1000}K")
    if claude_5h["output_tokens"] > 0:
        src_parts.append(f"Claude {claude_5h['output_tokens']//1000}K")
    if ollama_5h["total_tokens"] > 0:
        src_parts.append(f"Ollama {ollama_5h['total_tokens']//1000}K")
    if codex_5h["recent_interactions"] > 0:
        src_parts.append(f"Codex x{codex_5h['recent_interactions']}")
    if src_parts:
        entries.append(_truncate(" | ".join(src_parts), 92))
    else:
        entries.append("no recent activity")

    # Line 4: Full total (incl cache reads) for reference
    entries.append(_truncate(f"full 5h: {total_5h:,} tok", 92))

    # Line 5: Hermes cost
    if hermes_5h["estimated_cost_usd"] > 0:
        entries.append(_truncate(f"cost 5h: ${hermes_5h['estimated_cost_usd']:.2f}", 92))

    # Line 6: Ollama loaded models
    if ollama_5h["loaded_models"]:
        models_str = ", ".join(ollama_5h["loaded_models"][:3])
        entries.append(_truncate(f"ollama: {models_str}", 92))

    # Line 7: Hermes session info
    if hermes_sessions["total"] > 0:
        entries.append(_truncate(
            f"sessions: {hermes_sessions['active']} active / {hermes_sessions['total']} total",
            92,
        ))

    # Line 8: Weekly cost
    if hermes_week["estimated_cost_usd"] > 0:
        entries.append(_truncate(f"week cost: ${hermes_week['estimated_cost_usd']:.2f}", 92))

    # Ensure max 8 entries
    entries = entries[:8]

    # Build the heartbeat
    heartbeat = {
        "total": min(hermes_sessions["active"] or 1, 255) if hermes_sessions["total"] > 0 else 0,
        "running": min(hermes_sessions["running"], 255),
        "waiting": 0,  # No approval bridge yet
        "tokens": output_5h,  # Output tokens drive level-up celebration
        "tokens_today": output_5h,  # Shown on Info screen
        "msg": msg,
        "entries": entries,
        # No prompt field — approval bridge is a future enhancement
    }

    return heartbeat


# --- FastAPI app ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Hermes Stick Buddy server starting")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info(f"Auth: {'enabled' if _auth_token else 'disabled (local dev mode)'}")
    yield
    logger.info("Hermes Stick Buddy server stopping")


app = FastAPI(title="Hermes Stick Buddy", lifespan=lifespan)


@app.get("/health")
async def health():
    """Health check — no auth required."""
    return {"status": "ok", "timestamp": time.time()}


@app.get("/heartbeat")
async def heartbeat(authorization: Optional[str] = Header(None)):
    """Main endpoint — returns the unified heartbeat JSON.

    The BLE central daemon polls this every 3-5s and forwards the JSON
    to the M5StickC Plus over BLE Nordic UART Service.
    """
    if not _check_auth(authorization):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    return JSONResponse(content=build_heartbeat())


@app.get("/stats/detailed")
async def detailed_stats(authorization: Optional[str] = Header(None)):
    """Detailed breakdown for debugging — not sent to the stick."""
    if not _check_auth(authorization):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    return JSONResponse(content={
        "claude_code_5h": claude_collector.get_tokens(FIVE_HOURS),
        "claude_code_week": claude_collector.get_tokens(ONE_WEEK),
        "codex_5h": codex_collector.get_activity(FIVE_HOURS),
        "hermes_5h": hermes_collector.get_tokens(FIVE_HOURS),
        "hermes_week": hermes_collector.get_tokens(ONE_WEEK),
        "hermes_sessions": hermes_collector.get_active_sessions(),
        "ollama_5h": ollama_collector.get_stats(FIVE_HOURS),
        "ollama_week": ollama_collector.get_stats(ONE_WEEK),
        "timestamp": time.time(),
    })


@app.post("/ollama/record")
async def record_ollama_usage(
    body: dict,
    authorization: Optional[str] = Header(None),
):
    """Endpoint for the Ollama proxy interceptor to report token usage.

    Call this from a modified ollama-proxy.py that posts usage data
    after each request. Body: {"prompt_tokens": N, "completion_tokens": N}
    """
    if not _check_auth(authorization):
        raise HTTPException(status_code=401, detail="Invalid or missing token")

    prompt_t = body.get("prompt_tokens", 0)
    completion_t = body.get("completion_tokens", 0)
    ollama_collector.record_usage(prompt_t, completion_t)
    return {"status": "ok", "recorded": prompt_t + completion_t}


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else server_cfg.get("port", 9120)
    host = server_cfg.get("host", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")