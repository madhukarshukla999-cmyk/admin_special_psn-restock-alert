"""Extra alert channels: WhatsApp (CallMeBot, free) and email (SMTP).

WhatsApp: each person links their own number once:
  1. Save the CallMeBot number in your contacts (see CALLMEBOT_NUMBER).
  2. Send it "I allow callmebot to send me messages" on WhatsApp.
  3. It replies with an API key -> send `/whatsapp <your number> <apikey>` to the Telegram bot.
Email: the owner puts SMTP settings in .env (Gmail: use an App Password), then anyone can
send `/email you@example.com`.
"""
from __future__ import annotations

import asyncio
import html as htmlmod
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage

import httpx

log = logging.getLogger(__name__)

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_NUMBER = "+34 644 59 71 67"
CALLMEBOT_HELP = "https://www.callmebot.com/blog/free-api-whatsapp-messages/"


def html_to_text(s: str, whatsapp: bool = False) -> str:
    """Telegram-style HTML -> plain text (WhatsApp gets *bold* / ~strike~)."""
    s = re.sub(r'<a href="([^"]+)">(.*?)</a>', lambda m: f"{m.group(2)}: {htmlmod.unescape(m.group(1))}", s)
    if whatsapp:
        s = re.sub(r"</?b>", "*", s)
        s = re.sub(r"</?s>", "~", s)
        s = re.sub(r"</?i>", "_", s)
    s = re.sub(r"<[^>]+>", "", s)
    return htmlmod.unescape(s).strip()


def clean_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw or "")
    if len(digits) == 10:              # Indian mobile without country code
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 else None


# -- WhatsApp ------------------------------------------------------------------
async def send_whatsapp(client: httpx.AsyncClient, phone: str, apikey: str, text: str) -> tuple[bool, str]:
    """Returns (ok, detail). CallMeBot limits message length and rate, so keep it short."""
    text = text[:1500]
    try:
        r = await client.get(CALLMEBOT_URL, params={"phone": phone, "text": text, "apikey": apikey}, timeout=30)
    except httpx.HTTPError as e:
        return False, f"request failed: {e!r}"
    body = re.sub(r"<[^>]+>", " ", r.text)
    body = " ".join(body.split())
    if r.status_code == 200 and re.search(r"queued|sent", body, re.I) and not re.search(r"error|invalid", body, re.I):
        return True, "sent"
    return False, (body[:200] or f"HTTP {r.status_code}")


# -- Email -----------------------------------------------------------------------
class Mailer:
    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587") or 587)
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", "") or self.user

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def _send_sync(self, to: str, subject: str, html_body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = to
        msg.set_content(html_to_text(html_body))
        msg.add_alternative(f"<div style='font-family:sans-serif;font-size:15px'>"
                            f"{html_body.replace(chr(10), '<br>')}</div>", subtype="html")
        ctx = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(self.user, self.password)
                s.send_message(msg)

    async def send(self, to: str, subject: str, html_body: str) -> tuple[bool, str]:
        if not self.configured:
            return False, "email isn't set up (SMTP_* in .env)"
        try:
            await asyncio.to_thread(self._send_sync, to, subject, html_body)
            return True, "sent"
        except (smtplib.SMTPException, OSError) as e:
            return False, f"{e.__class__.__name__}: {str(e)[:200]}"


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
