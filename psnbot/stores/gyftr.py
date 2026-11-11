from __future__ import annotations

import json
import re

from ..models import Item, StoreError
from .base import Store

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _find(o, test, depth: int = 0):
    """First nested value for which test(value) is true (GyFTR moves its data around)."""
    if depth > 8:
        return None
    if test(o):
        return o
    vals = o.values() if isinstance(o, dict) else o if isinstance(o, list) else ()
    for v in vals:
        hit = _find(v, test, depth + 1)
        if hit is not None:
            return hit
    return None


def _is_product(p) -> bool:
    return isinstance(p, dict) and ("product_id" in p or "productId" in p) and \
        any(k in p for k in ("mrp", "denomination", "price", "product_name", "productName"))


def _num(v) -> float | None:
    try:
        n = float(str(v).replace(",", "").replace("₹", "").strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def parse_brand(data: dict) -> tuple[list[dict], dict, list[dict], bool]:
    """(products, brand details, payment options, page says 'not found') from __NEXT_DATA__."""
    props = data.get("props", data)
    page = props.get("pageProps", props) if isinstance(props, dict) else {}
    brand = (((page.get("reduxState") or {}).get("brandInfo")) if isinstance(page, dict) else None) \
        or _find(page, lambda o: isinstance(o, dict) and "productDetailStore" in o) or {}

    products = brand.get("productDetailStore")
    if isinstance(products, dict):                       # e.g. {"data": [...]} / {"products": [...]}
        products = _find(products, lambda o: isinstance(o, list) and o and all(_is_product(p) for p in o))
    if not isinstance(products, list):
        products = _find(page, lambda o: isinstance(o, list) and o and all(_is_product(p) for p in o))
    products = [p for p in (products or []) if _is_product(p)]

    detail = brand.get("brandDetailStore") if isinstance(brand.get("brandDetailStore"), dict) else {}
    pay = brand.get("brandPgListStore") if isinstance(brand.get("brandPgListStore"), list) else []
    not_found = bool(page.get("isNotFound")) if isinstance(page, dict) else False
    return products, detail, pay, not_found


class Gyftr(Store):
    """GyFTR brand pages (Next.js).

    Every card value on sale is listed in the page data (reduxState.brandInfo.productDetailStore
    on the current site; found wherever it is if GyFTR moves it). When PlayStation is sold out the
    list is empty and the page shows "No Product Found!" – that's sold out, not an error.
    `stock_left` is not reliable, so: listed => in stock.
    """

    id = "gyftr"
    name = "GyFTR"

    async def fetch(self, client):
        items: list[Item] = []
        for n, slug in enumerate(self.cfg.get("brand_slugs", ["sony-playstation"])):
            if n:
                await self.polite_pause()
            url = f"https://www.gyftr.com/{slug}"
            r = await self.get(client, url)
            m = _NEXT_DATA.search(r.text)
            if not m:
                raise StoreError(f"GyFTR: page data not found for {slug} (layout changed?)")
            try:
                data = json.loads(m.group(1))
            except ValueError as e:
                raise StoreError(f"GyFTR: couldn't read page data for {slug}: {e!r}")
            products, detail, pay_options, not_found = parse_brand(data)
            if not_found:
                raise StoreError(f"GyFTR: brand page '{slug}' not found (address changed?)")

            # Payment options can give a discount (e.g. UPI 5%) or, now, a surcharge (cards +8%):
            # the price shown is the best real discount; surcharges are ignored.
            def disc(o):
                return _num(o.get("pg_discount")) or 0
            best = max(pay_options, key=disc, default={})
            pay_disc = disc(best) if best else 0

            brand_name = detail.get("brand_name") or "Sony PlayStation"
            for p in products:
                mrp = _num(p.get("mrp")) or _num(p.get("denomination")) or _num(p.get("price"))
                d = _num(p.get("discount")) or 0
                price, notes = mrp, []
                if mrp and d:   # product-level offer
                    if str(p.get("discount_type", "")).upper() == "FLAT":
                        price = mrp - d
                        notes.append(f"₹{d:g} off")
                    else:
                        price = mrp * (1 - d / 100)
                        notes.append(f"{d:g}% off")
                if price and pay_disc:
                    price = price * (1 - pay_disc / 100)
                    notes.append(f"{pay_disc:g}% off with {best.get('pg_name')}")
                title = p.get("product_name") or p.get("productName") or \
                    (f"{brand_name} ₹{mrp:,.0f}" if mrp else brand_name)
                items.append(Item(
                    store=self.id,
                    item_id=str(p.get("product_id") or p.get("productId")),
                    title=title,
                    url=url,
                    in_stock=True,
                    price=round(price, 2) if price else None,
                    mrp=mrp,
                    note=", ".join(notes),
                ))
        return items
