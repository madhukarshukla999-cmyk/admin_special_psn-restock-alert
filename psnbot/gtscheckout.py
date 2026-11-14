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

import asyncio
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
QR_MINUTES = 5            # the QR is deleted from Telegram after this long
COOLDOWN = 10 * 60        # same account + same card: no second order within this time (stock flapping)
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


def _session():
    """A curl_cffi session (Chrome fingerprint). Reusing one across a checkout keeps the TLS
    connection open, so the 2nd–5th calls skip the handshake (the slow part on a US->India path)."""
    if AsyncSession is None:
        raise GtsError("curl_cffi isn't installed (pip install curl_cffi)")
    kw = {"proxy": _proxy()} if _proxy() else {}
    return AsyncSession(impersonate="chrome", **kw)


# One long-lived session, kept warm by warm_up() below. Opening a connection to India costs
# several round trips (~0.5-0.75 s from a US server) and cards can sell out in seconds, so the
# handshake must already be done before a restock, not during the checkout.
_WARM = None
_WARM_AT = 0.0
WARM_MAX_AGE = 15 * 60


def warm_session():
    global _WARM, _WARM_AT
    if _WARM is None or time.time() - _WARM_AT > WARM_MAX_AGE:
        drop_warm()
        _WARM = _session()
        _WARM_AT = time.time()
    return _WARM


def drop_warm() -> None:
    """Throw the session away (after an error, or when it's too old) so the next call reconnects."""
    global _WARM
    s, _WARM = _WARM, None
    if s is None:
        return
    try:
        closer = getattr(s, "close", None)
        if closer:
            res = closer()
            if asyncio.iscoroutine(res):
                task = asyncio.create_task(res)
                task.add_done_callback(lambda t: t.exception())
    except Exception:  # noqa: BLE001 - closing a dead session must never raise
        pass


async def warm_up() -> bool:
    """Cheap unauthenticated call that keeps the connection hot. True if it answered."""
    try:
        r = await _request("GET", "/products/categories", session=warm_session())
        return r.status_code < 500
    except GtsError:
        drop_warm()
        return False


async def _request(method, path, *, json=None, params=None, headers=None, session=None):
    h = {"Accept": "application/json, text/plain, */*", **SITE, **(headers or {})}
    s = session or warm_session()                     # already-connected session: no handshake
    try:
        return await s.request(method, f"{API}{path}", json=json, params=params, headers=h, timeout=12)
    except GtsError:
        raise
    except Exception as e:  # noqa: BLE001 - network/curl errors surface as a clean message
        if s is _WARM:
            drop_warm()                               # maybe a dead connection: reconnect next time
        raise GtsError(f"request failed: {e!r}")


async def _post(client, path, json=None, params=None, headers=None, expect=(200, 201), session=None):
    return await _request("POST", path, json=json, params=params, headers=headers, session=session)


async def _get(client, path, headers=None, session=None):
    return await _request("GET", path, headers=headers, session=session)


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
    async with _session() as session:
        return await _verify_otp(session, state, label, mobile, otp)


async def _verify_otp(session, state, label: str, mobile: str, otp: str) -> dict:
    r = await _post(None, "/auth/sms/verify", json={"mobile_number": mobile, "otp": otp}, session=session)
    data = _json(r)
    access, refresh = data.get("access"), data.get("refresh")
    if r.status_code != 200 or not access:
        raise GtsError(data.get("message") or f"wrong or expired OTP (HTTP {r.status_code})")
    acc = accounts(state).setdefault(label, {})
    acc.update(mobile=mobile, access=access, refresh=refresh, pending_otp=False, added=time.time())
    acc.pop("expired", None)                       # a fresh login clears the watchdog's flag
    acc.pop("expired_told", None)
    state.save()                                   # save the login first, so a profile hiccup can't lose it
    # fill in user_id (needed for the cart call), reusing the same connection as the verify call
    try:
        prof = _json(await _get(None, "/customer/profile", headers=_auth(acc), session=session)).get("data") or {}
        acc["user_id"] = str(prof.get("user_id") or prof.get("id") or "")
        acc["addresses"] = prof.get("addresses") if isinstance(prof.get("addresses"), list) else acc.get("addresses")
        state.save()
    except Exception as e:  # noqa: BLE001 - login already saved; user_id is filled in on first use
        log.warning("GamesTheShop: profile lookup after login failed: %r", e)
    return acc


async def profile(client, acc: dict) -> dict:
    r = await _get(client, "/customer/profile", headers=_auth(acc))
    if r.status_code in (401, 403):
        raise GtsError("login expired – add this account again with /gtsbuy add")
    return (_json(r).get("data") or {})


# card details don't change, so cache them: a multi-card restock across several accounts then
# fetches each product only once (product_id -> (fetched_at, data))
_VCACHE: dict[str, tuple[float, dict]] = {}
_VCACHE_TTL = 3600


