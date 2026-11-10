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

import httpx

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


async def _post(client, path, json=None, params=None, headers=None, expect=(200, 201)):
    try:
        r = await client.post(f"{API}{path}", json=json, params=params,
                              headers={"Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
                                       **SITE, **(headers or {})}, timeout=20)
    except httpx.HTTPError as e:
        raise GtsError(f"request failed: {e!r}")
    return r


async def _get(client, path, headers=None):
    try:
        r = await client.get(f"{API}{path}", headers={"Accept": "application/json, text/plain, */*", **SITE, **(headers or {})}, timeout=20)
    except httpx.HTTPError as e:
        raise GtsError(f"request failed: {e!r}")
    return r


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        raise GtsError(f"unexpected response (HTTP {r.status_code})")


# -- login (mobile + OTP) --------------------------------------------------------
def _detail(r) -> str:
    """A short, safe snippet of the server's reply, for error messages."""
    try:
        d = r.json()
        return str(d.get("message") or d.get("detail") or d.get("error") or d)[:160]
    except ValueError:
        return (r.text or "")[:160]


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

    # 1) buy-now cart
    cart_body = {"productId": item.item_id, "qty": 1, "name": item.title,
                 "regular_price": item.mrp or item.price, "sale_price": item.price or item.mrp,
                 "is_digital": True, "parentProductId": product_id, "is_cod": False}
    r = await _post(client, "/cart/", params={"userId": acc["user_id"], "buyNow": "true"}, json=cart_body, headers=H)
    if r.status_code in (401, 403):
        raise GtsError("login expired – add this account again with /gtsbuy add")
    if r.status_code not in (200, 201):
        raise GtsError(f"add to cart failed (HTTP {r.status_code})")

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
