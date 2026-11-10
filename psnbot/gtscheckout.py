"""GamesTheShop checkout over its own website API (no browser needed until the Paytm step).

Captured from the site's own requests:
  login:   POST /storefront/auth/sms/request {mobile_number}      -> OTP by SMS
           POST /storefront/auth/sms/verify  {mobile_number, otp} -> {access, refresh} (JWTs)
  profile: GET  /storefront/customer/profile (Bearer)            -> user_id, addresses
  cart:    POST /storefront/cart/?userId=<id>&buyNow=true (Bearer)
  order:   POST /storefront/orders/create  (Bearer) {payment_type:PREPAID, items:[{variant_id,qty}], addresses}
                                                                  -> order_id
  pay:     POST /storefront/orders/checkout (Bearer) {checkout_group_id: order_id}
                                                                  -> {txnToken, paytmOrderId, amount, mid}

Accounts (one per GamesTheShop login) live in state.settings["gts_accounts"]:
  { "<label>": {"mobile": "...", "user_id": "...", "access": "<jwt>", "refresh": "<jwt>",
                "pending_otp": bool, "added": ts} }
Tokens persist in the bot's state (Postgres on Heroku), so logins survive restarts.
"""
from __future__ import annotations

import logging
import time

import os

# GamesTheShop's login/checkout endpoints sit behind Cloudflare, which 403s normal HTTP clients
# from cloud IPs. curl_cffi sends a real Chrome TLS fingerprint (same approach as the Blinkit store).
try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover
    AsyncSession = None

log = logging.getLogger(__name__)

API = "https://green-api.gamestheshop.com/storefront"
# The storefront API only answers requests that look like they come from the shop's own site.
# (The stock checker sends the same Origin/Referer; without them the API rejects the call.)
SITE = {
    "Origin": "https://www.gamestheshop.com",
    "Referer": "https://www.gamestheshop.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
}


class GtsError(Exception):
    """A GamesTheShop checkout step failed (message is safe to show the owner)."""


def accounts(state) -> dict:
    return state.settings.setdefault("gts_accounts", {})


def _auth(acc: dict) -> dict:
    return {"Authorization": f"Bearer {acc['access']}", "Accept": "application/json, text/plain, */*"}


def _proxy() -> str | None:
    """Optional proxy for the checkout calls, e.g. the WARP tunnel if Cloudflare still blocks the IP:
    GTS_PROXY=socks5://127.0.0.1:40000 (or reuse BLINKIT_PROXY)."""
    return os.environ.get("GTS_PROXY") or None


async def _request(method, path, *, json=None, params=None, headers=None):
    if AsyncSession is None:
        raise GtsError("curl_cffi isn't installed (pip install curl_cffi)")
    h = {"Accept": "application/json, text/plain, */*", **SITE, **(headers or {})}
    kw = {"proxy": _proxy()} if _proxy() else {}
    try:
        async with AsyncSession(impersonate="chrome", **kw) as s:
            r = await s.request(method, f"{API}{path}", json=json, params=params, headers=h, timeout=20)
    except Exception as e:  # noqa: BLE001 - network/curl errors surface as a clean message
        raise GtsError(f"request failed: {e!r}")
    return r


async def _post(client, path, json=None, params=None, headers=None, expect=(200, 201)):
    return await _request("POST", path, json=json, params=params, headers=headers)


async def _get(client, path, headers=None):
    return await _request("GET", path, headers=headers)


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        raise GtsError(f"unexpected response (HTTP {r.status_code})")


def _detail(r) -> str:
    """A short, safe snippet of the server's reply, for error messages."""
    try:
        d = r.json()
        return str(d.get("message") or d.get("detail") or d.get("error") or d)[:160]
    except ValueError:
        return (r.text or "")[:160]


# -- login (mobile + OTP) --------------------------------------------------------
async def request_otp(client, mobile: str) -> None:
    r = await _post(client, "/auth/sms/request", json={"mobile_number": mobile})
    if r.status_code != 200:
        raise GtsError(f"couldn't send the OTP (HTTP {r.status_code}: {_detail(r)})")


async def verify_otp(client, state, label: str, mobile: str, otp: str) -> dict:
    r = await _post(client, "/auth/sms/verify", json={"mobile_number": mobile, "otp": otp})
    data = _json(r)
    access, refresh = data.get("access"), data.get("refresh")
    if r.status_code != 200 or not access:
        raise GtsError(data.get("message") or f"wrong or expired OTP (HTTP {r.status_code})")
    acc = accounts(state).setdefault(label, {})
    acc.update(mobile=mobile, access=access, refresh=refresh, pending_otp=False, added=time.time())
    # fill in user_id (needed for the cart call)
    try:
        prof = _json(await _get(client, "/customer/profile", headers=_auth(acc))).get("data") or {}
        acc["user_id"] = str(prof.get("user_id") or prof.get("id") or "")
        acc["addresses"] = prof.get("addresses") if isinstance(prof.get("addresses"), list) else acc.get("addresses")
    except GtsError:
        pass
    state.save()
    return acc