async def prefetch(product_id: str) -> None:
    """Warm the details cache for a card (call when it's first seen, so buying skips this request)."""
    try:
        await _product(product_id)
    except GtsError:
        pass


async def _product(product_id: str, session=None) -> dict:
    hit = _VCACHE.get(product_id)
    if hit and time.time() - hit[0] < _VCACHE_TTL:
        return hit[1]
    r = await _get(None, f"/products/{product_id}/", session=session)
    if r.status_code != 200:
        raise GtsError(f"couldn't read the product (HTTP {r.status_code}: {_detail(r)})")
    d = _json(r).get("data") or {}
    _VCACHE[product_id] = (time.time(), d)
    return d


async def _variant(client, product_id: str, variant_id: str, session=None) -> tuple[dict, dict]:
    """(variant, product) from the public product detail, to fill the cart exactly like the site."""
    d = await _product(product_id, session=session)
    variants = d.get("variants") or []
    v = next((x for x in variants if str(x.get("id")) == str(variant_id)), variants[0] if variants else {})
    return v, d


def _as_str(*candidates) -> str | None:
    """First candidate as a plain string: a dict becomes its 'name', others str()'d. The API
    rejects objects ('category must be a string'), so these must never be dicts."""
    for c in candidates:
        if c is None or c == "":
            continue
        if isinstance(c, dict):
            n = c.get("name")
            if n:
                return str(n)
            continue
        return str(c)
    return None


def _cart_body(v: dict, product_id: str, variant_id: str, item) -> dict:
    """Mirror the website's buy-now cart payload, from live variant data (falling back to the Item)."""
    prod = v.get("product") or {}
    reg = v.get("regular_price") or getattr(item, "mrp", None) or v.get("sale_price") or getattr(item, "price", None)
    sale = v.get("sale_price") or getattr(item, "price", None) or reg
    is_digital = prod.get("is_digital")
    if is_digital is None:
        is_digital = v.get("condition") == "digital" or getattr(item, "store", "") == "gamestheshop"
    return {
        "productId": variant_id, "qty": 1,
        "name": _as_str(v.get("name"), v.get("product_name"), getattr(item, "title", "")) or "",
        "regular_price": float(reg) if reg else None, "sale_price": float(sale) if sale else None,
        "image": _as_str(v.get("thumbnail"), (v.get("media") or [{}])[0].get("url")),
        "is_digital": bool(is_digital), "parentProductId": product_id,
        "is_cod": bool(prod.get("is_cod_applicable", False)),
        "category": _as_str(prod.get("category"), v.get("category")) or "Digital",
        "platform": _as_str(prod.get("platform"), v.get("platform")) or "PSN",
        "edition": _as_str(v.get("edition"), prod.get("edition")) or "Unknown",
        "release_date": v.get("release_date") or prod.get("release_date"),
        "shipping_date": v.get("shipping_date"),
        "is_pre_order": bool(v.get("is_pre_order") or prod.get("is_pre_order", False)),
    }


# -- checkout (cart -> order -> Paytm token) -------------------------------------
async def checkout(client, state, acc: dict, item) -> dict:
    """Run one buy-now checkout for `item` (a bot Item for a GamesTheShop card).
    Returns {paytmOrderId, txnToken, amount, mid} ready for payment. Places a real (unpaid) order."""
    if not acc.get("access"):
        raise GtsError("not logged in – add the account with /gtsbuy add <mobile>")
    H = _auth(acc)
    product_id = (getattr(item, "extra", {}) or {}).get("product_id")
    if not product_id:
        raise GtsError("missing product id for this card")

    s = warm_session()                                # already connected: saves the TLS handshake
    if not acc.get("user_id"):                    # login-time profile fetch may have been skipped
        pr = await _get(None, "/customer/profile", headers=H, session=s)
        if pr.status_code in (401, 403):
            raise GtsError("login expired – add this account again with /gtsbuy add")
        prof = _json(pr).get("data") or {}
        acc["user_id"] = str(prof.get("user_id") or prof.get("id") or "")
        if isinstance(prof.get("addresses"), list):
            acc["addresses"] = prof["addresses"]
        if not acc.get("user_id"):
            raise GtsError("couldn't read the account id – add the account again with /gtsbuy add")
        state.save()

    # card details (cached), so the cart matches what the website sends (prices, category, etc.)
    v, _d = await _variant(client, product_id, item.item_id, session=s)

    # 1) buy-now cart
    cart_body = _cart_body(v, product_id, item.item_id, item)
    r = await _post(client, "/cart/", params={"userId": acc["user_id"], "buyNow": "true"}, json=cart_body, headers=H, session=s)
    if r.status_code in (401, 403):
        raise GtsError("login expired – add this account again with /gtsbuy add")
    if r.status_code not in (200, 201):
        raise GtsError(f"add to cart failed (HTTP {r.status_code}: {_detail(r)})")

    # 2) create order
    order_body = {"payment_type": "PREPAID", "items": [{"variant_id": item.item_id, "qty": 1}]}
    addr = (acc.get("addresses") or [None])[0] if isinstance(acc.get("addresses"), list) else acc.get("addresses")
    if addr:
        order_body["shipping_address"] = order_body["billing_address"] = addr
    r = await _post(client, "/orders/create", json=order_body, headers=H, session=s)
    data = _json(r).get("data") or {}
    order_id = data.get("order_id") or data.get("checkout_group_id")
    if r.status_code not in (200, 201) or not order_id:
        raise GtsError(_json(r).get("message") or f"couldn't create the order (HTTP {r.status_code})")

    # 3) checkout -> Paytm token
    r = await _post(client, "/orders/checkout", json={"checkout_group_id": order_id}, headers=H, session=s)
    pay = _json(r).get("data") or {}
    if r.status_code not in (200, 201) or not pay.get("txnToken"):
        raise GtsError(_json(r).get("message") or f"couldn't start the payment (HTTP {r.status_code})")
    return {"order_id": order_id, "paytmOrderId": pay.get("paytmOrderId"), "txnToken": pay.get("txnToken"),
            "amount": pay.get("amount") or data.get("grand_total"), "mid": pay.get("mid")}


