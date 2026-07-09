"""
Token usage collectors for Hermes Stick Buddy.

Each collector reads from a different source and returns token counts
within a rolling time window. Results are cached with TTLs to avoid
hammering the filesystem / APIs.
"""

import os
import json
import time
import sqlite3
import glob
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _expand(path: str) -> str:
    return os.path.expanduser(path)


class ClaudeCodeCollector:
    """Reads Claude Code session JSONL files for token usage."""

    def __init__(self, sessions_dir: str, cache_ttl: int = 30):
        self.sessions_dir = _expand(sessions_dir)
        self._cache_ttl = cache_ttl
        self._cache_ts = 0
        self._cached = []

    def _scan_sessions(self):
        """Scan all JSONL files, collect (timestamp, input_tokens, output_tokens)."""
        results = []
        pattern = os.path.join(self.sessions_dir, "**", "*.jsonl")
        files = glob.glob(pattern, recursive=True)
        for f in files:
            try:
                mtime = os.path.getmtime(f)
                with open(f, "r", errors="replace") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "assistant":
                                msg = obj.get("message", {})
                                usage = msg.get("usage")
                                if usage:
                                    # Claude Code assistant messages have timestamps
                                    ts_str = obj.get("timestamp", "")
                                    ts = None
                                    if ts_str:
                                        from datetime import datetime, timezone
                                        try:
                                            dt = datetime.fromisoformat(
                                                ts_str.replace("Z", "+00:00")
                                            )
                                            ts = dt.timestamp()
                                        except Exception:
                                            pass
                                    if ts is None:
                                        ts = mtime  # fallback to file mtime

                                    input_t = usage.get("input_tokens", 0)
                                    output_t = usage.get("output_tokens", 0)
                                    cache_read = usage.get("cache_read_input_tokens", 0)
                                    cache_write = usage.get("cache_creation_input_tokens", 0)
                                    results.append({
                                        "ts": ts,
                                        "input_tokens": input_t,
                                        "output_tokens": output_t,
                                        "cache_read": cache_read,
                                        "cache_write": cache_write,
                                    })
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.debug(f"Error reading Claude Code session {f}: {e}")
        return results

    def get_tokens(self, window_seconds: int) -> dict:
        """Return token counts within the rolling window."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._cached = self._scan_sessions()
            self._cache_ts = now

        cutoff = now - window_seconds
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        count = 0
        for entry in self._cached:
            if entry["ts"] >= cutoff:
                total_input += entry["input_tokens"]
                total_output += entry["output_tokens"]
                total_cache_read += entry["cache_read"]
                total_cache_write += entry["cache_write"]
                count += 1

        return {
            "source": "claude_code",
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "total_tokens": total_input + total_output + total_cache_read + total_cache_write,
            "request_count": count,
        }


class CodexCollector:
    """Reads Codex session JSONL files for activity tracking.

    Codex doesn't store token counts in session files (unlike Claude Code).
    We track session count and duration as a proxy, plus check the SQLite logs
    for recent activity. If Codex gains token tracking in the future, we'll
    parse it here.
    """

    def __init__(self, sessions_dir: str, logs_db: str, cache_ttl: int = 30):
        self.sessions_dir = _expand(sessions_dir)
        self.logs_db = _expand(logs_db)
        self._cache_ttl = cache_ttl
        self._cache_ts = 0
        self._cached = []

    def _scan_sessions(self):
        """Scan Codex session files for activity timestamps."""
        results = []
        pattern = os.path.join(self.sessions_dir, "**", "*.jsonl")
        files = glob.glob(pattern, recursive=True)
        for f in files:
            try:
                mtime = os.path.getmtime(f)
                # Codex session files have response_item entries with timestamps
                with open(f, "r", errors="replace") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "response_item":
                                payload = obj.get("payload", {})
                                if payload.get("type") == "message" and payload.get("role") == "assistant":
                                    ts_str = obj.get("timestamp", "")
                                    ts = None
                                    if ts_str:
                                        from datetime import datetime, timezone
                                        try:
                                            dt = datetime.fromisoformat(
                                                ts_str.replace("Z", "+00:00")
                                            )
                                            ts = dt.timestamp()
                                        except Exception:
                                            pass
                                    if ts is None:
                                        ts = mtime
                                    results.append({"ts": ts, "type": "assistant_msg"})
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.debug(f"Error reading Codex session {f}: {e}")
        return results

    def get_activity(self, window_seconds: int) -> dict:
        """Return Codex activity within the rolling window."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._cached = self._scan_sessions()
            self._cache_ts = now

        cutoff = now - window_seconds
        count = sum(1 for e in self._cached if e["ts"] >= cutoff)
        return {
            "source": "codex",
            "session_count": len(self._cached),
            "recent_interactions": count,
            "token_estimate": count * 4000,  # rough estimate: ~4K tokens per Codex interaction
        }


