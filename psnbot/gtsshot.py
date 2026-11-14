"""Screenshot of a GamesTheShop order, taken on the server with a headless Chromium (Playwright).

GTSbuy orders are placed through the shop's API, so there's no browser tab to look at. For the
🔄 button and for "payment recorded", this opens the website logged in as the account that
placed the order (the site keeps its login in localStorage: `accessToken` and `user`, the same
keys the browser extension reads) and photographs the order page.

Optional and heavy (~150-300 MB RAM while it runs), so:
  - only installed when .env has GTS_SCREENSHOT=1 (deploy/install.sh then adds Playwright +
    Chromium); everywhere else this module reports "not installed" and nothing changes
  - Chromium starts per screenshot and closes straight after; one screenshot at a time
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
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    async with _LOCK:                                   # one Chromium at a time (small VM)
        try:
            return await asyncio.wait_for(_shoot(acc, order_id, orders_url, remember), TIMEOUT)
        except asyncio.TimeoutError:
            return None, f"the order page didn't load within {TIMEOUT}s"
        except Exception as e:  # noqa: BLE001 - a screenshot problem must never break payments
            log.exception("GamesTheShop screenshot failed")
            return None, f"screenshot failed: {e!r}"[:200]


async def _shoot(acc, order_id, orders_url, remember):
    from playwright.async_api import async_playwright

    if not os.access(os.path.expanduser("~"), os.W_OK):  # the service's home is read-only
        os.environ["HOME"] = tempfile.gettempdir()        # Playwright's driver writes a little there
    paths = []
    if orders_url:
        paths.append(orders_url)
    if remember and remember.get("gts_orders_path"):
        paths.append(remember["gts_orders_path"])
    paths += [p for p in CANDIDATES if p not in paths]
    oid = str(order_id or "")

    async with async_playwright() as pw:
        # HOME in /tmp: the service's own folder is read-only, and Chromium wants somewhere to write
        browser = await pw.chromium.launch(headless=True, env={**os.environ, "HOME": tempfile.gettempdir()},
                                           args=["--disable-dev-shm-usage", "--disable-gpu"])
        try:
            ctx = await browser.new_context(viewport={"width": 412, "height": 915}, device_scale_factor=2,
                                            locale="en-IN", timezone_id="Asia/Kolkata")
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
            await browser.close()