# -- Paytm: token -> UPI payment link -> QR photo in the owner's DM -------------------------
# Paytm's documented "Process Transaction" API with paymentMode UPI_INTENT returns a UPI deep link
# (upi://pay?...) for the order, which is exactly what a UPI QR encodes. No browser needed.
PAYTM_HOSTS = ("https://secure.paytmpayments.com", "https://securegw.paytm.in")


async def paytm_upi_link(pay: dict) -> str:
    """UPI deep link for the GamesTheShop order's Paytm token. Raises GtsError with Paytm's reason."""
    if AsyncSession is None:
        raise GtsError("curl_cffi isn't installed (pip install curl_cffi)")
    mid, oid, tok = pay.get("mid"), pay.get("paytmOrderId"), pay.get("txnToken")
    if not (mid and oid and tok):
        raise GtsError("checkout didn't return a Paytm token")
    body = {"head": {"channelId": "WAP", "txnToken": tok},
            "body": {"requestType": "NATIVE", "mid": mid, "orderId": oid, "paymentMode": "UPI_INTENT"}}
    kw = {"proxy": _proxy()} if _proxy() else {}

    async def ask(host):
        """(link, None) or (None, reason) from one Paytm host."""
        try:
            async with AsyncSession(impersonate="chrome", **kw) as s:
                r = await s.post(f"{host}/theia/api/v1/processTransaction", params={"mid": mid, "orderId": oid},
                                 json=body, headers={"Content-Type": "application/json"}, timeout=8)
            d = r.json()
        except Exception as e:  # noqa: BLE001
            return None, f"{host}: {e!r}"
        b = d.get("body") or {}
        link = (b.get("deepLinkInfo") or {}).get("deepLink") or b.get("deepLink")
        if link and str(link).startswith("upi:"):
            return str(link), None
        info, txn = b.get("resultInfo") or {}, b.get("txnInfo") or {}
        return None, (txn.get("RESPMSG") or info.get("resultMsg") or str(d)[:160])

    # one host at a time (the same token sent to both at once could look like a duplicate payment);
    # secure.paytmpayments.com is GamesTheShop's own, the old securegw.paytm.in is only a fallback
    last = "no reply"
    for host in PAYTM_HOSTS:
        link, why = await ask(host)
        if link:
            return link
        last = why or last
    raise GtsError(f"Paytm didn't give a UPI link: {last}")


def qr_png(data: str) -> bytes | None:
    """PNG of a QR code for `data` (needs the small pure-Python 'segno' package)."""
    try:
        import io
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=10, border=4)
    return buf.getvalue()


async def send_photo(client, token: str, chat_id: str, png: bytes, caption: str, reply_markup: dict | None = None) -> int | None:
    """Telegram sendPhoto (multipart). Returns the message id."""
    import json as _json
    data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = _json.dumps(reply_markup)
    try:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data=data, files={"photo": ("qr.png", png, "image/png")}, timeout=30)
        j = r.json()
        return (j.get("result") or {}).get("message_id") if j.get("ok") else None
    except Exception as e:  # noqa: BLE001
        log.warning("sendPhoto failed: %r", e)
        return None


# Open payments (not paid, not cancelled, not expired), so the buttons under a QR can act on them.
# Kept in memory only: the Paytm token is short-lived and shouldn't be stored.
PENDING: dict[str, dict] = {}


def upi_id(state, label: str | None = None) -> str:
    """The account's own UPI ID if it has one, else the default UPI ID (or '')."""
    own = str((accounts(state).get(label) or {}).get("upi_id") or "").strip() if label else ""
    return own or str(state.settings.get("gts_upi_id") or "").strip()


