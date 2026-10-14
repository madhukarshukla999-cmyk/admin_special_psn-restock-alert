from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from ..models import Item, StoreError
from .base import Store

# Blinkit sits behind Cloudflare, which rejects normal Python HTTP clients and cloud/data-center
# IPs. curl_cffi sends a real Chrome TLS fingerprint; it still won't help on a blocked IP.
try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover
    AsyncSession = None

BASE = "https://blinkit.com"
_PRID = re.compile(r"/prid/(\d+)")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_NOT_SERVICEABLE = ("not serviceable", "coming soon to your area", "don't deliver", "do not deliver",
                    "currently unavailable in your area", "unserviceable")


def _walk(o, path=""):
    """Yield (path, key, value) for every key in nested JSON."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield path, k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{path}[{i}]")


def read_stock(data: dict, prid: str) -> tuple[bool | None, dict]:
    """Work out stock from Blinkit's product JSON. Returns (in_stock, details).

    Uses only signals that belong to *this* product id, so "similar products" on the same
    page can't affect the result. Returns None when the signals are missing or disagree.
    """
    info: dict = {"name": None, "price": None, "mrp": None, "signals": []}

    pos, neg = 0, 0
    for path, k, v in _walk(data):
        kl = k.lower()
        # Only objects tagged with this product id count ("similar products" are ignored).
        if kl == "common_attributes" and isinstance(v, dict) and str(v.get("product_id")) == prid:
            info["name"] = info["name"] or v.get("name")
            info["price"] = info["price"] or v.get("price")
            info["mrp"] = info["mrp"] or v.get("mrp")
            state = str(v.get("state") or "").lower()
            if state:
                info["signals"].append(f"state={state}")
                if state in ("available", "in_stock", "instock"):
                    pos += 1
                elif any(w in state for w in ("unavailable", "out", "sold", "oos")):
                    neg += 1
            inv = v.get("inventory")
            if isinstance(inv, (int, float)):
                info["signals"].append(f"inventory={inv}")
                pos, neg = (pos + 1, neg) if inv > 0 else (pos, neg + 1)
        if kl == "cart_item" and isinstance(v, dict) and str(v.get("product_id")) == prid:
            info["name"] = info["name"] or v.get("display_name") or v.get("product_name")
            info["price"] = info["price"] or v.get("price")
            info["mrp"] = info["mrp"] or v.get("mrp")
            inv = v.get("inventory")
            if isinstance(inv, (int, float)):
                info["signals"].append(f"cart_item.inventory={inv}")
                pos, neg = (pos + 1, neg) if inv > 0 else (pos, neg + 1)
            if "unavailable_quantity" in v and v.get("unavailable_quantity"):
                info["signals"].append("unavailable_quantity")
                neg += 1

    # Button labels ("Add to cart") are deliberately NOT used: Blinkit's response contains them
    # as templates even when the product is out of stock (seen on prid 698590: state=out_of_stock,
    # inventory=0, yet 4x "Add to cart"). Only this product's own state/inventory decide.

    if pos and not neg:
        return True, info
    if neg and not pos:
        return False, info
    if not info["signals"]:
        blob = json.dumps(data).lower()
        if any(w in blob for w in _NOT_SERVICEABLE):
            raise StoreError("Blinkit: this location isn't serviceable (check pincode / lat-lon)")
    return None, info   # nothing found or conflicting -> unknown (never alerts)


def proxy_url() -> str | None:
    """Blinkit blocks cloud IPs, so on Heroku route it through a residential proxy:
    BLINKIT_PROXY=http://user:pass@host:port (only Blinkit uses it)."""
    return os.environ.get("BLINKIT_PROXY") or None


def session():
    p = proxy_url()
    return AsyncSession(impersonate="chrome", **({"proxy": p} if p else {}))


