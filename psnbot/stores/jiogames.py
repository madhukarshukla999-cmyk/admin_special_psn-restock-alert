from __future__ import annotations

import base64
import re

from ..models import Item, StoreError
from .base import Store

BASE = "https://store.jiogames.com"
CATALOG = f"{BASE}/api/service/application/catalog/v1.0"
# Public application credentials embedded in the store's own HTML (Fynd/JioCommerce platform).
_APP_ID = re.compile(r"applications/([0-9a-f]{24})/")
_TOKEN = re.compile(r'"token":"([A-Za-z0-9_\-]+)","tokens"')


class JioGames(Store):
    """store.jiogames.com

    PlayStation cards live in the "playstation" collection (store.jiogames.com/collection/playstation).
    When they sell out JioGames hides them completely (not in search, the collection or the
    sitemap), so "appears in the collection and is sellable" = in stock. Search is checked too,
    in case cards get listed outside the collection.
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
                    seen[it.item_id] = it
        return seen

    async def fetch(self, client):
        seen = await self._collections(client)
        for q in self.cfg.get("search_queries", ["playstation", "psn"]):
            await self.polite_pause()
            for p in await self._api(client, "products/", {"q": q, "page_size": 50, "page_id": "*"}):
                if self.filter.matches(p.get("name") or "", require_card_word=False):
                    it = self._item(p)
                    seen.setdefault(it.item_id, it)
        return list(seen.values())

    async def fetch_fast(self, client):
        if not self.cfg.get("fast_check", False):
            return None
        return list((await self._collections(client)).values())