def pay_mode(state, label: str | None = None) -> str:
    """'qr' (QR in DM) or 'upi' (payment request to the UPI ID, QR as a backup).
    An account can override the default ('qr' / 'upi'); 'upi' needs a UPI ID, else it's 'qr'."""
    own = (accounts(state).get(label) or {}).get("pay_mode") if label else None
    m = own if own in ("qr", "upi") else state.settings.get("gts_pay_mode")
    return m if m in ("qr", "upi") and (m != "upi" or upi_id(state, label)) else "qr"


async def paytm_upi_collect(pay: dict, vpa: str) -> str:
    """Ask Paytm to send a payment request (UPI collect) for this order to `vpa`. Returns Paytm's
    status text on success; raises GtsError with Paytm's reason otherwise."""
    if AsyncSession is None:
        raise GtsError("curl_cffi isn't installed (pip install curl_cffi)")
    mid, oid, tok = pay.get("mid"), pay.get("paytmOrderId"), pay.get("txnToken")
    if not (mid and oid and tok):
        raise GtsError("checkout didn't return a Paytm token")
    body = {"head": {"channelId": "WAP", "txnToken": tok},
            "body": {"requestType": "NATIVE", "mid": mid, "orderId": oid, "paymentMode": "UPI",
                     "payerAccount": vpa}}
    kw = {"proxy": _proxy()} if _proxy() else {}
    last = "no reply"
    for host in PAYTM_HOSTS:                          # one host at a time: never two requests at once
        try:
            async with AsyncSession(impersonate="chrome", **kw) as s:
                r = await s.post(f"{host}/theia/api/v1/processTransaction", params={"mid": mid, "orderId": oid},
                                 json=body, headers={"Content-Type": "application/json"}, timeout=8)
            d = r.json()
        except Exception as e:  # noqa: BLE001
            last = f"{host}: {e!r}"
            continue
        b = d.get("body") or {}
        info, txn = b.get("resultInfo") or {}, b.get("txnInfo") or {}
        status = str(info.get("resultStatus") or "").upper()
        msg = txn.get("RESPMSG") or info.get("resultMsg") or ""
        if status in ("S", "P", "PENDING") or str(info.get("resultCode")) in ("0000", "400", "402"):
            return msg or "request sent"
        last = msg or str(d)[:160]
        if status == "F":
            break                                     # a real refusal: the other host would say the same
    raise GtsError(f"Paytm didn't send the UPI request: {last}")


def _pay_buttons(key: str, vpa: str) -> dict:
    row = [{"text": "❌ Cancel", "callback_data": f"gp:x:{key}"}]
    if vpa:
        row.insert(0, {"text": "📲 Send to my UPI ID", "callback_data": f"gp:upi:{key}"})
    return {"inline_keyboard": [row, [{"text": "🔄 Check if the shop got the payment",
                                       "callback_data": f"gp:chk:{key}"}]]}


async def pay_to_dm(bot, client, label: str, title: str, pay: dict, chat_ids: list[str], secs: float | None = None) -> str:
    """Send the order's payment to the owner: a UPI request to their UPI ID (pay mode 'upi') and/or
    the UPI QR, with ❌ Cancel / 📲 Send to my UPI ID buttons. Returns a status line.
    `secs` = how long the order took, shown in the caption so we can see the real speed."""
    import html as _html
    import secrets
    state = bot.state
    vpa = upi_id(state, label)                        # this account's UPI ID (or the default)
    amount = pay.get("amount") or "?"
    minutes = QR_MINUTES
    speed = f"⚡ Ordered in {secs:.1f}s\n" if secs is not None else ""
    head = (f"💳 <b>GamesTheShop</b> · {_html.escape(title)}\n"
            f"👤 Account: {_html.escape(label)}\n"
            f"💰 ₹{_html.escape(str(amount))} · 🧾 {_html.escape(str(pay.get('paytmOrderId', ''))[:40])}\n"
            f"{speed}")

    requested = ""
    if pay_mode(state, label) == "upi":               # UPI request first: no scanning needed
        try:
            await paytm_upi_collect(pay, vpa)
            requested = f"📲 <b>Payment request sent to {_html.escape(vpa)}</b> – approve it in your UPI app.\n"
        except GtsError as e:
            requested = f"⚠️ UPI request failed ({_html.escape(str(e))[:120]}). Use the QR instead.\n"

    try:
        link = await paytm_upi_link(pay)
    except GtsError:
        if requested.startswith("📲"):               # the request already went out; no QR is fine
            link = ""
        else:
            raise
    key = secrets.token_hex(4)
    PENDING[key] = {**pay, "label": label, "title": title, "chat": [], "ts": time.time(),
                    "requested": requested.startswith("📲")}
    kb = _pay_buttons(key, "" if requested.startswith("📲") else vpa)
    caption = (head + "\n" + requested +
               (f"⏳ Pay within ~{minutes} min. Scan with any UPI app and enter your PIN.\n"
                "Same phone: save the image → UPI app → Scan QR → gallery." if link else
                f"⏳ Approve within ~{minutes} min."))
    png = qr_png(link) if link else None
    sent = []
    for cid in chat_ids:
        mid = await send_photo(client, bot.tg.token, cid, png, caption, kb) if png else None
        if mid:
            sent.append((cid, mid, "photo"))
            continue
        text = caption + (f"\n\nUPI payment link (copy into your UPI app):\n<code>{_html.escape(link)}</code>" if link else "")
        res = await bot.tg.api(client, "sendMessage", {"chat_id": cid, "text": text, "parse_mode": "HTML",
                                                       "disable_web_page_preview": True, "reply_markup": kb})
        if res and res.get("message_id"):
            sent.append((cid, res["message_id"], "text"))
    PENDING[key]["chat"] = sent

    async def expire():                              # delete it once it can't be paid any more
        p = PENDING.pop(key, None)
        if p is None:
            return                                   # already cancelled or already reported
        PAID_SEEN[key] = dict(p)                     # the 🔄 button keeps working afterwards
        for cid, mid, _ in sent:
            await bot.tg.api(client, "deleteMessage", {"chat_id": cid, "message_id": mid})
    bot._later(minutes * 60, expire())
    # read the order back from GamesTheShop until it's paid / failed (the bot pays through Paytm,
    # so the website never runs its own "payment done" step)
    watch = asyncio.create_task(watch_order(bot, client, key, list(chat_ids)))
    bot._bg.add(watch)
    watch.add_done_callback(bot._bg.discard)
    if requested.startswith("📲"):
        return f"UPI request sent to {vpa}" + (" + QR" if png else "")
    return "QR sent to your DM" if png else "UPI link sent to your DM"


