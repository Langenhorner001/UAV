import os
import time
import threading

TOR_BROWSER_DEFAULT_PATH = r"C:\Tor Browser\Browser\firefox.exe"


def find_tor_browser_path():
    """
    Auto-detect Tor Browser's firefox.exe by:
      1. Scanning running processes for firefox.exe inside a 'Tor Browser' folder
      2. Falling back to common installation paths
    Returns the path string, or None if not found.
    """
    try:
        import psutil
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info["name"] or "").lower()
                exe = proc.info["exe"] or ""
                if name in ("firefox.exe", "firefox") and "tor browser" in exe.lower():
                    if os.path.isfile(exe):
                        return exe
            except Exception:
                continue
    except ImportError:
        pass

    up = os.environ.get("USERPROFILE", "")
    ap = os.environ.get("APPDATA", "")
    lp = os.environ.get("LOCALAPPDATA", "")

    candidates = [
        r"C:\Tor Browser\Browser\firefox.exe",
        r"C:\Program Files\Tor Browser\Browser\firefox.exe",
        r"C:\Program Files (x86)\Tor Browser\Browser\firefox.exe",
        os.path.join(up, "Desktop", "Tor Browser", "Browser", "firefox.exe"),
        os.path.join(up, "Downloads", "Tor Browser", "Browser", "firefox.exe"),
        os.path.join(up, "Tor Browser", "Browser", "firefox.exe"),
        os.path.join(ap, "Tor Browser", "Browser", "firefox.exe"),
        os.path.join(lp, "Tor Browser", "Browser", "firefox.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    return None


def _close_uncontrolled_tor_browsers(status_cb=None):
    """
    Kill any running Tor Browser processes that Selenium does not control.
    Called before launching a new driver to avoid duplicate windows.
    """
    try:
        import psutil
        targets = []
        for proc in psutil.process_iter(["name", "exe", "pid"]):
            try:
                name = (proc.info["name"] or "").lower()
                exe = proc.info["exe"] or ""
                if name in ("firefox.exe", "firefox") and "tor browser" in exe.lower():
                    targets.append(proc)
            except Exception:
                continue

        if targets:
            if status_cb:
                status_cb("Closing existing Tor Browser...")
            for proc in targets:
                try:
                    proc.kill()
                except Exception:
                    pass
            time.sleep(3)
    except ImportError:
        pass


TOR_SOCKS_HOST = "127.0.0.1"
TOR_SOCKS_PORT = 9150

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, WebDriverException
    )
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    from webdriver_manager.firefox import GeckoDriverManager
    WDM_AVAILABLE = True
except ImportError:
    WDM_AVAILABLE = False

try:
    from stem import Signal
    from stem.control import Controller
    STEM_AVAILABLE = True
except ImportError:
    STEM_AVAILABLE = False


def _get_service():
    if WDM_AVAILABLE:
        try:
            return Service(GeckoDriverManager().install())
        except Exception:
            pass
    return Service()


def _parse_proxy_url(proxy_url):
    """
    Parse proxy_url into (host, port, user, password).
    Accepted formats:
        host:port
        user:pass@host:port
    Returns None if the string is malformed.
    """
    url = proxy_url.strip()
    if not url:
        return None
    try:
        user = password = ""
        if "@" in url:
            auth, url = url.rsplit("@", 1)
            user, password = auth.split(":", 1) if ":" in auth else (auth, "")
        host, port_str = url.rsplit(":", 1)
        return host.strip(), int(port_str), user, password
    except Exception:
        return None


def _apply_proxy(options, proxy_type, proxy_url):
    """
    Configure Firefox proxy preferences from proxy_url.
    proxy_type : "SOCKS5" or "HTTP"
    Returns True on success, False on bad input.
    """
    parsed = _parse_proxy_url(proxy_url)
    if parsed is None:
        return False
    host, port, user, password = parsed

    options.set_preference("network.proxy.type", 1)
    options.set_preference("network.proxy.no_proxies_on", "")

    if proxy_type == "SOCKS5":
        options.set_preference("network.proxy.socks", host)
        options.set_preference("network.proxy.socks_port", port)
        options.set_preference("network.proxy.socks_version", 5)
        options.set_preference("network.proxy.socks_remote_dns", True)
        if user:
            options.set_preference("network.proxy.socks_username", user)
            options.set_preference("network.proxy.socks_password", password)
    else:
        options.set_preference("network.proxy.http", host)
        options.set_preference("network.proxy.http_port", port)
        options.set_preference("network.proxy.ssl", host)
        options.set_preference("network.proxy.ssl_port", port)
    return True


def build_driver(tor_binary_path=TOR_BROWSER_DEFAULT_PATH,
                 proxy_enabled=False, proxy_type="SOCKS5", proxy_url=""):
    options = Options()
    options.binary_location = tor_binary_path

    # Return as soon as the DOM is interactive — do not wait for every
    # image / late script to finish.  Tor is slow; "normal" strategy
    # regularly times out on heavy SPAs like Polymarket.
    options.page_load_strategy = "eager"

    if proxy_enabled and proxy_url.strip():
        _apply_proxy(options, proxy_type, proxy_url)
    else:
        options.set_preference("network.proxy.type", 1)
        options.set_preference("network.proxy.socks", TOR_SOCKS_HOST)
        options.set_preference("network.proxy.socks_port", TOR_SOCKS_PORT)
        options.set_preference("network.proxy.socks_version", 5)
        options.set_preference("network.proxy.socks_remote_dns", True)

    # Skip the "Connect to Tor" prompt — connect automatically on launch.
    # Do NOT set start_tor = False; Tor Browser must manage its own Tor daemon.
    options.set_preference("extensions.torlauncher.prompt_at_startup", False)

    driver = webdriver.Firefox(service=_get_service(), options=options)
    driver.set_page_load_timeout(45)
    return driver


_PICKER_JS = """
window.__pickedSelector = null;
(function () {
    function buildSelector(el) {
        var sel = el.tagName.toLowerCase();
        if (el.id) {
            sel += '#' + el.id;
        }
        var classes = [];
        for (var i = 0; i < Math.min(el.classList.length, 3); i++) {
            classes.push(el.classList[i]);
        }
        if (classes.length) { sel += '.' + classes.join('.'); }
        return sel;
    }
    function handler(e) {
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();
        window.__pickedSelector = buildSelector(e.target);
        e.target.style.outline = '3px solid #e94560';
        e.target.style.outlineOffset = '2px';
        document.removeEventListener('click', handler, true);
    }
    document.addEventListener('click', handler, true);
}());
"""


def _run_picker_on_driver(driver, status_cb, timeout=60):
    """Inject the JS picker into an already-open driver and wait for a click."""
    try:
        driver.execute_script(_PICKER_JS)
        status_cb("Click any element in the open browser window...")
        elapsed = 0.0
        while elapsed < timeout:
            result = driver.execute_script("return window.__pickedSelector;")
            if result:
                return result
            time.sleep(0.4)
            elapsed += 0.4
        status_cb("Timed out — no element was clicked")
        return None
    except WebDriverException as e:
        status_cb(f"Picker error: {str(e)[:80]}")
        return None
    except Exception as e:
        status_cb(f"Picker error: {str(e)[:80]}")
        return None


def pick_element(tor_binary_path, url, status_cb, timeout=60):
    driver = None
    try:
        status_cb("Launching browser for element picking...")
        driver = build_driver(tor_binary_path=tor_binary_path)

        status_cb("Navigating to target URL...")
        driver.get(url)
        time.sleep(3)

        driver.execute_script(_PICKER_JS)
        status_cb("Browser open — click any element to capture its selector...")

        elapsed = 0.0
        poll = 0.4
        while elapsed < timeout:
            result = driver.execute_script("return window.__pickedSelector;")
            if result:
                return result
            time.sleep(poll)
            elapsed += poll

        status_cb("Timed out — no element was clicked")
        return None

    except WebDriverException as e:
        status_cb(f"Browser error: {str(e)[:80]}")
        return None
    except Exception as e:
        status_cb(f"Pick error: {str(e)[:80]}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


class SeleniumAutomation:
    def __init__(self, status_cb, loop_cb, tor_binary_path=TOR_BROWSER_DEFAULT_PATH):
        self.status_cb = status_cb
        self.loop_cb = loop_cb
        self.tor_binary_path = tor_binary_path
        self._stop_event = threading.Event()
        self._driver = None
        self._active_thread = None

    def stop(self):
        """Pause the loop — browser stays open."""
        self._stop_event.set()

    def quit(self):
        """Stop loop and close the browser."""
        self._stop_event.set()
        self._quit_driver()

    def _quit_driver(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def _sleep_interruptible(self, seconds):
        interval = 0.1
        elapsed = 0.0
        while elapsed < seconds:
            if self._stop_event.is_set():
                return False
            time.sleep(interval)
            elapsed += interval
        return True

    def _click_selector(self, selector, wait_secs=20):
        if not selector or not selector.strip():
            return True
        try:
            element = WebDriverWait(self._driver, wait_secs).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector.strip()))
            )
            element.click()
            return True
        except TimeoutException:
            self.status_cb(f"Timeout waiting for: {selector[:40]}")
            return False
        except NoSuchElementException:
            self.status_cb(f"Element not found: {selector[:40]}")
            return False
        except WebDriverException as e:
            self.status_cb(f"Click error: {str(e)[:60]}")
            return False

    def is_browser_alive(self):
        if not self._driver:
            return False
        try:
            _ = self._driver.current_url
            return True
        except Exception:
            self._driver = None
            return False

    def pick_on_existing_driver(self, status_cb, timeout=60):
        """Use the already-open browser to pick an element — no new launch."""
        if not self.is_browser_alive():
            return None
        return _run_picker_on_driver(self._driver, status_cb, timeout)

    def _find_tor_cookie(self):
        """
        Find Tor Browser's control auth cookie file.
        Tor Browser stores it at:
            <browser_dir>/TorBrowser/Data/Tor/control_auth_cookie
        where <browser_dir> is the folder containing firefox.exe.
        """
        binary = self.tor_binary_path
        if binary and os.path.isfile(binary):
            browser_dir = os.path.dirname(binary)
            cookie = os.path.join(
                browser_dir, "TorBrowser", "Data", "Tor", "control_auth_cookie"
            )
            if os.path.isfile(cookie):
                return cookie
        return None

    def _send_new_identity(self):
        """
        Request a new Tor circuit (new IP) via the Tor control port.

        Tor Browser listens on control port 9151 and authenticates via a
        cookie file.  Sending NEWNYM changes circuits without touching the
        browser window, so the Selenium session stays alive.
        """
        TOR_CONTROL_PORT = 9151

        if not STEM_AVAILABLE:
            self.status_cb("New Identity skipped — stem not installed")
            return

        try:
            cookie = self._find_tor_cookie()

            with Controller.from_port(port=TOR_CONTROL_PORT) as ctrl:
                if cookie:
                    ctrl.authenticate(cookie)
                else:
                    ctrl.authenticate()

                wait = ctrl.get_newnym_wait()
                if wait > 0:
                    self.status_cb(
                        f"New Identity — rate limit, waiting {wait:.0f}s..."
                    )
                    self._sleep_interruptible(wait)

                ctrl.signal(Signal.NEWNYM)
                self.status_cb("Building new Tor circuit...")

                # Navigate to blank page immediately — this closes all
                # persistent TCP connections to the target site so the
                # next driver.get() is forced to open a brand-new
                # connection through the new circuit (new exit node / IP).
                try:
                    self._driver.get("about:blank")
                except Exception:
                    pass

                # Give Tor time to finish building the new circuit.
                # 6 seconds is reliable; _sleep_interruptible lets STOP
                # cancel the wait immediately.
                if not self._sleep_interruptible(6):
                    return
                self.status_cb("New circuit ready — fresh IP on next request")

        except Exception as e:
            self.status_cb(f"New Identity failed: {str(e)[:70]}")

    def _check_error_page(self):
        try:
            current = self._driver.current_url
            if current.startswith("about:neterror"):
                import urllib.parse as _up
                params = _up.parse_qs(_up.urlparse(current).query)
                code = params.get("e", ["unknown"])[0]
                self.status_cb(f"Network error: {code} — is Tor running?")
                return True
            if current.startswith("about:blocked") or current.startswith("about:certerror"):
                self.status_cb(f"Page blocked/cert error — skipping loop")
                return True
        except Exception:
            pass
        return False

    def run_loop(self, url, primary_selector, secondary_selector,
                 page_load_wait, stay_after_load, loop_delay, new_identity=False,
                 proxy_enabled=False, proxy_type="SOCKS5", proxy_url=""):
        self._stop_event.clear()
        my_thread = threading.current_thread()
        self._active_thread = my_thread
        loop_count = 0
        error_msg = None

        if not self.is_browser_alive():
            _close_uncontrolled_tor_browsers(self.status_cb)
            try:
                if proxy_enabled:
                    self.status_cb("Launching browser with custom proxy...")
                else:
                    self.status_cb("Launching Tor Browser...")
                self._driver = build_driver(
                    tor_binary_path=self.tor_binary_path,
                    proxy_enabled=proxy_enabled,
                    proxy_type=proxy_type,
                    proxy_url=proxy_url,
                )
            except WebDriverException as e:
                self.status_cb(f"Driver error: {str(e)[:80]}")
                return
            except Exception as e:
                self.status_cb(f"Launch error: {str(e)[:80]}")
                return

            # Wait for Tor Browser to establish its Tor circuit before
            # attempting any navigation. Without this, driver.get(url)
            # blocks for the full page-load timeout because the Tor
            # daemon hasn't finished connecting yet.
            self.status_cb("Waiting for Tor to connect (10s)...")
            if not self._sleep_interruptible(10):
                return
        else:
            self.status_cb("Resuming — browser already open...")

        timeout_retries = 0
        while not self._stop_event.is_set():
            try:
                self.status_cb("Navigating to URL...")
                timeout_retries = 0
                self._driver.get(url)

                if self._check_error_page():
                    if not self._sleep_interruptible(page_load_wait):
                        break
                    continue

                if not self._sleep_interruptible(page_load_wait):
                    break

                if self._stop_event.is_set():
                    break

                if primary_selector and primary_selector.strip():
                    self.status_cb("Clicking primary element...")
                    self._click_selector(primary_selector)

                if self._stop_event.is_set():
                    break

                if secondary_selector and secondary_selector.strip():
                    self.status_cb("Clicking secondary element...")
                    self._click_selector(secondary_selector)

                if self._stop_event.is_set():
                    break

                self.status_cb("Staying on page...")
                if not self._sleep_interruptible(stay_after_load):
                    break

                loop_count += 1
                self.loop_cb(loop_count)

                if self._stop_event.is_set():
                    break

                if new_identity:
                    if proxy_enabled:
                        self.status_cb("IP rotation handled by proxy provider")
                    else:
                        self.status_cb("Requesting new Tor identity...")
                        self._send_new_identity()
                        if not self._sleep_interruptible(3):
                            break

                if self._stop_event.is_set():
                    break

                self.status_cb("Waiting for next loop...")
                if not self._sleep_interruptible(loop_delay):
                    break

            except TimeoutException:
                timeout_retries += 1
                self.status_cb(
                    f"Page load timed out (attempt {timeout_retries}) — "
                    "retrying in 5s..."
                )
                if not self._sleep_interruptible(5):
                    break
                continue
            except WebDriverException as e:
                error_msg = f"Browser error: {str(e)[:80]}"
                break
            except Exception as e:
                error_msg = f"Error: {str(e)[:80]}"
                break

        # Only write final status if we are still the active thread.
        # A RESTART launches a new thread which overwrites _active_thread —
        # the old thread must not clobber the new thread's status.
        if self._active_thread is my_thread:
            if error_msg:
                self.status_cb(error_msg)
            else:
                self.status_cb("Stopped — browser still open")
