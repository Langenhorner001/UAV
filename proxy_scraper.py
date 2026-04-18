import logging
import random
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

TIMEOUT = 12

# Only SOCKS5 and SOCKS4 — they support any protocol including HTTPS.
# Plain HTTP proxies almost never support CONNECT tunneling for HTTPS
# on public lists and cause ERR_TUNNEL_CONNECTION_FAILED in Chrome.
SOURCES = [
    ("socks5", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("socks5", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("socks5", "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt"),
    ("socks5", "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt"),
    ("socks4", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("socks4", "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt"),
    ("socks4", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt"),
]

# HTTPS check URL — only proxies that can tunnel HTTPS traffic will pass.
# This filters out plain HTTP proxies that fail on HTTPS sites like polymarket.com.
CHECK_URL = "https://api.ipify.org?format=json"


def _fetch_source(scheme: str, url: str) -> list:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        lines = r.text.strip().splitlines()
        proxies = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                proxies.append(f"{scheme}://{line}")
        logger.info(f"Fetched {len(proxies)} from {url}")
        return proxies
    except Exception as e:
        logger.warning(f"Source fetch failed {url}: {e}")
        return []


def _check_proxy(proxy_uri: str) -> str | None:
    try:
        # For SOCKS proxies, requests needs socks5h:// to resolve DNS via proxy.
        # socks5h:// = SOCKS5 + remote DNS (avoids local DNS leaks and failures).
        if proxy_uri.startswith("socks5://"):
            req_uri = proxy_uri.replace("socks5://", "socks5h://", 1)
        else:
            req_uri = proxy_uri
        prx = {"http": req_uri, "https": req_uri}
        r = requests.get(CHECK_URL, proxies=prx, timeout=TIMEOUT)
        if r.status_code == 200 and r.text.strip():
            return proxy_uri
    except Exception:
        pass
    return None


def scrape_and_check(
    max_check: int = 200,
    max_live: int = 30,
    workers: int = 40,
    progress_cb=None,
) -> list:
    """
    Scrape proxies from public sources, check them concurrently.

    progress_cb(scraped, checked, live) — called periodically.
    Returns list of live proxy URIs (socks5h://... or http://...).
    """
    if progress_cb:
        progress_cb(0, 0, 0, "Proxy sources se scrape kar raha hai...")

    raw = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futs = [ex.submit(_fetch_source, s, u) for s, u in SOURCES]
        for f in as_completed(futs):
            raw.extend(f.result())

    seen = set()
    unique = []
    for p in raw:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    random.shuffle(unique)   # shuffle so we don't always check same-source proxies first

    to_check = unique[:max_check]
    scraped = len(to_check)
    logger.info(f"Total unique proxies scraped: {len(unique)} — checking {scraped}")

    if progress_cb:
        progress_cb(scraped, 0, 0, f"{scraped} proxies mili, check ho rahi hain...")

    live = []
    checked = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_check_proxy, p): p for p in to_check}
        for f in as_completed(futs):
            checked += 1
            result = f.result()
            if result:
                live.append(result)
            if progress_cb and (checked % 20 == 0 or checked == scraped):
                progress_cb(scraped, checked, len(live),
                            f"Check: {checked}/{scraped} | Live: {len(live)}")
            if len(live) >= max_live:
                for remaining in futs:
                    remaining.cancel()
                break

    logger.info(f"Scrape done — live: {len(live)}/{scraped} checked")
    return live
