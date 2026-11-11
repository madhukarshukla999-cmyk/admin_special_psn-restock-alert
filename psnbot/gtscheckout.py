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


async def _request(method, path, *, json=None, params=None, headers=None, session=None):
    h = {"Accept": "application/json, text/plain, */*", **SITE, **(headers or {})}
    try:
        if session is not None:                       # reuse the open connection
            return await session.request(method, f"{API}{path}", json=json, params=params, headers=h, timeout=12)
        async with _session() as s:                   # one-off call
            return await s.request(method, f"{API}{path}", json=json, params=params, headers=h, timeout=12)
    except GtsError:
        raise
    except Exception as e:  # noqa: BLE001 - network/curl errors surface as a clean message
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

    async with _session() as s:                       # one open connection for all the calls below
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


async def send_photo(client, token: str, chat_id: str, png: bytes, caption: str) -> int | None:
    """Telegram sendPhoto (multipart). Returns the message id."""
    try:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                              files={"photo": ("qr.png", png, "image/png")}, timeout=30)
        j = r.json()
        return (j.get("result") or {}).get("message_id") if j.get("ok") else None
    except Exception as e:  # noqa: BLE001
        log.warning("sendPhoto failed: %r", e)
        return None


async def pay_to_dm(bot, client, label: str, title: str, pay: dict, chat_ids: list[str], secs: float | None = None) -> str:
    """Send the order's UPI QR (or the UPI link if no QR library) to the owner. Returns a status line.
    `secs` = how long the order took, shown in the caption so we can see the real speed."""
    import html as _html
    link = await paytm_upi_link(pay)
    amount = pay.get("amount") or "?"
    minutes = QR_MINUTES
    speed = f"⚡ Ordered in {secs:.1f}s\n" if secs is not None else ""
    caption = (f"💳 <b>GamesTheShop</b> · {_html.escape(title)}\n"
               f"👤 Account: {_html.escape(label)}\n"
               f"💰 ₹{_html.escape(str(amount))} · 🧾 {_html.escape(str(pay.get('paytmOrderId', ''))[:40])}\n"
               f"{speed}\n"
               f"⏳ Pay within ~{minutes} min. Scan with any UPI app and enter your PIN.\n"
               "Same phone: save the image → UPI app → Scan QR → gallery.")
    png = qr_png(link)
    sent = []
    for cid in chat_ids:
        mid = await send_photo(client, bot.tg.token, cid, png, caption) if png else None
        if mid:
            sent.append((cid, mid))
        else:                                       # no QR library / photo failed: send the link as text
            await bot.tg.send(client, caption.split("\n\n")[0] + "\n\nUPI payment link (copy into your UPI app):\n"
                              f"<code>{_html.escape(link)}</code>", chat_ids=[cid])
    for cid, mid in sent:                          # delete the QR once it can't be paid any more
        async def drop(cid=cid, mid=mid):
            await bot.tg.api(client, "deleteMessage", {"chat_id": cid, "message_id": mid})
        bot._later(minutes * 60, drop())
    return "QR sent to your DM" if sent else "UPI link sent to your DM"


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
    Accounts run in parallel; within an account the orders go one after another (buy-now cart is
    per account), while each QR is fetched in the background so the first one arrives quickly."""
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
        for it in items:
            key = f"{label}|{it.item_id}"
            if time.time() - recent.get(key, 0) < COOLDOWN:
                continue                                          # ordered this card a moment ago
            recent[key] = time.time()
            c0 = time.time()
            try:
                pay = await checkout(client, state, acc, it)
            except GtsError as e:
                problem(f"{label} · {it.title}: {e}")
                if "login expired" in str(e):
                    return                                        # no point trying the other cards
                continue
            except Exception as e:  # noqa: BLE001 - never fail silently: tell the owner what broke
                log.exception("GamesTheShop auto-buy crashed for %s / %s", label, it.title)
                problem(f"{label} · {it.title}: unexpected error {e!r}"[:300])
                continue
            run["ok"] += 1
            secs = time.time() - c0
            log.info("GamesTheShop: %s ordered %s in %.1fs (%s)", label, it.title, secs, pay.get("paytmOrderId"))
            task = asyncio.create_task(_pay_safe(bot, client, label, it.title, pay, owners, secs=secs))
            bot._bg.add(task)
            task.add_done_callback(bot._bg.discard)

    await asyncio.gather(*(one_account(l, a) for l, a in accts.items()))
    state.save()
