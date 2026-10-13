from __future__ import annotations

import asyncio
import json
import re

from ..models import Item, StoreError
from .base import Store

_PRODUCT_LINK = re.compile(r'/([a-z0-9-]+)/p/(itm[0-9a-z]{13})')
_LD_JSON = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class Flipkart(Store):
    """Flipkart lists each gift card value as its own product page.

    1. Discovery: run search queries and collect product links whose URL slug looks like a
       PlayStation gift card (plus any `product_urls` you add in config).
    2. Check: open each product page and read schema.org `offers.availability`
       (InStock / OutOfStock) and price. Out-of-stock pages also show "Notify Me".
    """

    id = "flipkart"
    name = "Flipkart"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self.known: dict[str, str] = {}   # itm id -> url (filled by the runner from saved state)
        self._cycle = 0

    async def _discover(self, client) -> None:
        for q in self.cfg.get("search_queries", []):
            try:
                r = await self.get(client, "https://www.flipkart.com/search", params={"q": q})
            except StoreError:
                continue  # discovery is best-effort; known products are still checked
            for slug, itm in _PRODUCT_LINK.findall(r.text):
                if itm not in self.known and self.filter.matches(slug):
                    self.known[itm] = f"https://www.flipkart.com/{slug}/p/{itm}"
            await self.polite_pause()

    async def fetch(self, client):
        for u in self.cfg.get("product_urls", []):
            m = _PRODUCT_LINK.search(u)
            if m:
                self.known.setdefault(m.group(2), f"https://www.flipkart.com/{m.group(1)}/p/{m.group(2)}")

        every = max(1, int(self.cfg.get("discover_every_cycles", 10)))
        if self._cycle % every == 0 or not self.known:
            await self._discover(client)
        self._cycle += 1

        if not self.known:
            raise StoreError("Flipkart: no PlayStation gift card listings found")

        sem = asyncio.Semaphore(int(self.cfg.get("concurrency", 3)))
        failures: list[str] = []

        async def check(itm: str, url: str) -> Item | None:
            async with sem:
                try:
                    r = await self.get(client, url)
                except StoreError as e:
                    failures.append(str(e))
                    # Unknown this round: keeps the previous state (avoids false restock alerts)
                    return Item(store=self.id, item_id=itm, title=itm, url=url, in_stock=None)
                finally:
                    await self.polite_pause(0.5, 1.5)
            return self._parse(itm, url, r.text)

        targets = list(self.known.items())
        results = await asyncio.gather(*(check(i, u) for i, u in targets))
        items = [x for x in results if x]
        if failures and len(failures) == len(targets):
            raise StoreError(failures[0])
        return items

    def _parse(self, itm: str, url: str, html: str) -> Item | None:
        title, price, availability = None, None, None
        for block in _LD_JSON.findall(html):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for obj in data if isinstance(data, list) else [data]:
                if isinstance(obj, dict) and isinstance(obj.get("offers"), dict):
                    title = obj.get("name")
                    price = obj["offers"].get("price")
                    availability = obj["offers"].get("availability") or ""
                    break
            if title:
                break

        if title and not self.filter.matches(title):
            self.known.pop(itm, None)   # not a PSN card (e.g. memory card); stop checking it
            return None

        if availability:
            in_stock = availability.rstrip("/").endswith("InStock")
        elif "Notify Me" in html or "Sold Out" in html or "Currently unavailable" in html:
            in_stock = False
        elif "Add to cart" in html or "Buy Now" in html:
            in_stock = True
        else:
            in_stock = None

        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        return Item(
            store=self.id,
            item_id=itm,
            title=title or url.split("/")[3].replace("-", " ").title(),
            url=url,
            in_stock=in_stock,
            price=price,
        )
