from __future__ import annotations

import asyncio
import random

import httpx

from ..models import Item, StoreError, TitleFilter


class Store:
    """Base class for a store checker. Subclasses implement `fetch`."""

    id: str = ""
    name: str = ""

    def __init__(self, cfg: dict, title_filter: TitleFilter):
        self.cfg = cfg or {}
        self.filter = title_filter

    async def fetch(self, client: httpx.AsyncClient) -> list[Item]:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    async def get(self, client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
        return await self._request(client, "GET", url, **kw)

    async def post(self, client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
        return await self._request(client, "POST", url, **kw)

    async def _request(self, client, method, url, retries: int = 2, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = await client.request(method, url, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(2 + attempt * 3 + random.random())
                    continue
                if r.status_code >= 400:
                    raise StoreError(f"{self.name}: HTTP {r.status_code} for {url}")
                return r
            except httpx.HTTPError as e:
                last = e
                if attempt < retries:
                    await asyncio.sleep(2 + attempt * 3 + random.random())
        raise StoreError(f"{self.name}: request failed for {url}: {last!r}")

    @staticmethod
    async def polite_pause(lo: float = 0.8, hi: float = 2.0) -> None:
        await asyncio.sleep(random.uniform(lo, hi))
