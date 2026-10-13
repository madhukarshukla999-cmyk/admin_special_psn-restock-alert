from __future__ import annotations

from ..models import Item, StoreError
from .base import Store

API = "https://digistore.co.in/wp-json/wc/store/v1/products"


class DigiStore(Store):
    """digistore.co.in runs WooCommerce, which exposes the public Store API."""

    id = "digistore"
    name = "DigiStore"

    async def fetch(self, client):
        seen: dict[int, Item] = {}
        params_list = [{"category": c} for c in self.cfg.get("categories", ["playstation-card"])]
        params_list += [{"search": q} for q in self.cfg.get("search_queries", ["PSN"])]
        for params in params_list:
            r = await self.get(client, API, params={**params, "per_page": 100},
                               headers={"Accept": "application/json"})
            try:
                rows = r.json()
                assert isinstance(rows, list)
            except Exception as e:
                raise StoreError(f"DigiStore: unexpected API response: {e!r}")
            for p in rows:
                title = p.get("name") or ""
                if not self.filter.matches(title):
                    continue
                prices = p.get("prices") or {}
                unit = 10 ** int(prices.get("currency_minor_unit") or 0)
                price = int(prices.get("price") or 0) / unit
                mrp = int(prices.get("regular_price") or 0) / unit
                # Products with price 0 / not purchasable show "In Stock" but cannot be bought.
                in_stock = bool(p.get("is_in_stock")) and bool(p.get("is_purchasable")) and price > 0
                seen[p["id"]] = Item(
                    store=self.id,
                    item_id=str(p["id"]),
                    title=title,
                    url=p.get("permalink") or "https://digistore.co.in/",
                    in_stock=in_stock,
                    price=price or None,
                    mrp=mrp or None,
                )
            await self.polite_pause()
        return list(seen.values())