async def cancel_payment(bot, client, key: str) -> dict | None:
    """❌ Cancel under a QR: remove it from Telegram now. The unpaid GamesTheShop order just expires."""
    p = PENDING.pop(key, None)
    if not p:
        return None
    for cid, mid, _ in p.get("chat", []):
        await bot.tg.api(client, "deleteMessage", {"chat_id": cid, "message_id": mid})
    return p


async def request_payment(bot, key: str) -> str:
    """📲 Send to my UPI ID under a QR. Returns the UPI ID used."""
    p = PENDING.get(key)
    if not p:
        raise GtsError("this payment has expired or was cancelled")
    vpa = upi_id(bot.state, p.get("label"))
    if not vpa:
        raise GtsError("set a UPI ID first: /buy → ⚙️ account → ✏️ UPI ID")
    await paytm_upi_collect(p, vpa)
    p["requested"] = True
    return vpa


# -- several cards for one account, in parallel ------------------------------------------------
async def order_cards(client, state, acc: dict, items: list, on_result) -> None:
    """Order every card for one account AT THE SAME TIME. `await on_result(item, pay, error, secs)`
    is called as soon as each card is done, so its QR can go out straight away.

    GamesTheShop's buy-now cart belongs to the account, so parallel orders could step on each
    other. The order request names its own card, so they usually don't; any card whose parallel
    attempt fails (except an expired login) is retried once on its own, one after another, which
    is the order the website itself uses."""
    async def attempt(it):
        t0 = time.time()
        try:
            pay = await checkout(client, state, acc, it)
            await on_result(it, pay, None, time.time() - t0)
            return None
        except GtsError as e:
            return (it, e, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            log.exception("GamesTheShop order crashed for %s", getattr(it, "title", "?"))
            return (it, GtsError(f"unexpected error {e!r}"[:200]), time.time() - t0)

    failed = [f for f in await asyncio.gather(*(attempt(i) for i in items)) if f]
    if not failed:
        return
    if any("login expired" in str(e) for _, e, _ in failed):
        for it, e, secs in failed:
            await on_result(it, None, e, secs)
        return
    if len(items) > 1:
        log.info("GamesTheShop: %d of %d parallel orders failed, retrying them one by one", len(failed), len(items))
    for it, first_err, _ in failed:                  # retry one by one (and only if it ran in parallel)
        if len(items) == 1:
            await on_result(it, None, first_err, 0.0)
            continue
        t0 = time.time()
        try:
            pay = await checkout(client, state, acc, it)
            await on_result(it, pay, None, time.time() - t0)
        except GtsError as e:
            await on_result(it, None, e, time.time() - t0)
            if "login expired" in str(e):
                return
        except Exception as e:  # noqa: BLE001
            log.exception("GamesTheShop order retry crashed")
            await on_result(it, None, GtsError(f"unexpected error {e!r}"[:200]), time.time() - t0)


# -- automatic checkout on restock -------------------------------------------------------------
def auto_on(state) -> bool:
    return bool(state.settings.get("gts_auto"))


async def _pay_safe(bot, client, label, title, pay, chat_ids, secs=None):
    import html as _html
    try:
        await pay_to_dm(bot, client, label, title, pay, chat_ids, secs=secs)
    except GtsError as e:
        await bot.tg.send(client, f"⚠️ GamesTheShop · {_html.escape(label)} · {_html.escape(title)}: order placed, "
                                  f"but no QR – {_html.escape(str(e))}", chat_ids=chat_ids)
    except Exception as e:  # noqa: BLE001
        log.exception("GamesTheShop QR failed")
        await bot.tg.send(client, f"⚠️ GamesTheShop · {_html.escape(label)} · {_html.escape(title)}: order placed, "
                                  f"but the QR step crashed – {_html.escape(repr(e))[:200]}", chat_ids=chat_ids)


async def auto_checkout(bot, client, items) -> None:
    """A restock with one or more cards: every logged-in account places ONE ORDER PER CARD
    (highest value first) and each order's QR goes to the owner's DM as soon as it's ready.
    Every account and every card runs at the same time (see order_cards for the safety retry);
    each QR is fetched in the background so the first one arrives quickly."""
    import html as _html
    state = bot.state
    owners = bot.users.recipients(admins_only=True)
    accts = {l: a for l, a in accounts(state).items() if a.get("access")}
    usable = sorted([i for i in items if (i.extra or {}).get("product_id")],
                    key=lambda i: -(i.denomination or 0))
    # remembered for /buy ("Last restock: …"), so a run that didn't work can be seen without logs
    run = {"ts": time.time(), "cards": [i.title for i in items], "accounts": len(accts), "ok": 0, "errors": []}
    state.settings["gts_last_run"] = run

    def problem(text):
        run["errors"].append(text)
        task = asyncio.create_task(bot.tg.send(client, f"⚠️ <b>GamesTheShop auto-buy</b>: {_html.escape(text)}", chat_ids=owners))
        bot._bg.add(task)
        task.add_done_callback(bot._bg.discard)

    if not accts:
        problem("cards restocked, but no account is logged in. Open /buy → ➕ Add account.")
        state.save()
        return
    if not usable:
        problem(f"cards restocked, but the bot couldn't read their product id ({', '.join(i.title for i in items)[:150]}).")
        state.save()
        return
    items = usable
    log.info("GamesTheShop auto-buy: %d account(s) × %d card(s)", len(accts), len(items))
    note = asyncio.create_task(bot.tg.send(client, f"🛒 <b>Auto-buy started</b>: {len(accts)} account{'s' if len(accts) != 1 else ''}"
                                                   f" × {len(items)} card{'s' if len(items) != 1 else ''}. QRs follow…",
                                           chat_ids=owners))
    bot._bg.add(note)
    note.add_done_callback(bot._bg.discard)
    recent = state.settings.setdefault("gts_recent", {})          # "label|variant" -> ts
    now = time.time()
    for k in [k for k, t in recent.items() if now - t > COOLDOWN]:
        recent.pop(k)

    async def one_account(label, acc):
        todo = []
        for it in items:
            key = f"{label}|{it.item_id}"
            if time.time() - recent.get(key, 0) < COOLDOWN:
                continue                                          # ordered this card a moment ago
            recent[key] = time.time()
            todo.append(it)

        async def done(it, pay, err, secs):
            if err is not None:
                problem(f"{label} · {it.title}: {err}")
                if "login expired" in str(err):
                    await mark_expired(bot, client, label)       # re-login button for next time
                return
            run["ok"] += 1
            log.info("GamesTheShop: %s ordered %s in %.1fs (%s)", label, it.title, secs, pay.get("paytmOrderId"))
            task = asyncio.create_task(_pay_safe(bot, client, label, it.title, pay, owners, secs=secs))
            bot._bg.add(task)
            task.add_done_callback(bot._bg.discard)

        if todo:
            await order_cards(client, state, acc, todo, done)   # all this account's cards at once

    await asyncio.gather(*(one_account(l, a) for l, a in accts.items()))
    state.save()


# -- login watchdog: find expired logins before a restock does -------------------------------
LOGIN_CHECK_EVERY = 30 * 60
# The login returns access + refresh JWTs (Django SimpleJWT style). These are the usual refresh
# addresses for that; if none works, the owner is asked to log in again with an OTP.
REFRESH_PATHS = ("/auth/token/refresh/", "/auth/refresh/")


async def _try_refresh(acc: dict) -> bool:
    """Get a new access key with the refresh key, silently. True if it worked."""
    if not acc.get("refresh"):
        return False
    for path in REFRESH_PATHS:
        try:
            r = await _post(None, path, json={"refresh": acc["refresh"]})
        except GtsError:
            continue
        if r.status_code in (200, 201):
            try:
                d = r.json()
            except ValueError:
                continue
            if d.get("access"):
                acc["access"] = d["access"]
                if d.get("refresh"):
                    acc["refresh"] = d["refresh"]
                log.info("GamesTheShop: login refreshed via %s", path)
                return True
    return False


async def check_login(acc: dict) -> str:
    """'ok', 'expired', or 'unknown' (network / server trouble: not the login's fault)."""
    try:
        r = await _get(None, "/customer/profile", headers=_auth(acc))
    except GtsError:
        return "unknown"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (401, 403):
        if await _try_refresh(acc):
            try:
                r2 = await _get(None, "/customer/profile", headers=_auth(acc))
                if r2.status_code == 200:
                    return "ok"
            except GtsError:
                return "unknown"
        return "expired"
    return "unknown"


async def mark_expired(bot, client, label: str) -> None:
    """Flag an account and tell the owner once, with a one-tap re-login button."""
    import html as _html
    acc = accounts(bot.state).get(label)
    if not acc:
        return
    acc["expired"] = True
    if acc.get("expired_told"):
        return
    acc["expired_told"] = time.time()
    bot.state.save()
    await bot.tg.send(client, f"🔑 <b>GamesTheShop login expired</b> for <b>{_html.escape(label)}</b>.\n"
                              "Auto-buy can't order with this account until you log in again. "
                              "Tap below: an OTP goes to its mobile number.",
                      chat_ids=bot.users.recipients(admins_only=True),
                      reply_markup={"inline_keyboard": [[{"text": f"🔑 Log in {label} again"[:40],
                                                          "callback_data": f"gb:relogin:{label}"[:64]}]]})


async def check_logins(bot, client) -> dict:
    """Check every logged-in account once. Returns {label: status}."""
    out = {}
    for label, acc in list(accounts(bot.state).items()):
        if not acc.get("access"):
            continue
        status = await check_login(acc)
        out[label] = status
        if status == "ok":
            acc.pop("expired", None)                  # working again (e.g. refreshed)
            acc.pop("expired_told", None)
            acc["checked"] = time.time()
        elif status == "expired":
            await mark_expired(bot, client, label)
    bot.state.save()
    return out


# -- did GamesTheShop actually record the payment? ---------------------------------------------
# The bot pays through Paytm directly, so the website never runs its own "payment done" step.
# After each payment we therefore read the order back from GamesTheShop and say what it shows.
# The order-detail address wasn't in the recording, so these read-only (GET) candidates are tried
# once and the one that answers is remembered. Nothing here changes an order.
ORDER_PATHS = ("/orders/{oid}/", "/orders/{oid}", "/orders/detail/{oid}/", "/customer/orders/{oid}/",
               "/customer/orders/{oid}", "/orders/", "/customer/orders/")
_ORDER_PATH: str | None = None
_ORDER_PATH_NONE = False          # every candidate failed: ask the owner for a recording, once

ORDER_POLL_EVERY = 20             # seconds between order-status reads
ORDER_WATCH_GRACE = 180           # keep watching this long after the QR expires

PAID_WORDS = ("paid", "success", "completed", "complete", "confirmed", "placed", "processing",
              "delivered", "dispatched", "shipped")
FAILED_WORDS = ("failed", "cancelled", "canceled", "expired", "declined", "refund")
PENDING_WORDS = ("pending", "awaiting", "initiated", "unpaid", "created", "incomplete")


def _status_of(obj, oid: str) -> tuple[str, str]:
    """(state, text) from an order payload. state: paid / failed / pending / unknown."""
    found: list[str] = []

    def walk(o, depth=0):
        if depth > 6:
            return
        if isinstance(o, dict):
            # a list of orders: only the one we asked about counts
            ids = {str(o.get(k)) for k in ("order_id", "id", "checkout_group_id", "paytmOrderId") if o.get(k)}
            if ids and oid and not any(oid in i or i in oid for i in ids) and any(
                    k in o for k in ("order_status", "payment_status", "status")):
                return
            for k, v in o.items():
                if isinstance(v, str) and v and ("status" in k.lower() or k.lower() in ("state", "payment")):
                    found.append(f"{k}={v}")
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:20]:
                walk(v, depth + 1)

    walk(obj)
    blob = " ".join(found).lower()
    text = ", ".join(found[:4])[:160]
    if not blob:
        return "unknown", text
    if any(w in blob for w in FAILED_WORDS):
        return "failed", text
    if any(w in blob for w in PENDING_WORDS):
        return "pending", text
    if any(w in blob for w in PAID_WORDS):
        return "paid", text
    return "unknown", text


