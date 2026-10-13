from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from ..models import Item, StoreError
from .base import Store

_BLOCK_MARKERS = ("validateCaptcha", "Type the characters you see", "api-services-support@amazon.com")
_PRICE = re.compile(r"[\d,]+(?:\.\d+)?")


def _blocked(r) -> bool:
    return r.status_code == 503 or any(m in r.text for m in _BLOCK_MARKERS)


class Amazon(Store):
    """Amazon.in search + product pages.

    NOTE: Amazon often blocks requests from cloud/datacenter IPs (HTTP 503 / captcha).
    When that happens the store is reported as an error, never as "out of stock".
    """

    id = "amazon"
    name = "Amazon"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self.known: dict[str, str] = {}   # ASIN -> url
        self._cycle = 0

    async def _get_page(self, client, url, **kw):
        r = await client.get(url, **kw)
        if _blocked(r):
            raise StoreError("Amazon: blocked by anti-bot (503/captcha) — try a different server IP")
        if r.status_code >= 400:
            raise StoreError(f"Amazon: HTTP {r.status_code} for {url}")
        return r

    async def _discover(self, client):
        for q in self.cfg.get("search_queries", []):
            r = await self._get_page(client, "https://www.amazon.in/s", params={"k": q})
            doc = LexborHTMLParser(r.text)
            for node in doc.css('div[data-component-type="s-search-result"][data-asin]'):
                asin = node.attributes.get("data-asin")
                h2 = node.css_first("h2")
                title = h2.text(strip=True) if h2 else ""
                if asin and self.filter.matches(title):
                    self.known.setdefault(asin, f"https://www.amazon.in/dp/{asin}")
            await self.polite_pause(1.5, 3.0)

    async def fetch(self, client):
        for asin in self.cfg.get("asins", []):
            self.known.setdefault(asin, f"https://www.amazon.in/dp/{asin}")

        every = max(1, int(self.cfg.get("discover_every_cycles", 10)))
        if self._cycle % every == 0 or not self.known:
            await self._discover(client)
        self._cycle += 1
        if not self.known:
            return []

        items: list[Item] = []
        for asin, url in list(self.known.items()):   # sequential: Amazon is touchy
            r = await self._get_page(client, url)
            item = self._parse(asin, url, r.text)
            if item:
                items.append(item)
            await self.polite_pause(1.5, 3.0)
        return items

    def _parse(self, asin: str, url: str, html: str) -> Item | None:
        doc = LexborHTMLParser(html)
        t = doc.css_first("#productTitle")
        title = t.text(strip=True) if t else ""
        if not title:
            raise StoreError(f"Amazon: could not read product page {url} (layout changed or blocked)")
        if not self.filter.matches(title):
            self.known.pop(asin, None)
            return None

        avail_node = doc.css_first("#availability")
        avail = avail_node.text(strip=True).lower() if avail_node else ""
        has_buy = bool(doc.css_first("#add-to-cart-button") or doc.css_first("#buy-now-button"))
        if "currently unavailable" in avail or "out of stock" in avail:
            in_stock = False
        elif has_buy:
            in_stock = True
        elif doc.css_first("#outOfStock") or doc.css_first("#gc-buy-box-text"):
            in_stock = False
        else:
            in_stock = None

        price = None
        p = doc.css_first("#corePrice_feature_div .a-offscreen") or doc.css_first(".a-price .a-offscreen")
        if p:
            m = _PRICE.search(p.text())
            if m:
                price = float(m.group(0).replace(",", ""))
        return Item(store=self.id, item_id=asin, title=title, url=url, in_stock=in_stock, price=price)
