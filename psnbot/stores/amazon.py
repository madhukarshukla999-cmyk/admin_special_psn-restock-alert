from __future__ import annotations

import re
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser

from ..models import Item, StoreError
from .base import Store

# Amazon blocks its desktop product page (/dp/...) and search (/s) for cloud server IPs,
# but the mobile product page (/gp/aw/d/...) still loads and shows the same stock info.
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")
_HEADERS = {"User-Agent": MOBILE_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9"}
_BLOCK_MARKERS = ("validateCaptcha", "Type the characters you see")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})(?:[/?#]|$)")
_ASIN = re.compile(r"^[A-Z0-9]{10}$")
_PRICE = re.compile(r"[\d,]+(?:\.\d+)?")
# Price must come from the buy box only; other ".a-price" elements belong to other products.
_PRICE_SELECTORS = (
    "#corePriceDisplay_mobile_feature_div .priceToPay",
    "#corePriceDisplay_desktop_feature_div .priceToPay",
    "#corePrice_feature_div .a-offscreen",
)


def _blocked(r) -> bool:
    return r.status_code == 503 or any(m in r.text for m in _BLOCK_MARKERS)


def dp_url(asin: str) -> str:
    return f"https://www.amazon.in/dp/{asin}"


class Amazon(Store):
    """Amazon.in product pages, checked via the mobile page.

    Search is blocked from cloud IPs, so listings come from `product_urls` / `asins` in config.
    Any link format works, including amzn.in share links. `fast_check` listings are also
    checked on the faster interval (see `fast_interval_seconds`).
    """

    id = "amazon"
    name = "Amazon"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self.known: dict[str, str] = {}   # ASIN -> url shown in alerts
        self._resolved: dict[str, str | None] = {}   # config entry -> ASIN
        self._cycle = 0

    # -- config entries -> ASINs -----------------------------------------
    async def _to_asin(self, client, entry: str) -> str | None:
        entry = entry.strip()
        if entry in self._resolved:
            return self._resolved[entry]
        asin = None
        if _ASIN.match(entry):
            asin = entry
        elif m := _ASIN_IN_URL.search(urlparse(entry).path + "/"):
            asin = m.group(1)
        elif "amzn." in entry:   # share link (amzn.in/d/xxxx) -> follow one redirect
            try:
                r = await client.get(entry, headers=_HEADERS, follow_redirects=False)
                loc = r.headers.get("location", "")
                if m := _ASIN_IN_URL.search(urlparse(loc).path + "/"):
                    asin = m.group(1)
            except Exception:  # noqa: BLE001 - retry next cycle
                return None
        self._resolved[entry] = asin
        if asin is None:
            raise StoreError(f"Amazon: couldn't find a product ID in '{entry}'")
        return asin

    async def _config_asins(self, client, key: str) -> list[str]:
        out = []
        for entry in self.cfg.get(key, []) or []:
            asin = await self._to_asin(client, str(entry))
            if asin and asin not in out:
                out.append(asin)
        return out

    async def _discover(self, client):
        """Best effort: search is usually blocked from cloud IPs, so failures are ignored."""
        for q in self.cfg.get("search_queries", []) or []:
            try:
                r = await client.get("https://www.amazon.in/s", params={"k": q}, headers=_HEADERS)
            except Exception:  # noqa: BLE001
                return
            if _blocked(r) or r.status_code >= 400:
                return
            doc = LexborHTMLParser(r.text)
            for node in doc.css('div[data-component-type="s-search-result"][data-asin]'):
                asin = node.attributes.get("data-asin")
                h2 = node.css_first("h2")
                if asin and h2 and self.filter.matches(h2.text(strip=True)):
                    self.known.setdefault(asin, dp_url(asin))
            await self.polite_pause(1.5, 3.0)

    # -- checks ----------------------------------------------------------
    async def fetch(self, client):
        for key in ("asins", "product_urls", "fast_check"):
            for asin in await self._config_asins(client, key):
                self.known.setdefault(asin, dp_url(asin))

        every = max(1, int(self.cfg.get("discover_every_cycles", 10)))
        if self.cfg.get("search_queries") and self._cycle % every == 0:
            await self._discover(client)
        self._cycle += 1
        if not self.known:
            return []
        return await self._check_many(client, list(self.known))

    async def fetch_fast(self, client) -> list[Item] | None:
        asins = await self._config_asins(client, "fast_check")
        return await self._check_many(client, asins) if asins else None

    async def _check_many(self, client, asins: list[str]) -> list[Item]:
        items, errors = [], []
        for n, asin in enumerate(asins):   # sequential + pauses: Amazon is touchy
            if n:
                await self.polite_pause(1.5, 3.0)    # between listings only, not after the last one
            try:
                item = await self._check_one(client, asin)
            except StoreError as e:
                errors.append(str(e))
                # Unknown this round: keeps the previous state, so no false "back in stock" later.
                item = Item(store=self.id, item_id=asin, title=asin, url=dp_url(asin), in_stock=None)
            if item:
                items.append(item)
                if item.in_stock and self.on_found:
                    # alert now; the other listings are still being checked
                    await self.on_found(client, [item])
        if errors and len(errors) == len(asins):
            raise StoreError(errors[0])
        return items

    async def _check_one(self, client, asin: str) -> Item | None:
        url = f"https://www.amazon.in/gp/aw/d/{asin}"
        try:
            r = await client.get(url, headers=_HEADERS)
        except Exception as e:  # noqa: BLE001
            raise StoreError(f"Amazon: request failed for {asin}: {e!r}")
        if _blocked(r):
            raise StoreError("Amazon: blocked by anti-bot (503/captcha)")
        if r.status_code == 404:
            self.known.pop(asin, None)
            raise StoreError(f"Amazon: product {asin} not found (404)")
        if r.status_code >= 400:
            raise StoreError(f"Amazon: HTTP {r.status_code} for {asin}")
        return self._parse(asin, r.text)

    def _parse(self, asin: str, html: str) -> Item | None:
        doc = LexborHTMLParser(html)
        t = doc.css_first("#productTitle") or doc.css_first("#title")
        title = " ".join(t.text(strip=True).split()) if t else ""
        if not title:
            raise StoreError(f"Amazon: could not read product page for {asin} (layout changed?)")
        if not self.filter.matches(title):
            self.known.pop(asin, None)
            return None

        avail_node = doc.css_first("#availability")
        avail = avail_node.text(strip=True).lower() if avail_node else ""
        # gc-buy-box-*: the buttons on e-gift card pages (amount + recipient email form)
        has_buy = bool(doc.css_first("#add-to-cart-button") or doc.css_first("#buy-now-button")
                       or doc.css_first("#gc-buy-box-atc") or doc.css_first("#gc-buy-box-bn"))
        if "currently unavailable" in avail or "out of stock" in avail:
            in_stock = False
        elif has_buy:
            in_stock = True
        elif avail_node or doc.css_first("#outOfStock"):
            in_stock = False
        else:
            in_stock = None

        price = None
        for sel in _PRICE_SELECTORS:
            node = doc.css_first(sel)
            m = _PRICE.search(node.text()) if node else None
            if m:
                price = float(m.group(0).replace(",", ""))
                break
        return Item(store=self.id, item_id=asin, title=title, url=dp_url(asin),
                    in_stock=in_stock, price=price if in_stock else None)