async def order_status(acc: dict, order_id: str) -> tuple[str, str]:
    """Read one order back from GamesTheShop. (state, text); state 'unknown' = couldn't tell."""
    global _ORDER_PATH, _ORDER_PATH_NONE
    if _ORDER_PATH_NONE or not acc.get("access"):
        return "unknown", ""
    paths = [_ORDER_PATH] if _ORDER_PATH else list(ORDER_PATHS)
    for path in paths:
        try:
            r = await _get(None, path.format(oid=order_id), headers=_auth(acc))
        except GtsError:
            continue
        if r.status_code != 200:
            continue
        try:
            d = r.json()
        except ValueError:
            continue
        state, text = _status_of(d.get("data", d), order_id)
        if state != "unknown" or _ORDER_PATH:
            _ORDER_PATH = path
            return state, text
    if not _ORDER_PATH:
        _ORDER_PATH_NONE = True
        log.warning("GamesTheShop: no order-status endpoint found; can't confirm payments")
    return "unknown", ""


def _order_lines(p: dict) -> str:
    import html as _html
    return (f"👤 {_html.escape(str(p.get('label', '')))} · {_html.escape(str(p.get('title', '')))}\n"
            f"💰 ₹{_html.escape(str(p.get('amount', '?')))}\n"
            f"🧾 Paytm order: <code>{_html.escape(str(p.get('paytmOrderId', ''))[:60])}</code>\n"
            f"🆔 Shop order: <code>{_html.escape(str(p.get('order_id', ''))[:60])}</code>")


