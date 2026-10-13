from __future__ import annotations

from ..models import Item, StoreError
from .base import Store

API = "https://green-api.gamestheshop.com/storefront/products/filter"


class GamesTheShop(Store):
    """Uses the storefront search API the website itself calls."""

    id = "gamestheshop"
    name = "GamesTheShop"

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
                    if "out" in status:
                        in_stock = False
                    elif status.replace(" ", "") == "instock" or (avail is not None and avail > 0):
                        in_stock = True
                    else:
                        in_stock = None
                    vid = p.get("id")
                    seen[vid] = Item(
                        store=self.id,
                        item_id=vid,
                        title=" ".join(title.split()),
                        url=f"https://www.gamestheshop.com/product/{p.get('product_id')}?variant={vid}",
                        in_stock=in_stock,
                        price=p.get("sale_price"),
                        mrp=p.get("regular_price"),
                        note=f"only {avail} left" if in_stock and avail and avail < 10 else "",
                    )
                if page >= int(meta.get("totalPages") or 1):
                    break
                await self.polite_pause()
        return list(seen.values())
