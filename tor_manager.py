import os
import time
import subprocess
import threading
import logging

logger = logging.getLogger(__name__)

TOR_SOCKS_PORT   = int(os.environ.get("TOR_SOCKS_PORT", "9050"))
TOR_CONTROL_PORT = int(os.environ.get("TOR_CONTROL_PORT", "9051"))
TOR_DATA_DIR     = "/tmp/tor_data"
TORRC_PATH       = "/tmp/torrc_bot"
TOR_PROXY        = f"socks5://127.0.0.1:{TOR_SOCKS_PORT}"

def _get_tor_password() -> str:
    """Get Tor password from env, or generate a random one if not set."""
    pw = os.environ.get("TOR_PASSWORD", "").strip()
    if pw:
        return pw
    import secrets
    return secrets.token_hex(16)

TOR_PASSWORD = _get_tor_password()

_process = None
_lock    = threading.Lock()
_hashed_password = None


def _generate_hash() -> str:
    result = subprocess.run(
        ["tor", "--hash-password", TOR_PASSWORD],
        capture_output=True, text=True, timeout=10
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("16:"):
            return line
    raise RuntimeError(f"Could not hash Tor password. Output: {result.stdout} {result.stderr}")


def _write_torrc(hashed: str):
    os.makedirs(TOR_DATA_DIR, exist_ok=True)
    torrc = (
        f"SocksPort {TOR_SOCKS_PORT}\n"
        f"ControlPort {TOR_CONTROL_PORT}\n"
        f"HashedControlPassword {hashed}\n"
        f"DataDirectory {TOR_DATA_DIR}\n"
        f"Log notice stderr\n"
        f"ExitPolicy reject *:*\n"
        f"MaxCircuitDirtiness 10\n"
        f"NewCircuitPeriod 10\n"
    )
    with open(TORRC_PATH, "w") as f:
        f.write(torrc)


def start(timeout: int = 45) -> bool:
    global _process, _hashed_password
    with _lock:
        if _process and _process.poll() is None:
            logger.info("Tor already running.")
            return True

        try:
            _hashed_password = _generate_hash()
        except Exception as e:
            logger.error(f"Tor hash error: {e}")
            return False

        _write_torrc(_hashed_password)

        # Log Tor stdout to file and stderr to DEVNULL.
        # Avoid PIPE for both: if the pipe buffer fills and nobody reads it, Tor blocks.
        # Writing stdout to a file lets us inspect it for early-exit debugging.
        _tor_log = "/tmp/tor-bot.log"
        try:
            _log_fh = open(_tor_log, "w")
        except Exception:
            _log_fh = subprocess.DEVNULL

        try:
            _process = subprocess.Popen(
                ["tor", "-f", TORRC_PATH],
                stdout=_log_fh,
                stderr=_log_fh,
            )
        except Exception as e:
            logger.error(f"Tor process start error: {e}")
            return False

        logger.info("Waiting for Tor to connect...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _process.poll() is not None:
                try:
                    tail = open(_tor_log).read()[-300:]
                except Exception:
                    tail = ""
                logger.error(f"Tor exited early. Log tail: {tail}")
                return False
            try:
                from stem.control import Controller
                with Controller.from_port(port=TOR_CONTROL_PORT) as c:
                    c.authenticate(TOR_PASSWORD)
                    info = c.get_info("status/bootstrap-phase", default="")
                    if "PROGRESS=100" in info:
                        logger.info("Tor connected (100%)!")
                        return True
            except Exception:
                pass
            time.sleep(2)

        logger.warning("Tor did not reach 100% bootstrap in time — may still work.")
        return _process.poll() is None


def new_identity() -> bool:
    """Send NEWNYM signal — full identity change, all circuits replaced (Ctrl+Shift+U equivalent)."""
    try:
        from stem import Signal
        from stem.control import Controller
        with Controller.from_port(port=TOR_CONTROL_PORT) as c:
            c.authenticate(TOR_PASSWORD)
            wait = c.get_newnym_wait()
            if wait > 0:
                logger.info(f"Tor: NEWNYM rate limit — waiting {wait:.1f}s...")
                time.sleep(wait + 0.5)
            c.signal(Signal.NEWNYM)
        # Wait for new circuit to fully build before next request
        time.sleep(10)
        logger.info("Tor: New identity applied — fresh circuit ready.")
        return True
    except Exception as e:
        logger.error(f"Tor new_identity error: {e}")
        return False


def new_circuit() -> bool:
    """Close all active BUILT circuits so next connection uses a fresh circuit (Ctrl+Shift+L equivalent).
    Faster than new_identity — no NEWNYM rate limit, only affects routing not cookie/session state."""
    try:
        from stem.control import Controller
        closed = 0
        with Controller.from_port(port=TOR_CONTROL_PORT) as c:
            c.authenticate(TOR_PASSWORD)
            for circuit in c.get_circuits():
                if circuit.status == "BUILT":
                    try:
                        c.close_circuit(circuit.id)
                        closed += 1
                    except Exception:
                        pass
        time.sleep(3)
        logger.info(f"Tor: {closed} circuit(s) closed — fresh circuit on next request.")
        return True
    except Exception as e:
        logger.error(f"Tor new_circuit error: {e}")
        return False


def check_ip_via_tor(timeout: int = 10) -> str:
    """Lightweight Tor exit-IP check using requests (no browser needed).
    Retries multiple APIs so one failure doesn't block verification."""
    try:
        import requests
        proxies = {
            "http":  f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}",
            "https": f"socks5h://127.0.0.1:{TOR_SOCKS_PORT}",
        }
        apis = [
            ("https://api.ipify.org?format=json", "ip"),
            ("https://api.my-ip.io/v2/ip.json",   "ip"),
            ("https://api.myip.com",               "ip"),
        ]
        for url, key in apis:
            try:
                r = requests.get(url, proxies=proxies, timeout=timeout)
                if r.status_code == 200:
                    ip = r.json().get(key, "")
                    if ip:
                        return ip
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"check_ip_via_tor failed: {e}")
    return ""


def get_proxy() -> str:
    return TOR_PROXY


def stop():
    global _process
    with _lock:
        if _process:
            try:
                _process.terminate()
                _process.wait(timeout=5)
            except Exception:
                try:
                    _process.kill()
                except Exception:
                    pass
            _process = None
            logger.info("Tor stopped.")


def is_running() -> bool:
    return _process is not None and _process.poll() is None


def get_current_ip() -> str:
    """Return current Tor exit IP using HTTPS (reuses check_ip_via_tor logic)."""
    ip = check_ip_via_tor(timeout=15)
    return ip if ip else "unknown"