async def report_order(bot, client, p: dict, chat_ids: list[str], state: str, text: str) -> None:
    """Tell the owner what GamesTheShop shows for this order."""
    import html as _html
    detail = f"\n<i>{_html.escape(text)}</i>" if text else ""
    if state == "paid":
        msg = "✅ <b>Payment recorded by GamesTheShop</b>\n" + _order_lines(p) + detail
    elif state == "failed":
        msg = ("❌ <b>GamesTheShop says this order failed or was cancelled</b>\n" + _order_lines(p) + detail +
               "\nIf money left your account, it's normally refunded automatically. Keep the UPI reference.")
    elif state == "pending":
        msg = ("⚠️ <b>You paid, but GamesTheShop still shows this order as unpaid</b>\n" + _order_lines(p) + detail +
               "\nSend these details and your UPI reference (UTR) to GamesTheShop support so they can match "
               "the payment. Unmatched UPI payments are usually refunded automatically.")
    else:
        msg = ("❔ <b>Couldn't read this order's status from GamesTheShop</b>\n" + _order_lines(p) +
               "\nCheck <b>My orders</b> on the site. If it isn't there, send these details to their support.")
    await bot.tg.send(client, msg, chat_ids=chat_ids)


async def check_order_now(bot, client, key: str, chat_ids: list[str]) -> str:
    """🔄 button: read the order back right now and report it."""
    p = PENDING.get(key) or PAID_SEEN.get(key)
    if not p:
        raise GtsError("this payment is no longer being tracked")
    acc = accounts(bot.state).get(p.get("label")) or {}
    state, text = await order_status(acc, p.get("order_id") or p.get("paytmOrderId") or "")
    await report_order(bot, client, p, chat_ids, state, text)
    return state


