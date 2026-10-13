from __future__ import annotations

import json
import re

from ..models import Item, StoreError
from .base import Store

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class Gyftr(Store):
    """GyFTR brand pages (Next.js).

    Every card value on sale is listed in reduxState.brandInfo.productDetailStore.
    When the brand is sold out that list is empty and the page shows
    "SORRY!!! We are Sold Out!". So: listed => in stock. (Checked on both normal and
    "OTT" brands such as Steam, Nintendo and Roblox; `stock_left` is not reliable.)
    """

    id = "gyftr"
    name = "GyFTR"

    async def fetch(self, client):
        items: list[Item] = []
        for slug in self.cfg.get("brand_slugs", ["sony-playstation"]):
            url = f"https://www.gyftr.com/{slug}"
            r = await self.get(client, url)
            m = _NEXT_DATA.search(r.text)
            if not m:
                raise StoreError(f"GyFTR: page data not found for {slug} (layout changed?)")
            try:
                brand = json.loads(m.group(1))["props"]["pageProps"]["reduxState"]["brandInfo"]
                products = brand["productDetailStore"]
                detail = brand.get("brandDetailStore") or {}
                pay_options = brand.get("brandPgListStore") or []
            except (KeyError, TypeError, ValueError) as e:
                raise StoreError(f"GyFTR: unexpected page data for {slug}: {e!r}")
            if not detail.get("brand_id"):
                raise StoreError(f"GyFTR: brand '{slug}' not found")

            # The price GyFTR displays includes the best payment-method discount (e.g. UPI 5%).
            best = max(pay_options, key=lambda o: float(o.get("pg_discount") or 0), default={})
            pay_disc = float(best.get("pg_discount") or 0)

            for p in products or []:
                mrp = float(p.get("mrp") or 0) or None
                disc = float(p.get("discount") or 0)
                price, notes = mrp, []
                if mrp and disc:   # product-level offer
                    if p.get("discount_type") == "FLAT":
                        price = mrp - disc
                        notes.append(f"₹{disc:g} off")
                    else:
                        price = mrp * (1 - disc / 100)
                        notes.append(f"{disc:g}% off")
                if price and pay_disc:
                    price = price * (1 - pay_disc / 100)
                    notes.append(f"{pay_disc:g}% off with {best.get('pg_name')}")
                items.append(Item(
                    store=self.id,
                    item_id=str(p.get("product_id")),
                    title=p.get("product_name") or f"{detail.get('brand_name')} ₹{mrp:g}",
                    url=url,
                    in_stock=True,
                    price=round(price, 2) if price else None,
                    mrp=mrp,
                    note=", ".join(notes),
                ))
            await self.polite_pause()
        return items
