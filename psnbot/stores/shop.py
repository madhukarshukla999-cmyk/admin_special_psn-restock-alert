"""Generic checker for small shops on Shopify or WooCommerce, configured entirely in config.yaml.

    stores:
      e2zstore:
        name: E2Z Store
        url: https://e2zstore.com
        platform: auto          # auto | shopify | woocommerce

Both platforms have public, login-free product APIs with a real stock flag, so no page scraping:
  WooCommerce  /wp-json/wc/store/v1/products?search=...   (same API as DigiStore)
  Shopify      /search/suggest.json to find the cards, then /products/<handle>.js for each one
               (per-value variants with an `available` flag)
Shopify cards found once are remembered (saved in the bot's state), so a card that the shop
hides from search while sold out is still checked and its restock is still noticed.
"""
from __future__ import annotations

import re

from ..models import Item, StoreError
from .base import Store

PLATFORMS = ("auto", "shopify", "woocommerce")
_CARD_RE = re.compile(r"\b(card|code)s?\b", re.IGNORECASE)
_JSON = {"Accept": "application/json"}


class Shop(Store):
    generic = True      # small shops: burst mode uses the gentler `risky_seconds`

    def __init__(self, sid: str, cfg: dict, title_filter):
        super().__init__(cfg, title_filter)
        self.id = sid
        self.name = self.cfg.get("name") or sid
        url = (self.cfg.get("url") or "").strip().rstrip("/")
        if not url.startswith("https://"):
            raise SystemExit(f"stores.{sid}: set url: https://... in config.yaml")
        self.base = url
        self.platform = (self.cfg.get("platform") or "auto").lower()
        if self.platform not in PLATFORMS:
            raise SystemExit(f"stores.{sid}: platform must be one of {', '.join(PLATFORMS)}")
        self.queries = self.cfg.get("search_queries") or ["playstation", "psn"]
        self.known: dict[str, str] = {}     # Shopify product handle -> title

    def _is_card(self, title: str) -> bool:
        # "PSN Card ₹1000" has no "gift card"/"wallet" word, so also accept card/code with a PSN word.
        return self.filter.matches(title) or (self.filter.matches(title, require_card_word=False)
                                              and bool(_CARD_RE.search(title)))

    async def fetch(self, client):
        if self.platform == "auto":
            self.platform = await self._detect(client)
        if self.platform == "woocommerce":
            return await self._woo(client)
        return await self._shopify(client)

    async def _detect(self, client) -> str:
        for platform, path in (("woocommerce", "/wp-json/wc/store/v1/products?per_page=1"),
                               ("shopify", "/products.json?limit=1")):
            try:
                r = await client.get(self.base + path, headers=_JSON)
                data = r.json() if r.status_code == 200 else None
            except Exception:   # noqa: BLE001 - not this platform (HTML page, timeout, ...)
                data = None
            if (platform == "woocommerce" and isinstance(data, list)) or \
               (platform == "shopify" and isinstance(data, dict) and "products" in data):
                return platform
        raise StoreError(f"{self.name}: not a Shopify or WooCommerce shop (or it blocks this server)")

    # -- WooCommerce ---------------------------------------------------------
    async def _woo(self, client) -> list[Item]:
        seen: dict[int, Item] = {}
        for q in self.queries:
            r = await self.get(client, self.base + "/wp-json/wc/store/v1/products",
                               params={"search": q, "per_page": 100}, headers=_JSON)
            try:
                rows = r.json()
                assert isinstance(rows, list)
            except Exception as e:
                raise StoreError(f"{self.name}: unexpected API response: {e!r}")
            for p in rows:
                title = p.get("name") or ""
                if p.get("id") in seen or not self._is_card(title):
                    continue
                prices = p.get("prices") or {}
                unit = 10 ** int(prices.get("currency_minor_unit") or 0)
                price = int(prices.get("price") or 0) / unit
                mrp = int(prices.get("regular_price") or 0) / unit
                seen[p["id"]] = Item(
                    store=self.id, item_id=str(p["id"]), title=title,
                    url=p.get("permalink") or self.base + "/",
                    in_stock=bool(p.get("is_in_stock")) and bool(p.get("is_purchasable")) and price > 0,
                    price=price or None, mrp=mrp or None)
            await self.polite_pause()
        return list(seen.values())

    # -- Shopify -------------------------------------------------------------
    async def _shopify(self, client) -> list[Item]:
        for q in self.queries:
            r = await self.get(client, self.base + "/search/suggest.json", headers=_JSON, params={
                "q": q, "resources[type]": "product", "resources[limit]": 10,
                "resources[options][unavailable_products]": "show"})
            try:
                found = r.json()["resources"]["results"]["products"]
            except Exception as e:
                raise StoreError(f"{self.name}: unexpected search response: {e!r}")
            for p in found:
                if p.get("handle") and self._is_card(p.get("title") or ""):
                    self.known.setdefault(p["handle"], p.get("title") or "")
        for h in self.cfg.get("product_handles") or []:     # extra cards the search doesn't find
            self.known.setdefault(h, h)

        items: list[Item] = []
        for handle in list(self.known):
            r = await client.get(f"{self.base}/products/{handle}.js", headers=_JSON)
            if r.status_code == 404:                         # listing deleted
                self.known.pop(handle, None)
                continue
            if r.status_code != 200:
                raise StoreError(f"{self.name}: HTTP {r.status_code} for {handle}")
            p = r.json()
            title = p.get("title") or handle
            variants = p.get("variants") or []
            for v in variants:
                vt = v.get("title") or ""
                full = title if vt.lower() in ("", "default title") or vt in title else f"{title} – {vt}"
                price = (v.get("price") or 0) / 100
                mrp = (v.get("compare_at_price") or 0) / 100
                items.append(Item(
                    store=self.id, item_id=str(v["id"]), title=full,
                    url=f"{self.base}/products/{handle}" + (f"?variant={v['id']}" if len(variants) > 1 else ""),
                    in_stock=bool(v.get("available")) and price > 0,
                    price=price or None, mrp=mrp or None))
            await self.polite_pause(0.3, 0.8)
        return items