PAID_SEEN: dict[str, dict] = {}   # orders already reported, so the 🔄 button still works after


async def watch_order(bot, client, key: str, chat_ids: list[str]) -> None:
    """After a payment is sent out: read the order back until it's paid, failed, or time runs out.
    A payment that GamesTheShop never records is the problem this is here to catch."""
    p = PENDING.get(key)
    if not p:
        return
    acc = accounts(bot.state).get(p.get("label")) or {}
    oid = p.get("order_id") or p.get("paytmOrderId") or ""
    deadline = time.time() + QR_MINUTES * 60 + ORDER_WATCH_GRACE   # a little past the QR's life
    last = "unknown"
    while time.time() < deadline:
        await asyncio.sleep(ORDER_POLL_EVERY)
        if key not in PENDING:                            # cancelled by the owner
            return
        try:
            state, text = await order_status(acc, oid)
        except Exception:  # noqa: BLE001
            log.exception("GamesTheShop order status failed")
            return
        if state == "unknown" and _ORDER_PATH_NONE:
            return                                        # no endpoint: nothing to poll
        last = state if state != "unknown" else last
        if state in ("paid", "failed"):
            PAID_SEEN[key] = dict(p)
            PENDING.pop(key, None)
            await report_order(bot, client, p, chat_ids, state, text)
            return
    # Time's up and it still isn't recorded. The bot can't know whether you paid, so this is a
    # quiet note (no notification sound) that reads right either way.
    import html as _html
    PAID_SEEN[key] = dict(p)
    if last == "unknown":
        return                                            # couldn't read the order at all: stay silent
    for cid in chat_ids:
        await bot.tg.api(client, "sendMessage", {
            "chat_id": cid, "parse_mode": "HTML", "disable_notification": True,
            "text": "⌛ <b>No payment recorded for this order</b>\n" + _order_lines(p) +
                    "\nFine if you didn't pay it. <b>If you did pay</b>, send these details and your UPI "
                    "reference (UTR) to GamesTheShop support – the payment didn't reach the order.",
            "reply_markup": {"inline_keyboard": [[{"text": "🔄 Check this order again",
                                                   "callback_data": f"gp:chk:{key}"}]]}})
