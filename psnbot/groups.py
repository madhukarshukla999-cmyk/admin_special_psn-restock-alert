"""Post stock alerts to Telegram groups / channels the owner connects.

Connect: /panel → 📢 Groups → "Add to a group" (or channel). Telegram opens its picker; when the
owner adds the bot, it connects by itself (anyone else adding it: the bot leaves and tells the
owner). Each group chooses its stores. When a card sells out, it's crossed out in the post and the
post is deleted once every card in it is gone (or edited to "sold out" if Telegram no longer lets the
bot delete it). Settings live in state.settings["groups"], open posts in ["group_posts"].
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

log = logging.getLogger(__name__)
POST_KEEP = 3 * 86400          # forget posts older than this (Telegram: bots delete own group msgs ≤ 48 h)


class Groups:
    def __init__(self, bot):
        self.bot = bot
        self.tg = bot.tg
        self._username: str | None = None

    # -- storage -----------------------------------------------------------
    @property
    def all(self) -> dict[str, dict]:
        return self.bot.state.settings.setdefault("groups", {})

    @property
    def posts(self) -> list[dict]:
        return self.bot.state.settings.setdefault("group_posts", [])

    def _save(self) -> None:
        self.bot.state.save()

    async def username(self, client) -> str:
        if not self._username:
            me = await self.tg.api(client, "getMe", {}) or {}
            self._username = me.get("username") or ""
        return self._username

    # -- joining / leaving -------------------------------------------------
    async def on_member_update(self, client, upd: dict) -> None:
        """my_chat_member: the bot was added to / removed from a group or channel."""
        chat = upd.get("chat") or {}
        cid, title = str(chat.get("id")), chat.get("title") or "group"
        who = upd.get("from") or {}
        new = (upd.get("new_chat_member") or {}).get("status")
        owners = self.bot.users.recipients(admins_only=True)
        if chat.get("type") not in ("group", "supergroup", "channel"):
            return
        if new in ("left", "kicked"):
            if cid in self.all:
                self.all.pop(cid)
                self._save()
                await self.tg.send(client, f"📢 I was removed from <b>{html.escape(title)}</b> – it's disconnected.",
                                   chat_ids=owners)
            return
        if new not in ("member", "administrator"):
            return
        if not self.bot.users.is_admin(who.get("id")):
            # only the owner may connect the bot somewhere
            await self.tg.api(client, "leaveChat", {"chat_id": cid})
            name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or str(who.get("id"))
            await self.tg.send(client, f"🛡 <b>{html.escape(name)}</b> added me to <b>{html.escape(title)}</b>. "
                                       "Only you can connect me to groups, so I left.", chat_ids=owners)
            return
        fresh = cid not in self.all
        rec = self.all.setdefault(cid, {"title": title, "type": chat.get("type"), "stores": [s.id for s in self.bot.stores],
                                        "on": True, "autodelete": True, "added": time.time()})
        rec.update(title=title, type=chat.get("type"), admin=new == "administrator")
        self._save()
        if fresh:
            text, kb = self.group_view(cid)
            await self.tg.send(client, f"✅ <b>Connected to {html.escape(title)}</b>\n"
                                       "Stock alerts for the stores below will be posted there. Pick the ones you want:\n\n" + text,
                               chat_ids=[str(who.get("id"))], reply_markup=kb)
            await self.tg.send(client, "👋 Hi! I'll post <b>PlayStation gift card restocks</b> here with buy links. "
                                       "Posts disappear by themselves when the cards sell out.", chat_ids=[cid])

    async def migrate(self, old: str, new: str) -> None:
        """A group became a supergroup (new chat ID)."""
        if old in self.all:
            self.all[new] = self.all.pop(old)
            for p in self.posts:
                if p["chat"] == old:
                    p["chat"] = new
            self._save()

    async def disconnect(self, client, cid: str) -> None:
        rec = self.all.pop(cid, None)
        self.bot.state.settings["group_posts"] = [p for p in self.posts if p["chat"] != cid]
        self._save()
        if rec:
            await self.tg.send(client, "👋 I've been disconnected. Bye!", chat_ids=[cid])
            await self.tg.api(client, "leaveChat", {"chat_id": cid})

    # -- posting -----------------------------------------------------------
    def _render(self, post: dict) -> tuple[str, dict | None]:
        gone = set(post.get("gone", []))
        lines = [f"🟢 <b>PlayStation gift card back in stock on {html.escape(post['store_name'])}!</b>",
                 f"<i>{post['when']}</i>", ""]
        btns = []
        for it in post["items"]:
            if it["key"] in gone:
                lines.append(f"<s>{html.escape(it['title'])}</s> ❌ sold out")
            else:
                lines.append(it["html"])
                btns.append([{"text": f"🛒 Buy {it['label']}", "url": it["url"]}])
            lines.append("")
        if post.get("autodelete") and len(gone) < len(post["items"]):
            lines.append("<i>⏱ This post disappears when the cards sell out.</i>")
        return "\n".join(lines).strip(), ({"inline_keyboard": btns[:20]} if btns else None)

    def delay(self, g: dict) -> int:
        """Owner-set wait before posting (per group, else the default for all groups)."""
        d = g.get("delay")
        return int(d if d is not None else (self.bot.state.settings.get("delays") or {}).get("groups", 0))

    async def publish(self, client, store_name: str, items, fmt_item, when: str) -> None:
        sid = items[0].store
        if sid in self.bot.users.private():
            return                                  # private stores are for the owner only
        targets = [(cid, g) for cid, g in self.all.items() if g.get("on", True) and sid in g.get("stores", [])]
        if not targets:
            return
        now_t = [(c, g) for c, g in targets if not self.delay(g)]
        for c, g in targets:
            if self.delay(g):
                async def later(c=c, g=g, wait=self.delay(g)):
                    await asyncio.sleep(wait)
                    live = [i for i in items if (self.bot.state.items.get(i.key) or {}).get("in_stock")]
                    if live and c in self.all:
                        await self._post(client, store_name, live, fmt_item, when, [(c, g)])
                task = asyncio.create_task(later())
                self.bot._bg.add(task)
                task.add_done_callback(self.bot._bg.discard)
        if now_t:
            await self._post(client, store_name, items, fmt_item, when, now_t)

    async def _post(self, client, store_name, items, fmt_item, when, targets) -> None:
        rendered = [{"key": i.key, "title": i.title, "url": i.url, "html": fmt_item(i),
                     "label": f"₹{i.denomination:,}" if i.denomination else i.title[:25]} for i in items]
        for cid, g in targets:
            post = {"chat": cid, "store_name": store_name, "when": when, "items": rendered, "gone": [],
                    "autodelete": g.get("autodelete", True), "ts": time.time()}
            text, kb = self._render(post)
            payload = {"chat_id": cid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if kb:
                payload["reply_markup"] = kb
            res = await self.tg.api(client, "sendMessage", payload)
            if res and res.get("message_id"):
                post["mid"] = res["message_id"]
                self.posts.append(post)
                g["fail"] = 0
            else:
                g["fail"] = g.get("fail", 0) + 1
                if g["fail"] == 3:
                    await self.tg.send(client, f"⚠ I can't post in <b>{html.escape(g.get('title', cid))}</b>. "
                                               "Am I still a member, and allowed to send messages? (/panel → 📢 Groups)",
                                       chat_ids=self.bot.users.recipients(admins_only=True))
        cutoff = time.time() - POST_KEEP
        self.bot.state.settings["group_posts"] = [p for p in self.posts if p["ts"] >= cutoff][-300:]
        self._save()

    async def sold_out(self, client, keys: set[str]) -> None:
        """Cross out sold-out cards; delete a post once all its cards are gone."""
        changed = False
        keep = []
        for p in self.posts:
            hit = keys & {i["key"] for i in p["items"]} - set(p["gone"])
            if not hit:
                keep.append(p)
                continue
            changed = True
            p["gone"] = sorted(set(p["gone"]) | hit)
            all_gone = len(p["gone"]) >= len(p["items"])
            if all_gone and p.get("autodelete"):
                ok = await self.tg.api(client, "deleteMessage", {"chat_id": p["chat"], "message_id": p["mid"]})
                if ok:
                    continue                      # deleted – forget it
            text, kb = self._render(p)
            if all_gone:
                text = f"🔴 <b>Sold out</b> on {html.escape(p['store_name'])} · <i>{p['when']}</i>"
            payload = {"chat_id": p["chat"], "message_id": p["mid"], "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True, "reply_markup": kb or {"inline_keyboard": []}}
            await self.tg.api(client, "editMessageText", payload)
            if not all_gone:
                keep.append(p)
        if changed:
            self.bot.state.settings["group_posts"] = keep
            self._save()

    async def test(self, client, cid: str) -> bool:
        res = await self.tg.api(client, "sendMessage", {
            "chat_id": cid, "parse_mode": "HTML",
            "text": "🧪 <b>Test alert</b> from the PSN stock bot – real restocks look like this, with buy buttons.\n"
                    "<i>⏱ This test disappears in 30 seconds, like a sold-out post.</i>"})
        if not res:
            return False

        async def later():
            await asyncio.sleep(30)
            await self.tg.api(client, "deleteMessage", {"chat_id": cid, "message_id": res["message_id"]})
        task = asyncio.create_task(later())
        self.bot._bg.add(task)
        task.add_done_callback(self.bot._bg.discard)
        return True

    # -- screens (owner) -----------------------------------------------------
    async def list_view(self, client) -> tuple[str, dict]:
        u = await self.username(client)
        lines = ["📢 <b>Groups & channels</b>", ""]
        if not self.all:
            lines.append("Not connected to any group yet.")
        for cid, g in self.all.items():
            names = [self.bot.names[s] for s in g.get("stores", []) if s in self.bot.names]
            state = "✅ on" if g.get("on", True) else "⏸ paused"
            lines.append(f"{'📣' if g.get('type') == 'channel' else '👥'} <b>{html.escape(g.get('title', cid))}</b> · {state}\n"
                         f"     {', '.join(names) or 'no stores'} · auto-delete {'on' if g.get('autodelete', True) else 'off'}")
        lines += ["", "<b>To connect:</b> tap a button below, pick the group or channel, and confirm. "
                  "That's it – I'll message you here when I'm in.",
                  "<i>For a channel, Telegram asks to make me an admin (needed to post).</i>"]
        rows = []
        if u:
            rows.append([{"text": "➕ Add to a group", "url": f"https://t.me/{u}?startgroup=psn&admin=delete_messages"},
                         {"text": "➕ Add to a channel", "url": f"https://t.me/{u}?startchannel&admin=post_messages+edit_messages+delete_messages"}])
        for cid, g in self.all.items():
            rows.append([{"text": f"⚙ {g.get('title', cid)}"[:40], "callback_data": f"gr:open:{cid}"}])
        rows.append([{"text": "🔄 Refresh", "callback_data": "gr:list"}, {"text": "⬅ Back to panel", "callback_data": "pn:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def group_view(self, cid: str) -> tuple[str, dict]:
        g = self.all[cid]
        on, ad = g.get("on", True), g.get("autodelete", True)
        stores = set(g.get("stores", []))
        text = (f"{'📣' if g.get('type') == 'channel' else '👥'} <b>{html.escape(g.get('title', cid))}</b>\n\n"
                f"Posting: <b>{'on' if on else 'paused'}</b> · Auto-delete when sold out: <b>{'on' if ad else 'off'}</b>\n"
                f"Post delay: <b>{self._delay_txt(g)}</b> (hidden – only you see this)\n"
                "Tap the stores whose restocks should be posted here (✅ = posted, 🔒 = private, never posted).")

    def _delay_txt(self, g: dict) -> str:
        d = self.delay(g)
        own = "" if g.get("delay") is not None else " (default)"
        return ("instant" if not d else f"{d // 60} min" + (f" {d % 60}s" if d % 60 else "") if d >= 60 else f"{d}s") + own
        priv = self.bot.users.private()
        sb = [{"text": f"{'🔒' if s.id in priv else '✅' if s.id in stores else '▫'} {s.name}", "callback_data": f"gr:st:{cid}:{s.id}"} for s in self.bot.stores]
        rows = [sb[i:i + 2] for i in range(0, len(sb), 2)]
        rows += [[{"text": "✅ All stores", "callback_data": f"gr:all:{cid}"}, {"text": "▫ None", "callback_data": f"gr:none:{cid}"}],
                 [{"text": "⏸ Pause posting" if on else "▶ Resume posting", "callback_data": f"gr:on:{cid}"},
                  {"text": f"{'✅' if ad else '▫'} Auto-delete", "callback_data": f"gr:ad:{cid}"}],
                 [{"text": ("✅ " if g.get("delay") == d else "") + ("⏳ default" if d is None else "⏳ now" if d == 0 else f"⏳ {d // 60} min" if d >= 60 else f"⏳ {d}s"),
                   "callback_data": f"gr:dl:{cid}:{'d' if d is None else d}"} for d in (None, 0, 30, 60, 120, 300)],
                 [{"text": "🧪 Send test", "callback_data": f"gr:test:{cid}"},
                  {"text": "🚫 Disconnect", "callback_data": f"gr:rm?:{cid}"}],
                 [{"text": "⬅ All groups", "callback_data": "gr:list"}]]
        return text, {"inline_keyboard": rows}

    async def on_button(self, client, cq, cid_owner: str, mid, data: str) -> None:
        parts = data.split(":")
        action = parts[1]
        gid = parts[2] if len(parts) > 2 else ""
        note = ""
        if action == "list" or (gid and gid not in self.all and action != "rm!"):
            text, kb = await self.list_view(client)
            await self.tg.answer_callback(client, cq["id"], "" if action == "list" else "That group isn't connected any more")
            await self.tg.edit(client, cid_owner, mid, text, kb)
            return
        g = self.all.get(gid, {})
        if action == "st" and len(parts) == 4 and parts[3] in self.bot.names:
            s = set(g.get("stores", []))
            s ^= {parts[3]}
            g["stores"] = sorted(s)
            note = f"{self.bot.names[parts[3]]}: {'posted ✅' if parts[3] in s else 'not posted'}"
        elif action == "all":
            g["stores"], note = [s.id for s in self.bot.stores], "All stores ✅"
        elif action == "none":
            g["stores"], note = [], "No stores"
        elif action == "on":
            g["on"] = not g.get("on", True)
            note = "Posting resumed ▶" if g["on"] else "Posting paused ⏸"
        elif action == "ad":
            g["autodelete"] = not g.get("autodelete", True)
            note = f"Auto-delete {'on' if g['autodelete'] else 'off'}"
        elif action == "dl" and len(parts) == 4:
            g["delay"] = None if parts[3] == "d" else int(parts[3])
            if g["delay"] is None:
                g.pop("delay")
            note = f"Post delay: {self._delay_txt(g)}"
        elif action == "test":
            note = "Test sent ✅ (deleted in 30 s)" if await self.test(client, gid) else "❌ Couldn't post there – check my permissions"
        elif action == "rm?":
            await self.tg.answer_callback(client, cq["id"])
            await self.tg.edit(client, cid_owner, mid,
                               f"🚫 <b>Disconnect {html.escape(g.get('title', gid))}?</b>\nI'll stop posting there and leave the group.",
                               {"inline_keyboard": [[{"text": "Yes, disconnect", "callback_data": f"gr:rm!:{gid}"},
                                                     {"text": "Cancel", "callback_data": f"gr:open:{gid}"}]]})
            return
        elif action == "rm!":
            await self.disconnect(client, gid)
            note = "Disconnected – I left the group"
            text, kb = await self.list_view(client)
            await self.tg.answer_callback(client, cq["id"], note)
            await self.tg.edit(client, cid_owner, mid, text, kb)
            return
        if note:
            self._save()
        await self.tg.answer_callback(client, cq["id"], note)
        text, kb = self.group_view(gid)
        await self.tg.edit(client, cid_owner, mid, text, kb)
