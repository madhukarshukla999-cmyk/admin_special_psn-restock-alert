# PSN Gift Card Stock Bot

Checks PlayStation Store gift cards (every value) on several Indian stores every ~3 minutes. When a card comes back in stock, it sends a Telegram message with the value, the price and a buy link.

| Store | How it's checked | Status |
|---|---|---|
| GyFTR | Brand page data (`gyftr.com/sony-playstation`) | ✅ Works |
| GamesTheShop | Store's own search API | ✅ Works |
| DigiStore | WooCommerce Store API (`playstation-card` category) | ✅ Works |
| Flipkart | Search results: a card tile without "Coming Soon" / "Currently unavailable" is in stock | ✅ Works |
| Rooter | Product page data, per-value outOfStock flags, every 60 s | ✅ Works |
| Hubble | Gaming category list; PlayStation appears only when in stock, every 60 s | ✅ Works |
| JioGames Store | PlayStation collection (cards are hidden when sold out), every 60 s | ✅ Works |
| Amazon.in | Listings from `config.yaml`, checked via the mobile product page (the ₹4000 card every 60 s) | ✅ Works |
| Blinkit | Product API with your location (pincode 271001 → lat/lon) | ⚠️ Off by default; test with `--test-blinkit` (blocks cloud IPs) |
| Paytm, CRED | — | ❌ Not included (see below) |

## How alerts work
- **First start:** one summary message listing what's in stock right now.
- **After that:** a message only when a card goes from out of stock to in stock, or a new in-stock listing appears.
- **Store problems:** if a store fails 5 checks in a row, you get one ⚠️ warning, and a ✅ message when it recovers. A failed check never counts as "out of stock", so you won't get false restock alerts.

## Setup

### 1. Telegram
1. In Telegram, message **@BotFather**, send `/newbot` and copy the token.
2. Send any message to your new bot. Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number under `"chat":{"id": ...}`.
3. Run `cp .env.example .env` and fill in both values.

### 2. Google Cloud VM
Create a small VM. An **e2-micro** running Debian 12 or Ubuntu is enough, and it's in the free tier in `us-central1`, `us-west1` and `us-east1`. Then:

```bash
gcloud compute ssh <vm-name>
git clone <this repo> psn-stock-bot && cd psn-stock-bot
cp .env.example .env && nano .env              # paste token + chat id
sudo bash deploy/install.sh                     # installs + starts a systemd service
sudo journalctl -u psn-bot -f                   # watch the logs
```
The service starts on boot and restarts if it crashes.

### Windows PC (start automatically at login)
From the `psn-stock-bot` folder, after creating `.venv`, installing requirements and filling in `.env`:
```
powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1
```
This creates a Task Scheduler task called **PSN Stock Bot**. It runs hidden at login, restarts if it crashes and keeps running on battery. Logs go to `data\bot.log`.
- Stop: `schtasks /End /TN "PSN Stock Bot"` · Start: `schtasks /Run /TN "PSN Stock Bot"`
- Remove: `powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-autostart.ps1`
- Set Windows Sleep to **Never**, or the checks stop while the PC sleeps.
- Only one copy can run at a time (`data\bot.lock`), so stop the task before running `python -m psnbot` by hand.

### Heroku (worker dyno + Postgres)
Heroku wipes the disk on every restart, so the bot saves everything (stock, history, people, settings) to **Heroku Postgres** whenever `DATABASE_URL` is set. On a PC it keeps using `data/state.json`.
1. Create an app, then **Resources → Add-ons → Heroku Postgres (Essential-0)**. This sets `DATABASE_URL`.
2. **Settings → Config Vars:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally the `SMTP_*` settings. **Blinkit** blocks cloud IPs, so set **`BLINKIT_WARP=1`**. This routes Blinkit and GyFTR (both block Heroku) through a free Cloudflare WARP tunnel. Use `WARP_STORES=blinkit,gyftr,amazon` to choose which stores use it. (Alternatively, `BLINKIT_PROXY=http://user:pass@host:port` for your own proxy.) Check with **More → Run console** → `python -m psnbot --test-blinkit`.
3. **Deploy → GitHub:** connect this repo, enable **Automatic deploys** from `main`, then click **Deploy Branch**.
4. **Resources:** turn the **worker** dyno on (the `Procfile` has no web process).
5. Optional: copy your PC's data into the database. Stop the PC bot, then run on the PC: `set DATABASE_URL=<value from Config Vars>` and `.venv\Scripts\python -m psnbot --import-state data\state.json`.

Only run **one** copy per Telegram bot, so stop the PC task (`uninstall-autostart.ps1`). Auto-update is off on Heroku, since automatic deploys update it. Logs: **More → View logs**.

### Laptop browser extension (fast checkout)
The `extension/` folder is a Chrome/Edge extension. When the bot finds a restock, it opens the card on your laptop **within about a second** and goes to checkout. **You always pay yourself**: it never places an order, and stops at the payment/OTP step.
- **GamesTheShop:** opens the card, clicks **BUY NOW**, then **Pay Now**, so Paytm's QR is on screen to scan with your phone. Or set your UPI ID in the options and it sends the request to your UPI app instead; you only enter your PIN.
- **Amazon:** opens the product page and clicks **Buy Now**. At checkout it picks **Scan and Pay with UPI**, clicks **Use this payment method**, then **Pay with UPI**, and highlights the QR. You scan it and enter your PIN. If there is no Buy Now button, it uses Add to Cart, then Proceed to Buy. You can turn off the UPI steps in Options.
- **Several cards at once** (e.g. ₹1000, ₹2000, ₹3000, ₹4000): every card opens in its own tab. On GamesTheShop each tab goes on to checkout and the Paytm QR. On Amazon only the highest-value tab clicks through, because each Amazon UPI payment creates its own order; the others just open.
- **QR to Telegram (optional):** for GamesTheShop (Paytm) and Amazon UPI, choose **Send QR in DM** (the QR photo, pay from your phone) or **Pay manually** (only a notification saying which browser/profile/account has the QR, so you pay there). In DM mode the QR is sent to your Telegram chat as a photo, one per card. Set it up in Options → *Send QR to Telegram* with your bot token and the chat ID that `/extension` shows. Each photo says which browser/account it came from (the name you give the browser in Options) and the GamesTheShop mobile number, read from the signed-in account or typed in Options. The QR has a countdown (2 min by default) and is deleted from Telegram if not paid. A successful payment sends ✅ with a screenshot of the result page; a failed one sends ❌. These go only to the chat ID set in Options (the owner); everyone else only gets stock alerts.
- **Other stores:** just opens the page.

