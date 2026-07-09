"""
Ollama proxy patch — adds token usage reporting to the existing ollama-proxy.py.

This script patches the existing ollama-proxy.py at ~/honcho-self-hosted/ollama-proxy.py
to POST token usage to the Hermes Stick Buddy server after each request.

The patch works by wrapping the response handler to extract usage data from
the OpenAI-compatible response and reporting it to /ollama/record on the
Stick Buddy server.

Manual integration:
  1. Read your existing ollama-proxy.py
  2. Add the reporting call after each successful response
  3. Restart the proxy

Alternatively, use this as a standalone sidecar that tails the Ollama
journald logs and estimates token counts. Less accurate but requires
no proxy modification.
"""

import json
import time
import logging
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# --- Standalone log-tail collector ---
# Tails `journalctl -u ollama -f -o json` and estimates token counts from
# request/response sizes. This is the zero-modification path — less accurate
# but doesn't touch the running proxy.

LOG_TAIL_CMD = "journalctl -u ollama -f -o json --since now"


def estimate_tokens_from_log_line(line: str) -> Optional[int]:
    """Estimate token count from an Ollama journald log line.

    Ollama's GIN logs include request duration and status, but not token counts.
    We use a rough heuristic: ~4 chars per token for text requests.
    For /v1/chat/completions, the response body includes usage data but
    isn't in the log line — we'd need to intercept the actual response.
    """
    try:
        entry = json.loads(line)
        msg = entry.get("MESSAGE", "")
        # GIN log format: | 200 | 2.863010891s | 127.0.0.1 | POST "/v1/chat/completions"
        if "POST" in msg and "chat/completions" in msg:
            # Very rough estimate: each chat completion request processes
            # approximately 500-2000 tokens. We use 1000 as a baseline.
            return 1000
    except Exception:
        pass
    return None


# --- Proxy integration code ---
# Add this to the existing ollama-proxy.py response handler:

PROXY_PATCH_SNIPPET = '''
# --- Hermes Stick Buddy: token usage reporting ---
import urllib.request
import json as _json

STICK_BUDDY_URL = os.environ.get("STICK_BUDDY_URL", "http://localhost:9120")
STICK_BUDDY_TOKEN = os.environ.get("STICK_BUDDY_TOKEN", "")

def _report_ollama_usage(usage: dict):
    """Report token usage to the Hermes Stick Buddy server."""
    try:
        prompt_t = usage.get("prompt_tokens", 0)
        completion_t = usage.get("completion_tokens", 0)
        if prompt_t == 0 and completion_t == 0:
            return
        data = _json.dumps({"prompt_tokens": prompt_t, "completion_tokens": completion_t}).encode()
        req = urllib.request.Request(
            f"{STICK_BUDDY_URL}/ollama/record",
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {STICK_BUDDY_TOKEN}"} if STICK_BUDDY_TOKEN else {}),
            },
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Non-critical — don't break the proxy

# Call _report_ollama_usage(response.get("usage", {})) after each successful
# /v1/chat/completions response, before returning to the client.
# --- End Hermes Stick Buddy patch ---
'''