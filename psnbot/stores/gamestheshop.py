from __future__ import annotations

import asyncio
import logging
import time

from ..models import Item, StoreError
from .base import Store

log = logging.getLogger(__name__)

API = "https://green-api.gamestheshop.com/storefront/products/filter"
DETAIL = "https://green-api.gamestheshop.com/storefront/products/{}/"
HEADERS = {"Origin": "https://www.gamestheshop.com", "Referer": "https://www.gamestheshop.com/",
           "Accept": "application/json, text/plain, */*"}


class GamesTheShop(Store):
    """Uses the storefront search API the website itself calls.

    The search list updates minutes late when a card sells out, while the product page reads live
    stock. So every card the list calls "In Stock" is double-checked with the product page's own
    request (storefront/products/<id>/, the selected variant's available_stock), within a request
    budget that stays under the shop's limit of 100 requests a minute.
    """

    id = "gamestheshop"
    name = "GamesTheShop"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self._live: dict[str, tuple[float, bool | None]] = {}   # variant -> (checked at, live in stock)
        self._listed: set[str] = set()      # variants the list called In Stock on the last check

    async def _live_stock(self, client, product_id: str, variant_id: str) -> bool | None:
        """Exactly what the product page shows: False = 'Currently Out of Stock'. None = couldn't tell."""
        try:
            r = await client.get(DETAIL.format(product_id), headers=HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            d = r.json().get("data") or {}
        except Exception:  # noqa: BLE001 - keep what the list says this round
            return None
        vs = d.get("variants") or []
        v = next((x for x in vs if x.get("id") == variant_id), vs[0] if vs else None)
        avail = ((v or {}).get("inventory") or {}).get("available_stock")
        if avail is not None:
            return avail > 0
        return "out of stock" not in (d.get("stock_status") or "").lower()

    async def _verify(self, client, items: list[Item]) -> None:
        # (only card values that are tracked: no live-stock requests for the others)
        listed = [i for i in items if i.in_stock and i.extra.get("product_id") and self.wanted(i)]
        for vid in [k for k in self._live if k not in {i.item_id for i in listed}]:
            self._live.pop(vid)                    # the list says sold out too: nothing to correct
        if not listed or not self.cfg.get("verify_stock", True):
            return
        budget = float(self.cfg.get("verify_per_minute", 50))   # + the list itself, under 100/min
        gap = max(float(self.cfg.get("verify_min_seconds", 3)), len(listed) * 60 / budget)
        now = time.time()
        due = [i for i in listed if now - self._live.get(i.item_id, (0, None))[0] >= gap]
        results = await asyncio.gather(*(self._live_stock(client, i.extra["product_id"], i.item_id) for i in due))
        for it, live in zip(due, results):
            if live is not None:
                self._live[it.item_id] = (now, live)
        for it in listed:
            live = self._live.get(it.item_id, (0, None))[1]
            if live is False:                      # page says "Currently Out of Stock"
                it.in_stock = False
                it.note = ""

    async def fetch(self, client):
        headers = {
            "Origin": "https://www.gamestheshop.com",
            "Referer": "https://www.gamestheshop.com/",
            "Accept": "application/json, text/plain, */*",
        }
        seen: dict[str, Item] = {}
        for query in self.cfg.get("search_queries", ["playstation store gift card"]):
            for page in range(1, int(self.cfg.get("max_pages", 3)) + 1):
                r = await self.post(client, f"{API}?page={page}&limit=50",
                                    json={"searchQuery": query}, headers=headers)
                try:
                    data = r.json()
                    rows, meta = data["data"], data.get("meta") or {}
                except (ValueError, KeyError) as e:
                    raise StoreError(f"GamesTheShop: unexpected API response: {e!r}")
                for p in rows:
                    title = p.get("name") or ""
                    if not self.filter.matches(title):
                        continue
                    inv = p.get("inventory") or {}
                    status = (p.get("stock_status") or "").strip().lower()
                    avail = inv.get("available_stock")
                    # Seen values: "In Stock" (stock > 0) and "Out of Stock" (stock 0).
                    # Require both signals to agree before calling it in stock.
                    if "out" in status or (avail is not None and avail <= 0):
                        in_stock = False
                    elif status.replace(" ", "") == "instock":
                        in_stock = True
                    else:
                        in_stock = None
                    limit = inv.get("buy_limit_count")
                    notes = []
                    if in_stock and avail and avail < 10:
                        notes.append(f"only {avail} left")
                    if in_stock and limit:
                        notes.append(f"max {limit} per order")
                    vid = p.get("id")
                    seen[vid] = Item(
                        store=self.id,
                        item_id=vid,
                        title=" ".join(title.split()),
                        url=f"https://www.gamestheshop.com/product/{p.get('product_id')}?variant={vid}",
                        in_stock=in_stock,
                        price=p.get("sale_price"),
                        mrp=p.get("regular_price"),
                        note=", ".join(notes),
                        extra={"product_id": p.get("product_id")},
                    )
                if page >= int(meta.get("totalPages") or 1):
                    break
                await self.polite_pause()
        items = list(seen.values())
        await self._start_buying(client, items)      # before the slower double-check below
        await self._verify(client, items)
        return items

    async def _start_buying(self, client, items: list[Item]) -> None:
        """Cards the search list has just started calling "In Stock": tell the bot to buy now.
        PSN cards here can sell out in seconds, and the live double-check costs another round
        trip. The order itself is the real check — GamesTheShop refuses it if the card is gone."""
        listed = {i.item_id for i in items if i.in_stock and i.extra.get("product_id")}
        fresh = [i for i in items if i.item_id in listed and i.item_id not in self._listed
                 and self.wanted(i)]
        self._listed = listed
        if fresh and self.on_early_buy:
            try:
                await self.on_early_buy(client, fresh)
            except Exception:  # noqa: BLE001 - buying must never break the stock check
                log.exception("GamesTheShop: early buy failed")
