from __future__ import annotations

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-IN,en;q=0.9",
}


def make_client(timeout: float = 30.0, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy,
        headers=DEFAULT_HEADERS,
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
