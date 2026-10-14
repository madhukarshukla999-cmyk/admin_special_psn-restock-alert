from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from . import history as hist
from . import updater
from .controls import Controls
from .http import make_client
from .models import Item, StoreError, TitleFilter
from .state import State
from .stores import REGISTRY
from .channels import Mailer, html_to_text, send_whatsapp
from .stores.base import Store
from .telegram import Telegram
from .users import Users
from . import warp as warpmod

log = logging.getLogger("psnbot")
IST = ZoneInfo("Asia/Kolkata")


def _now() -> str:
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %I:%M %p")


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


def store_enabled(sid: str, scfg: dict) -> bool:
    """config.yaml `enabled`, overridable per machine with env vars, e.g. on Heroku
    DISABLE_STORES=blinkit (blocked from cloud IPs) or ENABLE_STORES=gyftr,flipkart."""
    def env_list(name):
        return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}
    if sid in env_list("DISABLE_STORES"):
        return False
    if env_list("ENABLE_STORES"):
        return sid in env_list("ENABLE_STORES")
    return bool(scfg.get("enabled", True))


def _sort_key(it: Item):
    return (it.denomination or 10**9, it.title)


async def _sleep(seconds: float, *events: asyncio.Event) -> None:
    """Sleep, but wake early if any event is set."""
    waits = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, timeout=max(0.0, seconds), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in waits:
            t.cancel()


