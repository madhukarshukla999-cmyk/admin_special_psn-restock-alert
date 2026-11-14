from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from ..models import Item, StoreError
from .base import Store

_PRODUCT_LINK = re.compile(r"/([a-z0-9-]+)/p/(itm[0-9a-z]{13})")
_PRICE = re.compile(r"₹\s*([\d,]+(?:\.\d+)?)")
# Labels Flipkart puts on a search-result tile when the item can't be bought.
# Product pages are NOT reliable: many show "Buy now" but fail with
# "item is not available to purchase". The search tile label matches reality.
UNAVAILABLE_LABELS = ("coming soon", "currently unavailable", "sold out", "out of stock",
                      "temporarily unavailable", "notify me")


class Flipkart(Store):
    """Reads Flipkart search results.

    Every PlayStation gift card tile shows "Coming Soon" or "Currently unavailable"
    when it can't be bought. A tile with neither label is in stock.
    """

    id = "flipkart"
    name = "Flipkart"

    async def fetch(self, client):
        queries = self.cfg.get("search_queries") or ["playstation rs gift card"]
        pages = int(self.cfg.get("max_pages", 1))
        found: dict[str, Item] = {}
        failures = 0
        gap = self.between()
        for q in queries:
            for page in range(1, pages + 1):
                await gap()
                try:
                    r = await self.get(client, "https://www.flipkart.com/search",
                                       params={"q": q, "page": page})
                except StoreError:
                    failures += 1
                    break
                tiles = self._parse_tiles(r.text)
                for it in tiles:
                    prev = found.get(it.item_id)
                    # If two searches disagree, trust "unavailable" (avoids false alerts).
                    if prev is None or (prev.in_stock and not it.in_stock):
                        found[it.item_id] = it
                if not tiles:
                    break
        if not found:
            raise StoreError("Flipkart: search returned no PlayStation gift cards"
                             + (" (requests failed)" if failures else " (layout changed?)"))
        return list(found.values())

    def _parse_tiles(self, html: str) -> list[Item]:
        doc = LexborHTMLParser(html)
        tiles: dict[str, object] = {}
        for a in doc.css('a[href*="/p/itm"]'):
            m = _PRODUCT_LINK.search(a.attributes.get("href") or "")
            if not m or m.group(2) in tiles:
                continue
            # Walk up to the largest element that still contains only this product.
            node = a
            while node.parent is not None:
                ids = set(re.findall(r"/p/(itm[0-9a-z]{13})", node.parent.html or ""))
                if len(ids) > 1:
                    break
                node = node.parent
            tiles[m.group(2)] = (m.group(1), node)

        items = []
        for itm, (slug, node) in tiles.items():
            title = ""
            for el in node.css("a[title], [title]"):
                title = el.attributes.get("title") or ""
                if title:
                    break
            if not title:
                img = node.css_first("img[alt]")
                title = (img.attributes.get("alt") if img else "") or slug.replace("-", " ")
            title = " ".join(title.split())
            if not self.filter.matches(title):
                continue
            text = " ".join(node.text(separator=" ", strip=True).split())
            low = text.lower()
            label = next((lbl for lbl in UNAVAILABLE_LABELS if lbl in low), None)
            pm = _PRICE.search(text)
            items.append(Item(
                store=self.id,
                item_id=itm,
                title=title,
                url=f"https://www.flipkart.com/{slug}/p/{itm}",
                in_stock=label is None,
                price=float(pm.group(1).replace(",", "")) if pm else None,
                note="" if label is None else label.title(),
            ))
        return items