class HermesCollector:
    """Reads Hermes state.db for per-session token tracking.

    Hermes sessions table has: input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens, reasoning_tokens, estimated_cost_usd.
    """

    def __init__(self, db_path: str, cache_ttl: int = 5):
        self.db_path = _expand(db_path)
        self._cache_ttl = cache_ttl
        self._cache_ts = 0
        self._cached = []

    def _query_sessions(self):
        """Query Hermes sessions for token data."""
        results = []
        if not os.path.exists(self.db_path):
            logger.warning(f"Hermes state DB not found: {self.db_path}")
            return results

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # Get all sessions with token data, using started_at as timestamp
            cur.execute("""
                SELECT id, source, model, started_at, ended_at,
                       input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, reasoning_tokens,
                       estimated_cost_usd, api_call_count
                FROM sessions
                WHERE input_tokens > 0 OR output_tokens > 0
                ORDER BY started_at DESC
            """)
            for row in cur.fetchall():
                results.append({
                    "ts": row["started_at"],
                    "source": row["source"],
                    "model": row["model"],
                    "input_tokens": row["input_tokens"] or 0,
                    "output_tokens": row["output_tokens"] or 0,
                    "cache_read_tokens": row["cache_read_tokens"] or 0,
                    "cache_write_tokens": row["cache_write_tokens"] or 0,
                    "reasoning_tokens": row["reasoning_tokens"] or 0,
                    "estimated_cost_usd": row["estimated_cost_usd"] or 0.0,
                    "api_call_count": row["api_call_count"] or 0,
                })
            conn.close()
        except Exception as e:
            logger.error(f"Error querying Hermes state DB: {e}")
        return results

    def get_tokens(self, window_seconds: int) -> dict:
        """Return Hermes token counts within the rolling window."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._cached = self._query_sessions()
            self._cache_ts = now

        cutoff = now - window_seconds
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_reasoning = 0
        total_cost = 0.0
        count = 0
        for entry in self._cached:
            if entry["ts"] >= cutoff:
                total_input += entry["input_tokens"]
                total_output += entry["output_tokens"]
                total_cache_read += entry["cache_read_tokens"]
                total_cache_write += entry["cache_write_tokens"]
                total_reasoning += entry["reasoning_tokens"]
                total_cost += entry["estimated_cost_usd"]
                count += 1

        return {
            "source": "hermes",
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "reasoning_tokens": total_reasoning,
            "total_tokens": total_input + total_output + total_cache_read + total_cache_write + total_reasoning,
            "estimated_cost_usd": round(total_cost, 4),
            "session_count": count,
        }

    def get_active_sessions(self) -> dict:
        """Get currently active Hermes sessions for buddy/pet state."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            self._cached = self._query_sessions()
            self._cache_ts = now

        # Active = no ended_at, or ended within last 60s
        active = []
        waiting = 0
        for entry in self._cached:
            # Check if session is recent (last 5 min)
            if now - entry["ts"] < 300:
                active.append(entry)

        return {
            "total": len(self._cached),
            "active": len(active),
            "running": sum(1 for e in active if e["api_call_count"] > 0),
        }


class OllamaCollector:
    """Tracks Ollama usage by counting requests through a lightweight counter.

    Ollama doesn't expose cumulative token stats. We track:
    1. Loaded models (from /api/ps)
    2. A rolling token counter via a local state file that we increment
       by querying the Ollama proxy's response usage fields.

    Since we can't intercept all Ollama traffic without a proxy modification,
    we use a state-file approach: a background task periodically samples
    Ollama's loaded models and tracks estimated token throughput.
    """

    def __init__(self, ollama_url: str, proxy_url: str, state_file: str = None, cache_ttl: int = 15):
        self.ollama_url = ollama_url
        self.proxy_url = proxy_url
        self._cache_ttl = cache_ttl
        self._cache_ts = 0
        self._cached = {}
        self.state_file = state_file or os.path.expanduser("~/.hermes/stick-buddy-ollama-state.json")

    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"total_tokens": 0, "history": [], "last_reset": time.time()}

    def _save_state(self, state: dict):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f)

    def _poll_loaded_models(self):
        """Check what models are loaded in Ollama."""
        import urllib.request
        try:
            url = f"{self.ollama_url}/api/ps"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = [m.get("name", "") for m in data.get("models", [])]
                return models
        except Exception as e:
            logger.debug(f"Error polling Ollama /api/ps: {e}")
            return []

    def get_stats(self, window_seconds: int) -> dict:
        """Return Ollama stats within the rolling window."""
        now = time.time()
        if now - self._cache_ts > self._cache_ttl:
            models = self._poll_loaded_models()
            state = self._load_state()
            # Prune history outside the window
            cutoff = now - window_seconds
            state["history"] = [
                h for h in state.get("history", [])
                if h.get("ts", 0) >= cutoff
            ]
            self._cached = {
                "loaded_models": models,
                "total_tokens_window": sum(h.get("tokens", 0) for h in state["history"]),
                "request_count": len(state["history"]),
            }
            self._cache_ts = now

        return {
            "source": "ollama",
            "loaded_models": self._cached["loaded_models"],
            "total_tokens": self._cached["total_tokens_window"],
            "request_count": self._cached["request_count"],
        }

    def record_usage(self, prompt_tokens: int, completion_tokens: int):
        """Called by the Ollama proxy interceptor to record token usage."""
        state = self._load_state()
        state["history"].append({
            "ts": time.time(),
            "tokens": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        # Prune history older than 7 days
        cutoff = time.time() - 604800
        state["history"] = [h for h in state["history"] if h["ts"] >= cutoff]
        state["total_tokens"] = sum(h["tokens"] for h in state["history"])
        self._save_state(state)