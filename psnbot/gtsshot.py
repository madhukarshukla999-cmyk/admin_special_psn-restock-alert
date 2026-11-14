"""Screenshot of a GamesTheShop order, taken on the server with a headless Chromium (Playwright).

GTSbuy orders are placed through the shop's API, so there's no browser tab to look at. For the
🔄 button and for "payment recorded", this opens the website logged in as the account that
placed the order (the site keeps its login in localStorage: `accessToken` and `user`, the same
keys the browser extension reads) and photographs the order page.

Optional (~150-300 MB RAM for the browser), so:
  - only installed when .env has GTS_SCREENSHOT=1 (deploy/install.sh then adds Playwright +
    Chromium); everywhere else this module reports "not installed" and nothing changes
  - one Chromium stays open (started with the bot, restarted if it crashes); each screenshot
    gets its own private window, so it takes well under a second to start
  - goes through GTS_PROXY (WARP) like the GTSbuy API calls
The order page's address isn't known for sure, so a few likely ones are tried; the first that
shows this order (or an orders list) is used and remembered. Set `gts_orders_url` in config.yaml
(e.g. https://www.gamestheshop.com/my-orders) to skip the guessing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)

SITE = "https://www.gamestheshop.com"
CANDIDATES = ("/orders/{oid}", "/order/{oid}", "/my-orders/{oid}", "/account/orders/{oid}",
              "/my-orders", "/orders", "/account/orders", "/profile/orders", "/my-account/orders")
TIMEOUT = 45                     # seconds for the whole screenshot
_LOCK: asyncio.Lock | None = None


def enabled() -> bool:
    return os.environ.get("GTS_SCREENSHOT", "").strip().lower() in ("1", "true", "yes", "on")


def available() -> tuple[bool, str]:
    if not enabled():
        return False, "server screenshots are off (add GTS_SCREENSHOT=1 to .env and run install.sh)"
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False, "Playwright isn't installed (run: sudo bash deploy/install.sh)"
    return True, ""


def _login_script(acc: dict) -> str:
    """Runs before the site's own code, so the site starts already logged in as this account."""
    user = {"id": acc.get("user_id") or "", "mobile_number": acc.get("mobile") or "",
            **({"name": acc["name"]} if acc.get("name") else {})}
    items = {"accessToken": acc.get("access") or "", "user": json.dumps(user)}
    if acc.get("refresh"):
        items["refreshToken"] = acc["refresh"]
    return ("try{if(location.hostname==='www.gamestheshop.com'){const d=%s;"
            "for(const k in d){if(!localStorage.getItem(k)||k==='accessToken')localStorage.setItem(k,d[k]);}}}catch(e){}"
            % json.dumps(items))


async def order_page(acc: dict, order_id: str, orders_url: str | None = None,
                     remember: dict | None = None) -> tuple[bytes | None, str]:
    """(png, note). png None = no screenshot (note says why). `remember` (a settings dict) keeps
    the order-page path that worked, so later screenshots go straight there."""
    ok, why = available()
    if not ok:
        return None, why
    if not acc.get("access"):
        return None, "this account isn't logged in"
    try:
        return await asyncio.wait_for(_shoot(acc, order_id, orders_url, remember), TIMEOUT)
    except asyncio.TimeoutError:
        return None, f"the order page didn't load within {TIMEOUT}s"
    except Exception as e:  # noqa: BLE001 - a screenshot problem must never break payments
        log.exception("GamesTheShop screenshot failed")
        await close()                                   # maybe Chromium crashed: start fresh next time
        return None, f"screenshot failed: {e!r}"[:200]


# -- one Chromium that stays open -------------------------------------------------------------
# Starting Chromium takes seconds on a small VM; a new private window ("context") in an open
# browser takes ~0.1 s. So the browser is started once (at bot start, by warm()) and every
# screenshot gets its own context: separate logins, nothing shared between accounts.
_PW = None
_BROWSER = None


def _proxy() -> dict | None:
    """Same route as the GTSbuy API calls (GTS_PROXY, e.g. the WARP tunnel): the website sits
    behind the same 'Human Verification' check that blocks the server's own IP."""
    p = (os.environ.get("GTS_PROXY") or "").strip()
    return {"server": p.replace("socks5h://", "socks5://")} if p else None


async def _browser():
    global _PW, _BROWSER, _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    async with _LOCK:                                   # only one start at a time
        if _BROWSER is not None and _BROWSER.is_connected():
            return _BROWSER
        await _close_unlocked()
        from playwright.async_api import async_playwright
        if not os.access(os.path.expanduser("~"), os.W_OK):  # the service's home is read-only
            os.environ["HOME"] = tempfile.gettempdir()        # Playwright's driver writes a little there
        _PW = await async_playwright().start()
        _BROWSER = await _PW.chromium.launch(headless=True, proxy=_proxy(),
                                             env={**os.environ, "HOME": tempfile.gettempdir()},
                                             args=["--disable-dev-shm-usage", "--disable-gpu"])
        log.info("Headless Chromium started for GTSbuy screenshots%s", " (via GTS_PROXY)" if _proxy() else "")
        return _BROWSER


async def warm() -> bool:
    """Start the browser ahead of time (no-op if it's already running or the feature is off)."""
    if not available()[0]:
        return False
    try:
        await _browser()
        return True
    except Exception:  # noqa: BLE001
        log.warning("Couldn't start headless Chromium", exc_info=True)
        return False


async def _close_unlocked() -> None:
    global _PW, _BROWSER
    b, p, _BROWSER, _PW = _BROWSER, _PW, None, None
    for closer in ((b.close if b else None), (p.stop if p else None)):
        if closer:
            try:
                await closer()
            except Exception:  # noqa: BLE001 - already gone
                pass


async def close() -> None:
    if _LOCK is None:
        return await _close_unlocked()
    async with _LOCK:
        await _close_unlocked()


async def _shoot(acc, order_id, orders_url, remember):
    paths = []
    if orders_url:
        paths.append(orders_url)
    if remember and remember.get("gts_orders_path"):
        paths.append(remember["gts_orders_path"])
    paths += [p for p in CANDIDATES if p not in paths]
    oid = str(order_id or "")

    browser = await _browser()
    ctx = await browser.new_context(viewport={"width": 412, "height": 915}, device_scale_factor=2,
                                    locale="en-IN", timezone_id="Asia/Kolkata")
    try:
        await ctx.add_init_script(_login_script(acc))
        page = await ctx.new_page()
        fallback = None
        for path in paths:
            if "{oid}" in path and not oid:
                continue
            url = path.format(oid=oid)
            url = url if url.startswith("http") else SITE + url
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - slow page: look at what's there
                resp = None
            if resp is not None and resp.status >= 400:
                continue
            text = (await page.inner_text("body"))[:20000].lower()
            if "/login" in page.url or ("login" in text and "logout" not in text and "order" not in text):
                return None, "the website didn't accept this account's login (log in again in /buy)"
            if "404" in text[:300] or "page not found" in text or "not found" in text[:200]:
                continue
            has_order = bool(oid) and oid.lower() in text
            if has_order or "order" in text:
                if has_order and remember is not None and not orders_url:
                    remember["gts_orders_path"] = path
                if has_order:
                    return await page.screenshot(type="png"), f"GamesTheShop order page ({page.url.split('?')[0]})"
                fallback = fallback or (await page.screenshot(type="png"), f"GamesTheShop orders ({page.url.split('?')[0]})")
        if fallback:
            return fallback
        return None, "couldn't find the order page on the website (set gts_orders_url in config.yaml)"
    finally:
        await ctx.close()                           # the window goes, the browser stays open
