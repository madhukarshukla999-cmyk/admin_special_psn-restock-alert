from __future__ import annotations

import json
import re

from ..models import Item, StoreError
from .base import Store

BASE = "https://shop.rooter.gg"
# Next.js streams page data as self.__next_f.push([1,"<json string>"]) chunks.
_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


def _page_data(html: str) -> str:
    out = []
    for m in _CHUNK.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(out)


def parse_variations(html: str) -> list[dict]:
    data = _page_data(html)
    i = data.find('"baseVariations":')
    if i < 0:
        raise StoreError("Rooter: product data not found on page (layout changed?)")
    try:
        variations, _ = json.JSONDecoder().raw_decode(data, i + len('"baseVariations":'))
    except ValueError as e:
        raise StoreError(f"Rooter: couldn't read product data: {e!r}")
    return variations


class Rooter(Store):
    """shop.rooter.gg product pages.

    Each card value ("variation") has an `outOfStock` flag, and so does each seller behind it.
    Only the flags are trusted: a seller can show quantity 100 while being outOfStock=true.
    In stock = variation not out of stock AND at least one seller not out of stock.
    """

    id = "rooter"
    name = "Rooter"

    async def fetch(self, client):
        items: list[Item] = []
        for slug in self.cfg.get("products", ["playstation-gift-cards"]):
            url = f"{BASE}/product/{slug}"
            r = await self.get(client, url)
            for v in parse_variations(r.text):
                sellers = v.get("sellers") or []
                live = [s for s in sellers if s.get("outOfStock") is False]
                in_stock = v.get("outOfStock") is False and bool(live)
                prices = [s.get("sellPrice") for s in (live or sellers) if s.get("sellPrice")]
                mrps = [s.get("costPrice") for s in (live or sellers) if s.get("costPrice")]
                items.append(Item(
                    store=self.id,
                    item_id=str(v.get("id")),
                    title=f"PlayStation {v.get('name')}",
                    url=url,
                    in_stock=in_stock,
                    price=float(min(prices)) if prices else None,
                    mrp=float(max(mrps)) if mrps else None,
                ))
            await self.polite_pause()
        return items

    async def fetch_fast(self, client):
        return await self.fetch(client) if self.cfg.get("fast_check") else None

    fast_is_full_listing = True
