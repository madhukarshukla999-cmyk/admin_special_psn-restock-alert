from __future__ import annotations

import base64
import re

from ..models import Item, StoreError
from .base import Store

BASE = "https://store.jiogames.com"
API = f"{BASE}/api/service/application/catalog/v1.0/products/"
# Public application credentials embedded in the store's own HTML (Fynd/JioCommerce platform).
_APP_ID = re.compile(r"applications/([0-9a-f]{24})/")
_TOKEN = re.compile(r'"token":"([A-Za-z0-9_\-]+)","tokens"')


class JioGames(Store):
    """store.jiogames.com catalog search. At the time of writing JioGames does not list
    PlayStation cards at all, so this mainly alerts you if/when they start selling them."""

    id = "jiogames"
    name = "JioGames Store"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self._auth: str | None = None

    async def _get_auth(self, client) -> str:
        if self._auth:
            return self._auth
        r = await self.get(client, f"{BASE}/")
        app, tok = _APP_ID.search(r.text), _TOKEN.search(r.text)
        if not (app and tok):
            raise StoreError("JioGames: could not find store API token (layout changed?)")
        self._auth = "Bearer " + base64.b64encode(f"{app.group(1)}:{tok.group(1)}".encode()).decode()
        return self._auth

    async def fetch(self, client):
        seen: dict[str, Item] = {}
        for q in self.cfg.get("search_queries", ["playstation", "psn"]):
            auth = await self._get_auth(client)
            try:
                r = await self.get(client, API, params={"q": q, "page_size": 50, "page_id": "*"},
                                   headers={"authorization": auth, "x-currency-code": "INR",
                                            "Accept": "application/json"})
            except StoreError:
                self._auth = None   # token may have rotated; refresh next time
                raise
            for p in r.json().get("items", []):
                title = p.get("name") or ""
                if not self.filter.matches(title, require_card_word=False):
                    continue
                price = (p.get("price") or {})
                slug = p.get("slug")
                seen[str(p.get("uid") or slug)] = Item(
                    store=self.id,
                    item_id=str(p.get("uid") or slug),
                    title=title,
                    url=f"{BASE}/product/{slug}",
                    in_stock=bool(p.get("sellable")),
                    price=(price.get("effective") or {}).get("min"),
                    mrp=(price.get("marked") or {}).get("min"),
                )
            await self.polite_pause()
        return list(seen.values())