Setup: send `/extension` to the bot (owner only). Load the `extension/` folder (`chrome://extensions` → Developer mode → **Load unpacked**); its setup screen opens by itself. Paste the **setup code** from `/extension`, fill in what it couldn't detect (browser, profile, account name, mobile) and press **Connect**. You get a ✅ message in Telegram. Use `/extension test <product link>` to check it.

**Pause from Telegram:** `/extension off` (or the ⏸ button under `/extension`) stops every connected browser from opening or clicking anything, and Telegram alerts keep going to everyone. `/extension on` (▶) starts them again; each browser confirms in Telegram. A browser that was offline picks up the current state when it reconnects.

**QR mode from Telegram:** the buttons under `/extension` (📷 QR in DM · 🔔 Pay manually · ⛔ QR off), or `/extension qr dm` / `manual` / `off`, set the QR mode in every connected browser at once. Each browser confirms in Telegram. You can still change it per browser in Options afterwards.

The bot sends restocks to a private, random [ntfy.sh](https://ntfy.sh) topic, and the extension listens there (free, no account). It only opens links on known store sites, and only clicks in tabs it opened itself. Its options include a price limit, per-store switches and an auto-click switch. Turn the push off with `extension_push: false`.

### Telegram commands
Tap the bot's **Menu** button, or type a command:
- `/menu`: your own ✅/🔕 button per store, **🔔 All on / 🔕 All off**, and switches for **sold-out alerts**, the **daily summary**, **WhatsApp** and **email**
- `/status`: what's in stock right now (and for how long)
- `/check`: check every store right now
- `/history`: restocks in the last 7 days and how long each lasted (`/history 30` for 30 days)
- `/besttime`: when each store usually restocks (needs 3+ restocks on 2+ days)
- `/health`: is each store being checked OK, version, auto-update status
- `/cycles`: how often each store is checked. The owner can tap a store to change it, or type `/cycle amazon 2m`, `/cycle amazon fast 30s` (priority cards), or `/cycle amazon default`
- `/whatsapp`: get alerts on WhatsApp too, via the free CallMeBot service; follow the steps it shows. `/whatsapp on|off|test|remove`
- `/email you@example.com`: get alerts by email too (the owner sets `SMTP_*` in `.env` first; see `.env.example`). `/email on|off|test|remove`
- `/users` (owner): who gets alerts; approve or remove people
- `/update` (owner): install the latest version now

**Adding people:** they open the bot in Telegram and tap **Start**. You (the chat ID in `.env`) get **Allow / Deny** buttons. Each person has their own `/menu` settings.

**Automatic messages:** restocks, sold-outs ("was in stock for 6 min"), a daily summary at `daily_summary_time` (IST), a warning when the bot was offline or asleep for more than `down_alert_minutes`, and auto-update notices (owner only).

**Auto-update:** every `auto_update_hours`, the bot runs `git pull`, reinstalls packages if needed and restarts itself. This only works in a git clone. If you edited a file the update changes (e.g. `config.yaml`), it skips the update and tells you. On Windows, re-run `install-autostart.ps1` once so the task can restart itself after updates.

### Useful commands
```bash
.venv/bin/python -m psnbot --list            # print every listing found right now + stock status
.venv/bin/python -m psnbot --test-telegram   # check the bot can message you
.venv/bin/python -m psnbot --once --dry-run  # one cycle, print alerts instead of sending
```
To change something, edit `config.yaml` (check intervals, stores, search terms, extra Flipkart or Amazon links), then run `sudo systemctl restart psn-bot`. If you edited the copy in `/opt/psn-stock-bot`, that's all. If you edited your clone, run `install.sh` again first.

## Amazon notes
- Amazon blocks its normal product page (`/dp/…`) and search for cloud server IPs, but not the mobile product page (`/gp/aw/d/…`), so the bot reads that. Alerts still link to the normal page.
- Search is blocked, so the bot can't find new listings by itself. To add one, paste the link (any format, including `amzn.in` share links) under `amazon.product_urls` in `config.yaml`.
- Listings under `amazon.fast_check` are also checked every `fast_interval_seconds` (60 s by default).
- If Amazon starts blocking the mobile page too, you'll get the ⚠️ "can't be checked" warning, never a false alert.

## Why some platforms aren't included
- **Blinkit:** no login is needed, but Cloudflare blocks cloud/data-center IPs (403). Run `.venv/bin/python -m psnbot --test-blinkit` on the machine that will run the bot. If it isn't blocked, set `blinkit.enabled: true`. If it is, run a second copy at home with only `blinkit` enabled; it can share the same Telegram chat.
- **Paytm, CRED:** app-only and need a logged-in account. Automating them breaks their terms and can get the account banned.

## Adding a store
Create `psnbot/stores/<name>.py` with a `Store` subclass whose `fetch()` returns a list of `Item`s. Then register it in `psnbot/stores/__init__.py` and add a section to `config.yaml`.
