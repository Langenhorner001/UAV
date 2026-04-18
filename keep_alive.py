import os
import json
import threading
import time
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

# ── Config (set in .env to override) ─────────────────────────
# SELF_PING_ENABLED=true   → pinger ON  (use on Replit to prevent sleep)
# SELF_PING_ENABLED=false  → pinger OFF (default — not needed on EC2)
SELF_PING_ENABLED = os.environ.get("SELF_PING_ENABLED", "false").lower() in ("1", "true", "yes")
PING_URL          = os.environ.get("PING_URL", "https://auto-tg-bot-clicker.replit.app/api")
PING_INTERVAL     = int(os.environ.get("PING_INTERVAL", "1"))

# Provider callable injected by main bot — returns dict for /health JSON.
# Signature: () -> dict[str, Any]
_health_provider = None


def set_health_provider(fn):
    """Register a callable that returns runtime stats for /health endpoint."""
    global _health_provider
    _health_provider = fn


def _make_handler():
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/health"):
                payload = {"status": "ok"}
                if _health_provider:
                    try:
                        payload.update(_health_provider() or {})
                    except Exception as e:
                        payload = {"status": "err", "error": str(e)}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # Default: alive ping
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive!")

        def log_message(self, format, *args):
            pass

    return _Handler


def keep_alive(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), _make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _self_ping_loop(url: str, interval: int):
    try:
        import requests as _req
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        logger.warning("requests not installed — self-pinger disabled")
        return

    consecutive_fails = 0
    while True:
        try:
            r = _req.get(url, timeout=5, verify=False)
            if consecutive_fails > 0:
                logger.info(f"Self-ping OK ({r.status_code}) — back online after {consecutive_fails} fail(s)")
                consecutive_fails = 0
        except Exception as e:
            consecutive_fails += 1
            if consecutive_fails == 1 or consecutive_fails % 30 == 0:
                logger.warning(f"Self-ping failed ({consecutive_fails}x): {e}")
        time.sleep(interval)


def start_self_pinger(url: str = PING_URL, interval: int = PING_INTERVAL):
    if not SELF_PING_ENABLED:
        logger.info("Self-pinger disabled (SELF_PING_ENABLED=false) — not needed on EC2")
        return None
    t = threading.Thread(target=_self_ping_loop, args=(url, interval), daemon=True)
    t.start()
    logger.info(f"Self-pinger started → {url} (every {interval}s)")
    return t
