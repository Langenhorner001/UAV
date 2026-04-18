import asyncio
import logging
import os
import socket

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# How often (seconds) to self-ping the deployed URL
PING_INTERVAL = 1  # 1 second

# Fixed ping target
PING_URL = "https://bot-deployer-his.replit.app/api"


def _find_free_port(preferred: int = 5000) -> int:
    for port in [preferred, 5001, 4000, 3001, 6000]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


async def _handle(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def _self_ping_loop(ping_url: str):
    """Periodically ping the deployed URL to keep the bot alive."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logger.debug("Self-ping %s → %s", ping_url, resp.status)
            except Exception as e:
                logger.warning("Self-ping failed: %s", e)
            await asyncio.sleep(PING_INTERVAL)


async def start_keep_alive():
    port = int(os.environ.get("PORT", 0)) or _find_free_port()

    app = web.Application()
    app.router.add_get("/", _handle)
    app.router.add_get("/ping", _handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Keep-alive server running on port %s", port)

    # Start self-ping background task
    asyncio.create_task(_self_ping_loop(PING_URL))
    logger.info("Self-ping started every %ds → %s", PING_INTERVAL, PING_URL)