async def profile(client, acc: dict) -> dict:
    r = await _get(client, "/customer/profile", headers=_auth(acc))
    if r.status_code in (401, 403):
        raise GtsError("login expired – add this account again with /gtsbuy add")
    return (_json(r).get("data") or {})


async def _variant(client, product_id: str, variant_id: str) -> tuple[dict, dict]:
    """(variant, product) from the public product detail, to fill the cart exactly like the site."""
    r = await _get(client, f"/products/{product_id}/")
    if r.status_code != 200:
        raise GtsError(f"couldn't read the product (HTTP {r.status_code}: {_detail(r)})")
    d = _json(r).get("data") or {}
    variants = d.get("variants") or []
    v = next((x for x in variants if str(x.get("id")) == str(variant_id)), variants[0] if variants else {})
    return v, d


def _cart_body(v: dict, product_id: str, variant_id: str, item) -> dict:
    """Mirror the website's buy-now cart payload, from live variant data (falling back to the Item)."""
    prod = v.get("product") or {}
    reg = v.get("regular_price") or getattr(item, "mrp", None) or v.get("sale_price") or getattr(item, "price", None)
    sale = v.get("sale_price") or getattr(item, "price", None) or reg
    cat = (prod.get("category") or {}).get("name") or v.get("category") or "Digital"
    plat = (prod.get("platform") or {}).get("name") or v.get("platform") or "PSN"
    edition = (v.get("edition") or prod.get("edition") or {})
    edition = edition.get("name") if isinstance(edition, dict) else (edition or "Unknown")
    is_digital = prod.get("is_digital")
    if is_digital is None:
        is_digital = True if v.get("condition") == "digital" or getattr(item, "store", "") == "gamestheshop" else False
    return {
        "productId": variant_id, "qty": 1,
        "name": v.get("name") or v.get("product_name") or getattr(item, "title", ""),
        "regular_price": float(reg) if reg else None, "sale_price": float(sale) if sale else None,
        "image": v.get("thumbnail") or (v.get("media") or [{}])[0].get("url"),
        "is_digital": bool(is_digital), "parentProductId": product_id,
        "is_cod": bool(prod.get("is_cod_applicable", False)),
        "category": cat, "platform": plat, "edition": edition or "Unknown",
        "release_date": v.get("release_date") or prod.get("release_date"),
        "shipping_date": v.get("shipping_date"), "is_pre_order": bool(v.get("is_pre_order") or prod.get("is_pre_order", False)),
    }


# -- checkout (cart -> order -> Paytm token) -------------------------------------
async def checkout(client, state, acc: dict, item) -> dict:
    """Run one buy-now checkout for `item` (a bot Item for a GamesTheShop card).
    Returns {paytmOrderId, txnToken, amount, mid} ready for payment. Places a real (unpaid) order."""
    if not acc.get("access") or not acc.get("user_id"):
        raise GtsError("not logged in – add the account with /gtsbuy add <mobile>")
    H = _auth(acc)
    product_id = (getattr(item, "extra", {}) or {}).get("product_id")
    if not product_id:
        raise GtsError("missing product id for this card")

    # live product details, so the cart matches what the website sends (prices, category, etc.)
    v, _d = await _variant(client, product_id, item.item_id)

    # 1) buy-now cart
    cart_body = _cart_body(v, product_id, item.item_id, item)
    r = await _post(client, "/cart/", params={"userId": acc["user_id"], "buyNow": "true"}, json=cart_body, headers=H)
    if r.status_code in (401, 403):
        raise GtsError("login expired – add this account again with /gtsbuy add")
    if r.status_code not in (200, 201):
        raise GtsError(f"add to cart failed (HTTP {r.status_code}: {_detail(r)})")

    # 2) create order
    order_body = {"payment_type": "PREPAID", "items": [{"variant_id": item.item_id, "qty": 1}]}
    addr = (acc.get("addresses") or [None])[0] if isinstance(acc.get("addresses"), list) else acc.get("addresses")
    if addr:
        order_body["shipping_address"] = order_body["billing_address"] = addr
    r = await _post(client, "/orders/create", json=order_body, headers=H)
    data = _json(r).get("data") or {}
    order_id = data.get("order_id") or data.get("checkout_group_id")
    if r.status_code not in (200, 201) or not order_id:
        raise GtsError(_json(r).get("message") or f"couldn't create the order (HTTP {r.status_code})")

    # 3) checkout -> Paytm token
    r = await _post(client, "/orders/checkout", json={"checkout_group_id": order_id}, headers=H)
    pay = _json(r).get("data") or {}
    if r.status_code not in (200, 201) or not pay.get("txnToken"):
        raise GtsError(_json(r).get("message") or f"couldn't start the payment (HTTP {r.status_code})")
    return {"order_id": order_id, "paytmOrderId": pay.get("paytmOrderId"), "txnToken": pay.get("txnToken"),
            "amount": pay.get("amount") or data.get("grand_total"), "mid": pay.get("mid")}