class Blinkit(Store):
    """Blinkit product pages (no login needed): product id + delivery location."""

    id = "blinkit"
    name = "Blinkit"

    def __init__(self, cfg, title_filter):
        super().__init__(cfg, title_filter)
        self._device = str(uuid.uuid4())
        self._session_id = str(uuid.uuid4())

    def _prids(self) -> list[str]:
        out = []
        for entry in self.cfg.get("product_urls", []) or []:
            s = str(entry).strip()
            m = _PRID.search(s)
            prid = m.group(1) if m else (s if s.isdigit() else None)
            if not prid:
                raise StoreError(f"Blinkit: couldn't find a product id in '{s}'")
            if prid not in out:
                out.append(prid)
        return out

    def _headers(self, prid: str) -> dict:
        lat, lon = self.cfg.get("lat"), self.cfg.get("lon")
        if lat is None or lon is None:
            raise StoreError("Blinkit: set lat/lon for your pincode in config.yaml")
        return {
            "accept": "*/*", "accept-language": "en-IN,en;q=0.9", "content-type": "application/json",
            "app_client": "consumer_web", "app_version": "1010101010", "web_app_version": "1008010016",
            "rn_bundle_version": "1009003012", "access_token": "null",
            "device_id": self._device, "session_uuid": self._session_id,
            "lat": str(lat), "lon": str(lon),
            "origin": BASE, "referer": f"{BASE}/prn/x/prid/{prid}", "user-agent": UA,
            "cookie": f"gr_1_deviceId={self._device}; gr_1_lat={lat}; gr_1_lon={lon}",
        }

    async def fetch(self, client):
        return await self._check(self._prids())

    async def fetch_fast(self, client):
        return await self._check(self._prids()) if self.cfg.get("fast_check") else None

    async def _check(self, prids: list[str]) -> list[Item]:
        if AsyncSession is None:
            raise StoreError("Blinkit: install curl_cffi (pip install curl_cffi)")
        items, errors = [], []
        async with session() as s:
            for prid in prids:
                try:
                    items.append(await self._check_one(s, prid))
                except StoreError as e:
                    errors.append(str(e))
                    items.append(Item(store=self.id, item_id=prid, title=prid,
                                      url=f"{BASE}/prn/x/prid/{prid}", in_stock=None))
                await self.polite_pause(1.0, 2.5)
        if errors and len(errors) == len(prids):
            raise StoreError(errors[0])
        return items

    async def _check_one(self, s, prid: str) -> Item:
        try:
            r = await s.post(f"{BASE}/v1/layout/product/{prid}", headers=self._headers(prid), timeout=30)
        except Exception as e:  # noqa: BLE001
            raise StoreError(f"Blinkit: request failed: {e!r}")
        if r.status_code == 403 or "you have been blocked" in r.text.lower() or "just a moment" in r.text.lower():
            raise StoreError("Blinkit: blocked by Cloudflare (this server's IP is blocked; "
                             "run the Blinkit check from a home connection)")
        if r.status_code >= 400:
            raise StoreError(f"Blinkit: HTTP {r.status_code} for product {prid}")
        try:
            data = r.json()
        except ValueError:
            raise StoreError("Blinkit: response wasn't JSON (layout changed?)")

        in_stock, info = read_stock(data, prid)
        if in_stock is None or self.cfg.get("save_raw"):
            self._save_raw(prid, data)
        if in_stock is None:
            raise StoreError(f"Blinkit: couldn't tell stock for {prid} (signals: {info['signals'] or 'none'}); "
                             f"raw response saved to data/blinkit_{prid}.json")
        name = info["name"] or "Sony PlayStation Instant Voucher"

        def num(v):
            try:
                return float(str(v).replace("₹", "").replace(",", "").strip())
            except (TypeError, ValueError):
                return None
        return Item(store=self.id, item_id=prid, title=str(name),
                    url=f"{BASE}/prn/x/prid/{prid}", in_stock=in_stock,
                    price=num(info["price"]), mrp=num(info["mrp"]),
                    note=f"pincode {self.cfg.get('pincode')}" if self.cfg.get("pincode") else "")

    @staticmethod
    def _save_raw(prid: str, data: dict) -> None:
        p = Path("data") / f"blinkit_{prid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
