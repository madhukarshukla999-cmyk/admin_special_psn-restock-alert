from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..models import Item, StoreError
from .base import Store

BASE = "https://offers.myhubble.money"
# The website's own data call for a category page (TanStack server function).
_FN = "src_server_data_category_route_data_ts--getCategoryRouteData_1"
_FN_NAME = "/vercel/path0/hubble-app/src/server/data/category_route_data.ts?tsr-directive-use-server="
_PRODUCT = re.compile(
    r'type:"VOUCHER",voucherProduct:\$R\[\d+\]=\{title:"(?P<title>[^"]*)".*?'
    r'discountPercentage:(?P<disc>[\d.]+).*?status:"(?P<status>[A-Z_]+)"\},brandKey:"(?P<key>[^"]+)"',
    re.S)


def _url(category: str) -> str:
    args = json.dumps({"t": {"t": 9, "i": 0, "a": [{"t": 1, "s": category}], "o": 0}, "f": 31, "m": []},
                      separators=(",", ":"))
    return f"{BASE}/_server/?id={_FN}&name={quote(_FN_NAME, safe='')}&args={quote(args, safe='')}"


def parse_products(text: str) -> list[dict]:
    return [m.groupdict() for m in _PRODUCT.finditer(text)]


class Hubble(Store):
    """offers.myhubble.money

    PlayStation only appears in Hubble's catalogue while it's in stock, so the bot watches the
    Gaming category: a PlayStation/PSN brand that's listed and ACTIVE = in stock.
    """

    id = "hubble"
    name = "Hubble"
    fast_is_full_listing = True      # gone from the list = sold out

    async def fetch(self, client):
        headers = {"Accept": "*/*", "Referer": f"{BASE}/shop-by-category/gaming-gift-cards",
                   "x-server-id": quote(f"{_FN}#{_FN_NAME}", safe=""), "x-server-instance": "server-fn:0"}
        seen: dict[str, Item] = {}
        gap = self.between()
        for cat in self.cfg.get("categories", ["GAMING"]):
            await gap()
            r = await self.get(client, _url(cat), headers=headers)
            products = parse_products(r.text)
            if not products:
                raise StoreError(f"Hubble: no brands found in category {cat} (site changed?)")
            for p in products:
                title = " ".join(p["title"].split())
                if not self.filter.matches(f"{title} {p['key']}", require_card_word=False):
                    continue
                disc = float(p["disc"] or 0)
                seen[p["key"]] = Item(
                    store=self.id, item_id=p["key"], title=f"{title} gift card",
                    url=f"{BASE}/buy-gift-card/{p['key']}",
                    in_stock=p["status"] == "ACTIVE",
                    note=f"{disc:g}% off" if disc else "",
                )
        return list(seen.values())

    async def fetch_fast(self, client):
        return await self.fetch(client) if self.cfg.get("fast_check") else None
