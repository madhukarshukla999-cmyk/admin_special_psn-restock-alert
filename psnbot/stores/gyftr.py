from __future__ import annotations

import json
import re

from ..models import Item, StoreError
from .base import Store

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class Gyftr(Store):
    """GyFTR brand pages (Next.js). Each denomination currently on sale is listed in
    reduxState.brandInfo.productDetailStore; when the brand is sold out the list is empty
    and the page shows "SORRY!!! We are Sold Out!". So: listed => in stock."""

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
            except (KeyError, TypeError, ValueError) as e:
                raise StoreError(f"GyFTR: unexpected page data for {slug}: {e!r}")
            if not detail.get("brand_id"):
                raise StoreError(f"GyFTR: brand '{slug}' not found")

            for p in products or []:
                mrp = float(p.get("mrp") or 0) or None
                disc = float(p.get("discount") or 0)
                price = mrp
                if mrp and disc:
                    price = mrp - disc if p.get("discount_type") == "FLAT" else round(mrp * (1 - disc / 100), 2)
                items.append(Item(
                    store=self.id,
                    item_id=str(p.get("product_id")),
                    title=p.get("product_name") or f"{detail.get('brand_name')} ₹{mrp}",
                    url=url,
                    in_stock=True,
                    price=price,
                    mrp=mrp,
                    note=f"{disc:g}% off" if disc and p.get("discount_type") != "FLAT" else "",
                ))
            await self.polite_pause()
        return items
