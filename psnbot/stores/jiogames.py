from __future__ import annotations

import asyncio
import base64
import re

from ..models import Item, StoreError
from .base import Store

BASE = "https://store.jiogames.com"
API = f"{BASE}/api/service/application"
CATALOG = f"{API}/catalog/v1.0"
# Public application credentials embedded in the store's own HTML (Fynd/JioCommerce platform).
_APP_ID = re.compile(r"applications/([0-9a-f]{24})/")
_TOKEN = re.compile(r'"token":"([A-Za-z0-9_\-]+)","tokens"')


class JioGames(Store):
    """store.jiogames.com

    PlayStation cards live in the "playstation" collection (store.jiogames.com/collection/playstation).
    When they sell out JioGames hides them completely (not in search, the collection or the
    sitemap). Search is checked too, in case cards get listed outside the collection.

    Sometimes a card stays listed as "sellable" after it has run out, and Buy Now then says
    "Product is out of stock". So each listed card is double-checked the way Buy Now does it
    (live price/stock + a guest buy-now cart); only cards that pass count as in stock.
    Turn off with `verify_stock: false`.
    """

    id = "jiogames"
    name = "JioGames Store"
    # The fast check returns the whole collection, so a card missing from it = sold out.
    fast_is_full_listing = True

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self._auth: str | None = None

    async def _headers(self, client) -> dict:
        if not self._auth:
            r = await self.get(client, f"{BASE}/")
            app, tok = _APP_ID.search(r.text), _TOKEN.search(r.text)
            if not (app and tok):
                raise StoreError("JioGames: could not find store API token (layout changed?)")
            self._auth = "Bearer " + base64.b64encode(f"{app.group(1)}:{tok.group(1)}".encode()).decode()
        return {"authorization": self._auth, "x-currency-code": "INR", "Accept": "application/json"}

    async def _api(self, client, path: str, params: dict) -> list[dict]:
        try:
            r = await self.get(client, f"{CATALOG}/{path}", params=params, headers=await self._headers(client))
        except StoreError:
            self._auth = None   # token may have rotated; refresh next time
            raise
        try:
            return r.json().get("items") or []
        except ValueError as e:
            raise StoreError(f"JioGames: unexpected API response: {e!r}")

    def _item(self, p: dict) -> Item:
        price = p.get("price") or {}
        slug = p.get("slug")
        uid = str(p.get("uid") or slug)
        return Item(
            store=self.id,
            item_id=uid,
            title=p.get("name") or slug,
            url=f"{BASE}/product/{slug}",
            in_stock=bool(p.get("sellable")),
            price=(price.get("effective") or {}).get("min"),
            mrp=(price.get("marked") or {}).get("min"),
        )

    # -- real stock check ---------------------------------------------------
    # The listing's "sellable" flag is cached and can stay true after the cards run out; the
    # site only finds out when you press Buy Now ("Product is out of stock"). So for every card
    # the listing calls sellable we do what Buy Now does: get the live price/stock of the size,
    # then add it to a guest "buy now" cart (anonymous, nothing is ordered).
    async def _verify(self, client, it: Item, p: dict) -> None:
        if not it.in_stock or not self.cfg.get("verify_stock", True):
            return
        try:
            it.in_stock = await self._buyable(client, p)
        except StoreError:
            it.in_stock = None          # couldn't tell this round: keep the previous state
        if it.in_stock is False:
            it.note = "listed, but Buy Now says out of stock"

    async def _buyable(self, client, p: dict) -> bool | None:
        H = await self._headers(client)
        sizes = p.get("sizes") or ["OS"]
        for size in sizes:
            try:
                r = await client.get(f"{API}/catalog/v3.0/products/{p['slug']}/sizes/{size}/price/", headers=H)
            except Exception as e:  # noqa: BLE001
                raise StoreError(f"JioGames: price check failed: {e!r}")
            if r.status_code in (401, 403):
                self._auth = None
                raise StoreError(f"JioGames: price check HTTP {r.status_code}")
            if r.status_code == 400 and re.search(r"serviceable|out of stock|not available", r.text, re.I):
                continue                        # no seller has it in stock
            if r.status_code >= 400:
                raise StoreError(f"JioGames: price check HTTP {r.status_code}")
            pr = r.json()
            if not pr.get("quantity") or pr.get("is_serviceable") is False:
                continue
            body = {"items": [{"item_id": p.get("uid"), "item_size": size, "quantity": 1,
                               "article_id": pr.get("article_id"), "pos": False,
                               "store_id": (pr.get("store") or {}).get("uid"),
                               "seller_id": (pr.get("seller") or {}).get("uid"),
                               "article_assignment": pr.get("article_assignment")}]}
            try:
                r = await client.post(f"{API}/cart/v1.0/detail", params={"buy_now": "true"}, headers=H, json=body)
            except Exception as e:  # noqa: BLE001
                raise StoreError(f"JioGames: cart check failed: {e!r}")
            if r.status_code in (401, 403):
                self._auth = None
            if r.status_code >= 500 or r.status_code in (401, 403, 429):
                raise StoreError(f"JioGames: cart check HTTP {r.status_code}")
            try:
                j = r.json()
            except ValueError:
                raise StoreError("JioGames: cart check returned non-JSON")
            msg = (j.get("message") or "").lower()
            if j.get("success"):
                return True
            if "out of stock" in msg or "not available" in msg or "sold out" in msg:
                continue
            if "maximum quantity" in msg or "limit" in msg:   # already in this guest cart = buyable
                return True
            raise StoreError(f"JioGames: cart check said {j.get('message')!r}")
        return False

    def _excluded(self, title: str) -> bool:
        t = title.lower()
        return any(w in t for w in self.filter.exclude)

    async def _collections(self, client) -> dict[str, Item]:
        seen: dict[str, Item] = {}
        for col in self.cfg.get("collections", ["playstation"]):
            rows = await self._api(client, f"collections/{col}/items/",
                                   {"page_size": 100, "page_type": "cursor", "page_id": "*"})
            for p in rows:
                # Collection is PlayStation-only, so names like "Rs1000 Wallet Code" count too;
                # only the exclude list (e.g. PS Plus) applies.
                if not self._excluded(p.get("name") or ""):
                    it = self._item(p)
                    seen[it.item_id] = (it, p)
        return seen

    async def _verified(self, client, seen: dict) -> list[Item]:
        # only card values that are tracked (config only_values): no price/cart requests for the rest
        keep = [(it, p) for it, p in seen.values() if self.wanted(it)]
        await asyncio.gather(*(self._verify(client, it, p) for it, p in keep))
        return [it for it, _ in keep]

    async def fetch(self, client):
        seen = await self._collections(client)
        gap = self.between()
        for q in self.cfg.get("search_queries", ["playstation", "psn"]):
            await gap()
            for p in await self._api(client, "products/", {"q": q, "page_size": 50, "page_id": "*"}):
                if self.filter.matches(p.get("name") or "", require_card_word=False):
                    it = self._item(p)
                    seen.setdefault(it.item_id, (it, p))
        return await self._verified(client, seen)

    async def fetch_fast(self, client):
        if not self.cfg.get("fast_check", False):
            return None
        return await self._verified(client, await self._collections(client))
