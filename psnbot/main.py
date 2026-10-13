from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import random
import signal
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from .http import make_client
from .models import Item, StoreError, TitleFilter
from .state import State
from .controls import Controls
from .stores import REGISTRY
from .telegram import Telegram

log = logging.getLogger("psnbot")
IST = ZoneInfo("Asia/Kolkata")


def _now() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def _rupees(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    return f"₹{v:,.0f}" if v == int(v) else f"₹{v:,.2f}"


def _fmt_item(it: Item) -> str:
    denom = it.denomination
    head = f"<b>{html.escape(it.title)}</b>"
    lines = [head]
    if denom and (not it.price or float(it.price) != float(denom)):
        lines.append(f"💳 Card value: {_rupees(denom)}")
    price = f"💰 Price: {_rupees(it.price)}" if it.price else ""
    if price and it.mrp and it.price and float(it.mrp) > float(it.price):
        off = (1 - float(it.price) / float(it.mrp)) * 100
        price += f" <s>{_rupees(it.mrp)}</s> ({off:.0f}% off)"
    if price:
        lines.append(price)
    if it.note:
        lines.append(f"ℹ️ {html.escape(it.note)}")
    lines.append(f'🛒 <a href="{html.escape(it.url, quote=True)}">Buy now</a>')
    return "\n".join(lines)


def _sort_key(it: Item):
    return (it.denomination or 10**9, it.title)


class Bot:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        if not dry_run and not (token and chats):
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see .env.example)")
        if dry_run and not chats:
            chats = ["dry-run"]
        self.tg = Telegram(token, chats, dry_run=dry_run)
        self.state = State(cfg.get("state_file", "data/state.json"))
        self.filter = TitleFilter(cfg.get("title_filter", {}))
        self.error_alert_after = int(cfg.get("error_alert_after_failures", 5))
        self.store_timeout = float(cfg.get("store_timeout_seconds", 120))
        self.notify_oos = bool(cfg.get("notify_out_of_stock", False))

        self.stores = []
        for sid, scfg in (cfg.get("stores") or {}).items():
            scfg = scfg or {}
            if not scfg.get("enabled", True):
                continue
            if sid not in REGISTRY:
                log.warning("Unknown store '%s' in config — skipped", sid)
                continue
            store = REGISTRY[sid](scfg, self.filter)
            if hasattr(store, "known"):
                store.known.update(self.state.known.get(sid, {}))
            self.stores.append(store)
        if not self.stores:
            raise SystemExit("No stores enabled in config")
        self.controls = Controls(self)

    def is_muted(self, store_id: str) -> bool:
        return store_id in self.state.settings.get("muted", [])

    # ------------------------------------------------------------------
    async def run_cycle(self, client) -> None:
        first_run = self.state.is_new
        results = await asyncio.gather(*(self._check_store(client, s) for s in self.stores))
        restocked: dict[str, list[Item]] = {}
        sold_out: dict[str, list[Item]] = {}
        for store, items in zip(self.stores, results):
            if items is None:          # store failed this cycle -> keep previous state untouched
                continue
            self._apply(store, items, restocked, sold_out, full_listing=True, silent=first_run)
        if first_run:
            self.state.seeded = True
            await self._send_startup_summary(client)
        await self._notify(client, restocked, sold_out)
        self.state.save()

    async def run_fast_cycle(self, client) -> None:
        """Re-check only the priority listings (e.g. Amazon `fast_check`)."""
        if self.state.is_new:
            return   # let the first full cycle seed the state and send the summary
        restocked: dict[str, list[Item]] = {}
        sold_out: dict[str, list[Item]] = {}
        for store in self.stores:
            try:
                items = await asyncio.wait_for(store.fetch_fast(client), timeout=self.store_timeout)
            except Exception as e:  # noqa: BLE001 - errors are counted by the full cycle
                log.debug("%s: fast check failed: %s", store.name, e)
                continue
            full = getattr(store, "fast_is_full_listing", False)
            if items or (full and items is not None):
                self._apply(store, items, restocked, sold_out, full_listing=full, silent=False)
        await self._notify(client, restocked, sold_out)
        if restocked or sold_out:
            self.state.save()

    def _apply(self, store, items: list[Item], restocked, sold_out, *, full_listing: bool, silent: bool) -> None:
        """Compare fresh results with saved state and collect what changed."""
        now = time.time()
        seen_keys = set()
        # Muted stores (Telegram /menu) still update their state, they just don't notify.
        silent = silent or self.is_muted(store.id)
        for it in items:
            seen_keys.add(it.key)
            prev = self.state.items.get(it.key)
            prev_stock = prev.get("in_stock") if prev else None
            if it.in_stock is None:      # undetermined -> don't flip state
                if prev:
                    prev["last_seen"] = now
                continue
            if it.in_stock and prev_stock is not True and not silent:
                restocked.setdefault(store.name, []).append(it)
            if not it.in_stock and prev_stock is True and not silent:
                sold_out.setdefault(store.name, []).append(it)
            self.state.items[it.key] = {
                "store": it.store, "title": it.title, "url": it.url, "in_stock": it.in_stock,
                "price": it.price, "mrp": it.mrp, "last_seen": now,
                "last_change": now if (prev_stock != it.in_stock) else (prev or {}).get("last_change", now),
            }
        if full_listing:
            # Items that disappeared from a store's listing are treated as out of stock
            for key, rec in self.state.items.items():
                if rec.get("store") == store.id and key not in seen_keys and rec.get("in_stock"):
                    rec["in_stock"] = False
                    rec["last_change"] = now
        if hasattr(store, "known"):
            self.state.known[store.id] = dict(store.known)

    async def _notify(self, client, restocked, sold_out) -> None:
        if restocked:
            await self._send_restock(client, restocked)
        if sold_out and self.notify_oos:
            await self._send_sold_out(client, sold_out)

    async def _check_store(self, client, store) -> list[Item] | None:
        err = self.state.store_errors.setdefault(store.id, {"count": 0, "alerted": False})
        t0 = time.monotonic()
        try:
            items = await asyncio.wait_for(store.fetch(client), timeout=self.store_timeout)
        except (StoreError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - one store must not kill the bot
            msg = str(e) or e.__class__.__name__
            err["count"] += 1
            err["last_error"] = msg
            log.warning("%s: check failed (%d in a row): %s", store.name, err["count"], msg)
            if err["count"] >= self.error_alert_after and not err["alerted"]:
                err["alerted"] = True
                if not self.is_muted(store.id):
                    await self.tg.send(client, f"⚠️ <b>{store.name}</b> can't be checked "
                                               f"({err['count']} failures in a row).\n"
                                               f"<code>{html.escape(msg[:300])}</code>")
            return None
        if err["alerted"] and not self.is_muted(store.id):
            await self.tg.send(client, f"✅ <b>{store.name}</b> is being checked again.")
        err.update(count=0, alerted=False, last_error=None)
        in_stock = sum(1 for i in items if i.in_stock)
        log.info("%s: %d PSN card listings, %d in stock (%.1fs)", store.name, len(items), in_stock,
                 time.monotonic() - t0)
        return items

    # ------------------------------------------------------------------
    async def _send_restock(self, client, restocked: dict[str, list[Item]]) -> None:
        for store_name, items in restocked.items():
            body = "\n\n".join(_fmt_item(i) for i in sorted(items, key=_sort_key))
            await self.tg.send(client, f"🟢 <b>PlayStation gift card back in stock on {store_name}!</b>\n"
                                       f"<i>{_now()}</i>\n\n{body}")

    async def _send_sold_out(self, client, sold_out: dict[str, list[Item]]) -> None:
        for store_name, items in sold_out.items():
            names = "\n".join(f"• {html.escape(i.title)}" for i in sorted(items, key=_sort_key))
            await self.tg.send(client, f"🔴 Sold out on <b>{store_name}</b>:\n{names}")

    async def _send_startup_summary(self, client) -> None:
        active = [s for s in self.stores if not self.is_muted(s.id)]
        if not active:
            return   # everything muted via /menu
        by_store: dict[str, list[dict]] = {}
        for rec in self.state.items.values():
            if rec.get("in_stock") and not self.is_muted(rec["store"]):
                by_store.setdefault(rec["store"], []).append(rec)
        lines = [f"🤖 <b>PSN gift card tracker started</b>\n<i>{_now()}</i>",
                 f"Watching: {', '.join(s.name for s in active)}",
                 "Send /menu to turn store notifications on or off.", ""]
        if not by_store:
            lines.append("Nothing is in stock right now. I'll message you the moment something comes back.")
        else:
            lines.append("<b>Currently in stock:</b>")
            names = {s.id: s.name for s in self.stores}
            for sid, recs in by_store.items():
                lines.append(f"\n<b>{names.get(sid, sid)}</b>")
                for r in sorted(recs, key=lambda r: r.get("price") or 0):
                    lines.append(f'• <a href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a>'
                                 f" — {_rupees(r.get('price'))}")
            lines.append("\nFrom now on I'll only message you when something comes back in stock.")
        await self.tg.send(client, "\n".join(lines))

    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        interval = int(self.cfg.get("interval_seconds", 180))
        jitter = int(self.cfg.get("interval_jitter_seconds", 30))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        fast_interval = int(self.cfg.get("fast_interval_seconds", 60))

        async def loop(fn, every: int, spread: int, name: str):
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    await fn(client)
                except Exception:  # noqa: BLE001
                    log.exception("%s crashed; will retry next interval", name)
                sleep_for = max(10, every + random.randint(-spread, spread) - (time.monotonic() - t0))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

        async with make_client() as client:
            tasks = [loop(self.run_cycle, interval, jitter, "Full check")]
            if fast_interval > 0:
                tasks.append(loop(self.run_fast_cycle, fast_interval, max(1, fast_interval // 6), "Fast check"))
            if not self.tg.dry_run and self.cfg.get("telegram_menu", True):
                tasks.append(self.controls.run(client, stop))   # /menu, /status, /help
            running = [asyncio.create_task(t) for t in tasks]
            await stop.wait()
            for t in running:          # e.g. Telegram long-poll waits up to 50 s otherwise
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            self.state.save()
        log.info("Stopped.")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    ap = argparse.ArgumentParser(description="PlayStation gift card stock tracker → Telegram")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    ap.add_argument("--list", action="store_true", help="print every listing found right now and exit")
    ap.add_argument("--test-blinkit", action="store_true",
                    help="check Blinkit once, print what it sees and save the raw response to data/")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    os.chdir(Path(args.config).resolve().parent)
    load_dotenv(".env")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(Path(args.config).name)

    if args.list:
        asyncio.run(_list(cfg))
        return
    if args.test_blinkit:
        asyncio.run(_test_blinkit(cfg))
        return
    bot = Bot(cfg, dry_run=args.dry_run)
    if args.test_telegram:
        async def _t():
            async with make_client() as c:
                ok = await bot.tg.send(c, "✅ PSN gift card tracker can reach this chat.")
                raise SystemExit(0 if ok else 1)
        asyncio.run(_t())
    elif args.once:
        async def _o():
            async with make_client() as c:
                await bot.run_cycle(c)
        asyncio.run(_o())
    else:
        asyncio.run(bot.run_forever())


async def _test_blinkit(cfg: dict) -> None:
    from .stores.blinkit import Blinkit, read_stock
    scfg = dict((cfg.get("stores") or {}).get("blinkit") or {}, save_raw=True)
    b = Blinkit(scfg, TitleFilter(cfg.get("title_filter", {})))
    print(f"Location: pincode {scfg.get('pincode')}  lat={scfg.get('lat')} lon={scfg.get('lon')}")
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome") as s:
        for prid in b._prids():
            r = await s.post(f"https://blinkit.com/v1/layout/product/{prid}", headers=b._headers(prid), timeout=30)
            print(f"\nProduct {prid}: HTTP {r.status_code}, {len(r.text)} bytes")
            if r.status_code == 403 or "you have been blocked" in r.text.lower():
                print("  ❌ BLOCKED by Cloudflare — this machine's IP can't reach Blinkit.")
                continue
            try:
                data = r.json()
            except ValueError:
                print("  ❌ Not JSON. First 300 chars:", r.text[:300])
                continue
            b._save_raw(prid, data)
            try:
                stock, info = read_stock(data, prid)
            except StoreError as e:
                print("  ❌", e)
                continue
            print(f"  name={info['name']!r} price={info['price']} mrp={info['mrp']}")
            print(f"  signals: {info['signals'] or 'none'}")
            print("  RESULT:", {True: "✅ IN STOCK", False: "🔴 OUT OF STOCK", None: "❓ UNKNOWN"}[stock])
            print(f"  Raw response saved to data/blinkit_{prid}.json — send it to me if anything looks wrong.")


async def _list(cfg: dict) -> None:
    filt = TitleFilter(cfg.get("title_filter", {}))
    async with make_client() as client:
        for sid, scfg in (cfg.get("stores") or {}).items():
            scfg = scfg or {}
            if not scfg.get("enabled", True) or sid not in REGISTRY:
                continue
            store = REGISTRY[sid](scfg, filt)
            try:
                items = await store.fetch(client)
            except Exception as e:  # noqa: BLE001
                print(f"\n== {store.name}: ERROR {e}")
                continue
            print(f"\n== {store.name}: {len(items)} listings")
            for it in sorted(items, key=_sort_key):
                flag = {True: "IN STOCK ", False: "out      ", None: "unknown  "}[it.in_stock]
                print(f"  {flag} {_rupees(it.price):>9}  {it.title}\n            {it.url}")


if __name__ == "__main__":
    main()