class Bot:
    def __init__(self, cfg: dict, dry_run: bool = False, task_name: str | None = None):
        self.cfg = cfg
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        if not dry_run and not (token and chats):
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see .env.example)")
        if dry_run and not chats:
            chats = ["dry-run"]
        self.tg = Telegram(token, chats, dry_run=dry_run)
        self.state = State(cfg.get("state_file", "data/state.json"))
        self.users = Users(self.state, chats)
        self.filter = TitleFilter(cfg.get("title_filter", {}))
        self.error_alert_after = int(cfg.get("error_alert_after_failures", 5))
        self.store_timeout = float(cfg.get("store_timeout_seconds", 120))
        self.notify_oos = bool(cfg.get("notify_out_of_stock", True))
        self.interval = int(cfg.get("interval_seconds", 180))
        self.fast_interval = int(cfg.get("fast_interval_seconds", 60))
        self.down_alert_minutes = float(cfg.get("down_alert_minutes", 10))
        self.summary_time = str(cfg.get("daily_summary_time", "21:00"))
        # On Heroku there's no git checkout; it redeploys from GitHub itself (automatic deploys).
        self.on_heroku = bool(os.environ.get("DYNO"))
        self.auto_update = bool(cfg.get("auto_update", True)) and not dry_run and not self.on_heroku
        self.update_hours = float(cfg.get("auto_update_hours", 6))
        self.repo = Path(__file__).resolve().parent.parent
        self.version = updater.current_version(self.repo)
        self.task_name = task_name
        self.started = time.time()
        self.restart_requested = False
        self._stop: asyncio.Event | None = None
        self._wake: asyncio.Event | None = None
        self._check_requesters: list[str] = []
        self._updating = False
        self._resched: dict[str, asyncio.Event] = {}
        self._bg: set[asyncio.Task] = set()
        self.mailer = Mailer()

        self.stores = []
        for sid, scfg in (cfg.get("stores") or {}).items():
            scfg = scfg or {}
            if not store_enabled(sid, scfg):
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
        self.names = {s.id: s.name for s in self.stores}
        self._locks = {s.id: asyncio.Lock() for s in self.stores}
        self.warp = None
        self.warp_stores = warpmod.stores() & set(self.names) if warpmod.enabled() else set()
        self._warp_client = None
        if self.warp_stores:
            self.warp = warpmod.Warp(self.state)
        self.controls = Controls(self)

    async def broadcast(self, client, text: str, *, store_id: str | None = None, pref: str | None = None,
                        admins_only: bool = False, subject: str | None = None) -> None:
        """Send to everyone who should get it. With `subject`, also WhatsApp + email (in the background)."""
        ids = self.users.recipients(store_id=store_id, pref=pref, admins_only=admins_only)
        if ids:
            await self.tg.send(client, text, chat_ids=ids)
        if subject and not admins_only:
            task = asyncio.create_task(self._send_extra(client, text, subject, store_id, pref))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def _send_extra(self, client, text: str, subject: str, store_id, pref) -> None:
        wa_text = f"{html_to_text(text, whatsapp=True)}"
        for cid, ch in self.users.channel_recipients("whatsapp", store_id, pref):
            ok, detail = await send_whatsapp(client, ch["phone"], ch["apikey"], wa_text)
            await self._channel_result(client, cid, "whatsapp", ch, ok, detail)
            await asyncio.sleep(1.5)        # CallMeBot rate limit
        for cid, ch in self.users.channel_recipients("email", store_id, pref):
            ok, detail = await self.mailer.send(ch["address"], subject, text)
            await self._channel_result(client, cid, "email", ch, ok, detail)

    async def _channel_result(self, client, cid, name, ch, ok, detail) -> None:
        if ok:
            ch["fail"] = 0
            return
        ch["fail"] = ch.get("fail", 0) + 1
        log.warning("%s alert to %s failed (%d): %s", name, cid, ch["fail"], detail)
        if ch["fail"] == 3:                  # tell the person once, on Telegram
            label = "WhatsApp" if name == "whatsapp" else "Email"
            await self.tg.send(client, f"⚠️ {label} alerts aren't getting through:\n"
                                       f"<code>{html.escape(detail[:200])}</code>\n"
                                       f"Check /{name}.", chat_ids=[cid])
        self.state.save()

    def request_check(self, cid: str) -> None:
        self._check_requesters.append(str(cid))
        if self._wake:
            self._wake.set()

    # -- check cycles -------------------------------------------------------
    def has_fast(self, store) -> bool:
        return type(store).fetch_fast is not Store.fetch_fast and bool(store.cfg.get("fast_check"))

    def default_intervals(self, store) -> tuple[int, int | None]:
        full = int(store.cfg.get("interval_seconds") or self.interval)
        fast = self.fast_interval if (self.has_fast(store) and self.fast_interval > 0) else None
        return full, fast

    def intervals(self, store) -> tuple[int, int | None]:
        """(full check seconds, priority check seconds or None). /cycles overrides the config."""
        full, fast = self.default_intervals(store)
        ov = self.state.settings.get("intervals", {}).get(store.id, {})
        full = int(ov.get("full", full))
        if "fast" in ov and self.has_fast(store):
            fast = int(ov["fast"]) or None
        return full, fast

    def set_interval(self, store_id: str, kind: str, seconds: int | None) -> None:
        """kind 'full' or 'fast'; seconds None = back to default, 0 (fast) = off."""
        ov = self.state.settings.setdefault("intervals", {}).setdefault(store_id, {})
        if seconds is None:
            ov.pop(kind, None)
        else:
            ov[kind] = int(seconds)
        if not ov:
            self.state.settings["intervals"].pop(store_id, None)
        self.state.save()
        if store_id in self._resched:
            self._resched[store_id].set()

    # ------------------------------------------------------------------
    async def run_cycle(self, client) -> None:
        """Check every store now (startup, /check, after being offline)."""
        t0 = time.monotonic()
        first_run = self.state.is_new
        restocked: dict[str, list[Item]] = {}
        sold_out: dict[str, list[tuple[Item, float | None]]] = {}

        async def one(store):
            async with self._locks[store.id]:
                items = await self._check_store(client, store)
                if items is not None:
                    self._apply(store, items, restocked, sold_out, full_listing=True, silent=first_run)
                return items

        results = await asyncio.gather(*(one(s) for s in self.stores))
        if first_run:
            self.state.seeded = True
            await self._send_startup_summary(client)
        await self._notify(client, restocked, sold_out)
        self.state.settings["last_full_check"] = time.time()
        self.state.save()
        if self._check_requesters:
            ids, self._check_requesters = sorted(set(self._check_requesters)), []
            n = sum(1 for r in self.state.items.values() if r.get("in_stock"))
            failed = [s.name for s, r in zip(self.stores, results) if r is None]
            msg = (f"✅ Checked {len(self.stores)} stores in {time.monotonic() - t0:.0f} s. "
                   f"{n} card{'s' if n != 1 else ''} in stock" + (" (see /status)." if n else ".")
                   + (f"\n⚠️ Couldn't check: {', '.join(failed)}" if failed else ""))
            await self.tg.send(client, msg, chat_ids=ids)

    async def run_store(self, client, store, fast: bool = False) -> None:
        """One scheduled check of one store (full, or just its priority listings)."""
        async with self._locks[store.id]:
            if fast:
                try:
                    items = await asyncio.wait_for(store.fetch_fast(self._client_for(store, client)),
                                                   timeout=self.store_timeout)
                except Exception as e:  # noqa: BLE001 - errors are counted by full checks
                    log.debug("%s: priority check failed: %s", store.name, e)
                    return
                full = getattr(store, "fast_is_full_listing", False)
                if not (items or (full and items is not None)):
                    return
                log.info("%s: priority check, %d listing(s), %d in stock", store.name, len(items),
                         sum(1 for i in items if i.in_stock))
            else:
                items, full = await self._check_store(client, store), True
                if items is None:
                    self.state.save()
                    return
            restocked: dict[str, list[Item]] = {}
            sold_out: dict[str, list[tuple[Item, float | None]]] = {}
            self._apply(store, items, restocked, sold_out, full_listing=full, silent=self.state.is_new)
            await self._notify(client, restocked, sold_out)
            self.state.save()

    async def _store_loop(self, client, store) -> None:
        stop, ev = self._stop, self._resched[store.id]
        jitter_cap = int(self.cfg.get("interval_jitter_seconds", 30))

        def spread(sec):
            j = min(jitter_cap, sec // 10)
            return sec + random.randint(-j, j) if j else sec

        full, fast = self.intervals(store)
        now = time.time()
        next_full = now + spread(full)          # the startup check just did a full check
        next_fast = now + fast if fast else None
        while not stop.is_set():
            now = time.time()
            try:
                if now >= next_full:
                    await self.run_store(client, store)
                    full, fast = self.intervals(store)
                    next_full = time.time() + spread(full)
                    next_fast = time.time() + fast if fast else None   # a full check covers them too
                elif next_fast and now >= next_fast:
                    await self.run_store(client, store, fast=True)
                    next_fast = time.time() + fast
            except Exception:  # noqa: BLE001
                log.exception("%s check crashed; will retry", store.name)
                next_full = max(next_full, time.time() + 30)
            wake_at = min(next_full, next_fast or float("inf"))
            await _sleep(wake_at - time.time(), stop, ev)
            if ev.is_set():                     # /cycles changed this store
                ev.clear()
                full, fast = self.intervals(store)
                now = time.time()
                next_full = min(next_full, now + full)
                next_fast = min(next_fast or float("inf"), now + fast) if fast else None

    async def _scheduler(self, client) -> None:
        try:
            await self.run_cycle(client)        # startup: everything once, then each store on its own cycle
        except Exception:  # noqa: BLE001
            log.exception("Startup check crashed")
        await asyncio.gather(*(self._store_loop(client, s) for s in self.stores),
                             self._manual_check_loop(client))

    async def _manual_check_loop(self, client) -> None:
        while not self._stop.is_set():
            await _sleep(3600, self._stop, self._wake)
            if self._wake.is_set() and not self._stop.is_set():
                self._wake.clear()
                try:
                    await self.run_cycle(client)
                except Exception:  # noqa: BLE001
                    log.exception("Check-now crashed")

    def _apply(self, store, items: list[Item], restocked, sold_out, *, full_listing: bool, silent: bool) -> None:
        """Compare fresh results with saved state, record history, collect what changed.

        `silent` is only used for the very first check (nothing to compare with yet).
        Per-person muting happens when sending, so state and history stay complete.
        """
        now = time.time()
        seen_keys = set()
        events = self.state.history
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
                hist.record_restock(events, it.key, store.id, it.title, it.price, now)
            if not it.in_stock and prev_stock is True:
                lasted = hist.record_soldout(events, it.key, now)
                if not silent:
                    sold_out.setdefault(store.name, []).append((it, lasted))
            self.state.items[it.key] = {
                "store": it.store, "title": it.title, "url": it.url, "in_stock": it.in_stock,
                "price": it.price, "mrp": it.mrp, "last_seen": now,
                "last_change": now if (prev_stock != it.in_stock) else (prev or {}).get("last_change", now),
            }
        if full_listing:
            # Items that disappeared from a store's listing are treated as sold out
            for key, rec in self.state.items.items():
                if rec.get("store") == store.id and key not in seen_keys and rec.get("in_stock"):
                    rec["in_stock"] = False
                    rec["last_change"] = now
                    lasted = hist.record_soldout(events, key, now)
                    if not silent:
                        gone = Item(store=store.id, item_id=key.split(":", 1)[1], title=rec["title"],
                                    url=rec["url"], in_stock=False, price=rec.get("price"))
                        sold_out.setdefault(store.name, []).append((gone, lasted))
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
            items = await asyncio.wait_for(store.fetch(self._client_for(store, client)), timeout=self.store_timeout)
        except (StoreError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 - one store must not kill the bot
            msg = str(e) or e.__class__.__name__
            err["count"] += 1
            err["last_error"] = msg
            log.warning("%s: check failed (%d in a row): %s", store.name, err["count"], msg)
            if err["count"] >= self.error_alert_after and not err["alerted"]:
                err["alerted"] = True
                await self.broadcast(client, f"⚠️ <b>{store.name}</b> can't be checked "
                                             f"({err['count']} failures in a row).\n"
                                             f"<code>{html.escape(msg[:300])}</code>",
                                     store_id=store.id, admins_only=True)
            return None
        if err["alerted"]:
            await self.broadcast(client, f"✅ <b>{store.name}</b> is being checked again.",
                                 store_id=store.id, admins_only=True)
        took = time.monotonic() - t0
        err.update(count=0, alerted=False, last_error=None, last_ok=time.time(), took=round(took, 1))
        in_stock = sum(1 for i in items if i.in_stock)
        log.info("%s: %d PSN card listings, %d in stock (%.1fs)", store.name, len(items), in_stock, took)
        return items

    # ------------------------------------------------------------------
    async def _send_restock(self, client, restocked: dict[str, list[Item]]) -> None:
        for store_name, items in restocked.items():
            body = "\n\n".join(_fmt_item(i) for i in sorted(items, key=_sort_key))
            await self.broadcast(client, f"🟢 <b>PlayStation gift card back in stock on {store_name}!</b>\n"
                                         f"<i>{_now()}</i>\n\n{body}", store_id=items[0].store,
                                 subject=f"🟢 PlayStation card back in stock on {store_name}")

    async def _send_sold_out(self, client, sold_out: dict[str, list[tuple[Item, float | None]]]) -> None:
        for store_name, pairs in sold_out.items():
            lines = []
            for it, lasted in sorted(pairs, key=lambda p: _sort_key(p[0])):
                extra = f" — was in stock for {hist.fmt_duration(lasted)}" if lasted is not None else ""
                lines.append(f"• {html.escape(it.title)}{extra}")
            await self.broadcast(client, f"🔴 <b>Sold out on {store_name}</b> · <i>{_now()}</i>\n" + "\n".join(lines),
                                 store_id=pairs[0][0].store, pref="soldout",
                                 subject=f"🔴 PlayStation card sold out on {store_name}")

    def _summary_for(self, cid: str) -> str | None:
        muted = self.users.muted(cid)
        active = [s for s in self.stores if s.id not in muted]
        if not active:
            return None
        by_store: dict[str, list[dict]] = {}
        for rec in self.state.items.values():
            if rec.get("in_stock") and rec["store"] not in muted:
                by_store.setdefault(rec["store"], []).append(rec)
        lines = [f"🤖 <b>PSN gift card tracker started</b>\n<i>{_now()}</i>",
                 f"Watching: {', '.join(s.name for s in active)}",
                 "Send /menu to choose stores, /help for all commands.", ""]
        if not by_store:
            lines.append("Nothing is in stock right now. I'll message you the moment something comes back.")
        else:
            lines.append("<b>Currently in stock:</b>")
            for sid, recs in by_store.items():
                lines.append(f"\n<b>{self.names.get(sid, sid)}</b>")
                for r in sorted(recs, key=lambda r: r.get("price") or 0):
                    lines.append(f'• <a href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a>'
                                 f" — {_rupees(r.get('price'))}")
            lines.append("\nFrom now on I'll only message you when something changes.")
        return "\n".join(lines)

    async def _send_startup_summary(self, client) -> None:
        for cid in self.users.recipients():
            text = self._summary_for(cid)
            if text:
                await self.tg.send(client, text, chat_ids=[cid])

    # -- daily summary -----------------------------------------------------
    def daily_summary_text(self, cid: str, now: float | None = None) -> str:
        now = now or time.time()
        muted = self.users.muted(cid)
        evs = [e for e in hist.recent(self.state.history, 86400, now) if e["store"] not in muted]
        lines = [f"📰 <b>Daily summary</b> · {datetime.fromtimestamp(now, IST).strftime('%d %b %Y')}", ""]
        if evs:
            lines.append(f"<b>{len(evs)} restock{'s' if len(evs) != 1 else ''} in the last 24 h</b>")
            for e in sorted(evs, key=lambda e: e["start"])[:20]:
                lasted = "still in stock" if e.get("end") is None else f"lasted {hist.fmt_duration(e['end'] - e['start'])}"
                lines.append(f"• {_when(e['start'])} · {self.names.get(e['store'], e['store'])} · "
                             f"{html.escape(e['title'][:50])} · {lasted}")
            if len(evs) > 20:
                lines.append(f"…and {len(evs) - 20} more (/history)")
        else:
            lines.append("No restocks in the last 24 h.")
        n = sum(1 for r in self.state.items.values() if r.get("in_stock") and r["store"] not in muted)
        lines.append(f"\n📦 In stock right now: {n}" + (" (/status)" if n else ""))
        bad = [s.name for s in self.stores if (self.state.store_errors.get(s.id) or {}).get("count", 0)
               >= self.error_alert_after and s.id not in muted]
        if bad:
            lines.append(f"⚠️ Can't be checked: {', '.join(bad)}")
        down = sum(min(e, now) - max(s, now - 86400) for s, e in self.state.settings.get("downtime", [])
                   if e > now - 86400)
        if down > 60:
            lines.append(f"😴 I was offline for {hist.fmt_duration(down)} in the last 24 h, so restocks then were missed.")
        lines.append("\n⏰ /besttime shows when each store usually restocks.")
        return "\n".join(lines)

    async def _daily_summary_loop(self, client) -> None:
        try:
            hh, mm = (int(x) for x in self.summary_time.split(":"))
        except ValueError:
            log.error("daily_summary_time must look like 21:00; daily summary disabled")
            return
        while not self._stop.is_set():
            now = datetime.now(IST)
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            # Send at the set time, or up to 3 h late if the PC was off/asleep then.
            if (self.state.seeded and due <= now < due.replace(hour=min(23, hh + 3), minute=59)
                    and self.state.settings.get("last_summary") != today):
                self.state.settings["last_summary"] = today
                self.state.save()
                for cid in self.users.recipients(pref="summary"):
                    await self.tg.send(client, self.daily_summary_text(cid), chat_ids=[cid])
            await _sleep(60, self._stop)

    # -- "bot was down" -----------------------------------------------------
    async def _heartbeat_loop(self, client) -> None:
        while not self._stop.is_set():
            now = time.time()
            last = self.state.settings.get("last_alive")
            if last and self.state.seeded and now - last > self.down_alert_minutes * 60:
                dt = self.state.settings.setdefault("downtime", [])
                dt.append([last, now])
                dt[:] = [d for d in dt if d[1] > now - 7 * 86400]
                await self.broadcast(client,
                    f"😴 <b>I was offline</b> from {_when(last)} to {_when(now)} "
                    f"({hist.fmt_duration(now - last)}).\nThe PC was probably off, asleep or without internet, "
                    "so any restocks in that time were missed. Checking every store now.")
                if self._wake:
                    self._wake.set()
            self.state.settings["last_alive"] = now
            self.state.save()
            if self.warp and await asyncio.to_thread(self.warp.ensure):
                self._use_tunnel()
            await _sleep(30, self._stop)

    # -- auto-update ---------------------------------------------------------
    async def run_update(self, client, manual_cid: str | None = None) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            res = await asyncio.to_thread(updater.check_and_update, self.repo)
            st = self.state.settings
            st["last_update_check"] = time.time()
            reply = None
            if res.status == "updated":
                new = updater.current_version(self.repo)
                changes = html.escape(res.changes[:1500]) or "• (no description)"
                how = updater.restart_mode(self.task_name)
                tail = ("Restarting now…" if how != "manual" else
                        "Close the bot window and start it again to use the new version.")
                await self.broadcast(client, f"⬆️ <b>Updated</b> {html.escape(self.version)} → "
                                             f"{html.escape(new)}\n{changes}\n\n{tail}", admins_only=True)
                st.pop("last_update_error", None)
                self.state.save()
                if how != "manual":
                    self.restart_requested = True
                    self._stop.set()
                return
            if res.status == "up-to-date":
                reply = f"✅ Already up to date ({html.escape(self.version)})."
            else:
                reply = f"{'⚠️' if res.status == 'error' else 'ℹ️'} Auto-update: {html.escape(res.message)}"
                if not manual_cid and st.get("last_update_error") != res.message:
                    st["last_update_error"] = res.message      # tell admins once per distinct problem
                    await self.broadcast(client, reply, admins_only=True)
            self.state.save()
            if manual_cid and reply:
                await self.tg.send(client, reply, chat_ids=[manual_cid])
        finally:
            self._updating = False

    async def _update_loop(self, client) -> None:
        await _sleep(120, self._stop)          # let startup settle first
        while not self._stop.is_set():
            last = self.state.settings.get("last_update_check") or 0
            wait = last + self.update_hours * 3600 - time.time()
            if wait <= 0:
                try:
                    await self.run_update(client)
                except Exception:  # noqa: BLE001
                    log.exception("Auto-update failed")
                wait = self.update_hours * 3600
            await _sleep(min(wait, 3600), self._stop)

    # ------------------------------------------------------------------
    async def start_tunnel(self) -> None:
        if self.warp and not self.warp.alive():
            proxy = await asyncio.to_thread(self.warp.start)
            if proxy:
                self._use_tunnel()

    def _use_tunnel(self) -> None:
        if "blinkit" in self.warp_stores:
            os.environ["BLINKIT_PROXY"] = warpmod.PROXY_URL
        if self._warp_client is None and self.warp_stores - {"blinkit"}:
            self._warp_client = make_client(proxy=warpmod.HTTPX_PROXY)
        log.info("Through WARP: %s", ", ".join(self.names[s] for s in sorted(self.warp_stores)))

    def _client_for(self, store, client):
        """Stores in WARP_STORES use the tunnel (if it's up); everything else connects directly."""
        if store.id in self.warp_stores and self._warp_client is not None and self.warp.alive():
            return self._warp_client
        return client

    async def run_forever(self) -> None:
        self._stop = stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._resched = {s.id: asyncio.Event() for s in self.stores}
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass

        async def guarded(coro_fn, name):
            try:
                await coro_fn(client)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("%s stopped unexpectedly", name)

        log.info("Version %s", self.version)
        await self.start_tunnel()
        async with make_client() as client:
            tasks = [guarded(self._scheduler, "Store checks"),
                     guarded(self._heartbeat_loop, "Heartbeat"),
                     guarded(self._daily_summary_loop, "Daily summary")]
            if not self.tg.dry_run and self.cfg.get("telegram_menu", True):
                tasks.append(self.controls.run(client, stop))   # Telegram commands
            if self.auto_update:
                tasks.append(guarded(self._update_loop, "Auto-update"))
            running = [asyncio.create_task(t) for t in tasks]
            await stop.wait()
            for t in running:          # e.g. Telegram long-poll waits up to 50 s otherwise
                t.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            self.state.settings["last_alive"] = time.time()
            self.state.save()
        await self.stop_tunnel()
        log.info("Stopped.")

    async def stop_tunnel(self) -> None:
        if self._warp_client is not None:
            await self._warp_client.aclose()
            self._warp_client = None
        if self.warp:
            self.warp.stop()


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
    ap.add_argument("--log-file", help="also write logs to this file (rotated at 1 MB, 3 kept)")
    ap.add_argument("--import-state", metavar="FILE",
                    help="copy a state.json (history, people, settings) into the database (DATABASE_URL) and exit")
    ap.add_argument("--export-state", metavar="FILE", help="save the current state to a JSON file and exit")
    ap.add_argument("--task-name", help="Windows Task Scheduler task to re-run after an auto-update")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # Windows consoles/log files default to cp1252, which can't print ₹ or emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    os.chdir(Path(args.config).resolve().parent)
    load_dotenv(".env")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:          # pythonw.exe (no console window) has no stderr
        handlers.append(logging.StreamHandler())
    if args.log_file:
        from logging.handlers import RotatingFileHandler
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers or [logging.NullHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(Path(args.config).name)

    if args.import_state or args.export_state:
        _move_state(cfg, args.import_state, args.export_state)
        return
    if args.list:
        asyncio.run(_list(cfg))
        return
    if args.test_blinkit:
        asyncio.run(_test_blinkit(cfg))
        return
    bot = Bot(cfg, dry_run=args.dry_run, task_name=args.task_name)
    if args.test_telegram:
        async def _t():
            async with make_client() as c:
                ok = await bot.tg.send(c, "✅ PSN gift card tracker can reach this chat.")
                raise SystemExit(0 if ok else 1)
        asyncio.run(_t())
    elif args.once:
        async def _o():
            await bot.start_tunnel()
            try:
                async with make_client() as c:
                    await bot.run_cycle(c)
            finally:
                await bot.stop_tunnel()
        asyncio.run(_o())
    else:
        from .lock import AlreadyRunning, SingleInstance
        try:
            with SingleInstance(Path(cfg.get("state_file", "data/state.json")).parent / "bot.lock", wait=30):
                log.info("Bot started (pid %d). Press Ctrl+C to stop.", os.getpid())
                asyncio.run(bot.run_forever())
        except AlreadyRunning as e:
            log.error("%s", e)
            raise SystemExit(f"{e}\nStop the other copy first (Task Manager → pythonw.exe / python.exe).")
        if bot.restart_requested:
            how = updater.restart(args.task_name)
            log.info("Restarting after update (%s).", how)


def _move_state(cfg: dict, import_file: str | None, export_file: str | None) -> None:
    import json
    st = State(cfg.get("state_file", "data/state.json"))
    if export_file:
        Path(export_file).write_text(st.dumps(), encoding="utf-8")
        print(f"Exported state ({st.backend}) to {export_file}")
        return
    if st.backend != "postgres":
        raise SystemExit("Set DATABASE_URL first (heroku config:get DATABASE_URL -a <app>).")
    data = json.loads(Path(import_file).read_text(encoding="utf-8"))
    st.seeded = bool(data.get("seeded", bool(data.get("items"))))
    for k in ("items", "store_errors", "known", "settings", "history"):
        setattr(st, k, data.get(k, {} if k != "history" else []))
    st.settings.pop("tg_offset", None)       # the new copy starts reading Telegram fresh
    st.save()
    users = len(st.settings.get("users", {}))
    print(f"Imported into the database: {len(st.items)} cards, {len(st.history)} restock events, {users} people.")


async def _test_blinkit(cfg: dict) -> None:
    from .stores.blinkit import Blinkit, read_stock
    scfg = dict((cfg.get("stores") or {}).get("blinkit") or {}, save_raw=True)
    b = Blinkit(scfg, TitleFilter(cfg.get("title_filter", {})))
    print(f"Location: pincode {scfg.get('pincode')}  lat={scfg.get('lat')} lon={scfg.get('lon')}")
    from .stores.blinkit import proxy_url, session
    tunnel = None
    if warpmod.enabled():
        tunnel = warpmod.Warp(State(cfg.get("state_file", "data/state.json")))
        p = await asyncio.to_thread(tunnel.start)
        print("WARP tunnel:", "up" if p else f"FAILED ({tunnel.error})")
        if p:
            os.environ["BLINKIT_PROXY"] = p
    print("Proxy:", "yes" if proxy_url() else "none")
    async with session() as s:
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
    if tunnel:
        tunnel.stop()


async def _list(cfg: dict) -> None:
    filt = TitleFilter(cfg.get("title_filter", {}))
    tunnel, via = None, set()
    if warpmod.enabled():
        tunnel = warpmod.Warp(State(cfg.get("state_file", "data/state.json")))
        ok = await asyncio.to_thread(tunnel.start)
        via = warpmod.stores() if ok else set()
        print(f"WARP tunnel: {'up' if ok else 'FAILED (' + str(tunnel.error) + ')'}"
              + (f", used for: {', '.join(sorted(via))}" if via else ""))
        if ok and "blinkit" in via:
            os.environ["BLINKIT_PROXY"] = warpmod.PROXY_URL
    async with make_client() as direct, make_client(proxy=warpmod.HTTPX_PROXY) as tunneled:
        for sid, scfg in (cfg.get("stores") or {}).items():
            scfg = scfg or {}
            if not store_enabled(sid, scfg) or sid not in REGISTRY:
                continue
            store = REGISTRY[sid](scfg, filt)
            client = tunneled if sid in via else direct
            try:
                items = await store.fetch(client)
            except Exception as e:  # noqa: BLE001
                print(f"\n== {store.name}: ERROR {e}")
                continue
            print(f"\n== {store.name}: {len(items)} listings" + (" (via WARP)" if sid in via else ""))
            for it in sorted(items, key=_sort_key):
                flag = {True: "IN STOCK ", False: "out      ", None: "unknown  "}[it.in_stock]
                print(f"  {flag} {_rupees(it.price):>9}  {it.title}\n            {it.url}")
    if tunnel:
        tunnel.stop()


if __name__ == "__main__":
    main()
