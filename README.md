# PSN Gift Card Stock Bot

Checks PlayStation Store gift cards (every value) on several Indian stores every ~3 minutes. When a card comes back in stock, it sends a Telegram message with the value, the price and a buy link.

| Store | How it's checked | Status |
|---|---|---|
| GyFTR | Brand page data (`gyftr.com/sony-playstation`) | ✅ Works |
| GamesTheShop | Store's own search API | ✅ Works |
| DigiStore | WooCommerce Store API (`playstation-card` category) | ✅ Works |
| Flipkart | Search results: a card tile without "Coming Soon" / "Currently unavailable" is in stock | ✅ Works |
| JioGames Store | PlayStation collection (cards are hidden when sold out), every 60 s | ✅ Works |
| Amazon.in | Listings from `config.yaml`, checked via the mobile product page (the ₹4000 card every 60 s) | ✅ Works |
| Blinkit, Paytm, CRED | — | ❌ Not included (see below) |

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
- **Blinkit:** protected by Cloudflare bot blocking (403 from cloud IPs), and it also needs a location.
- **Paytm, CRED:** app-only and need a logged-in account. Automating them breaks their terms and can get the account banned.

## Adding a store
Create `psnbot/stores/<name>.py` with a `Store` subclass whose `fetch()` returns a list of `Item`s. Then register it in `psnbot/stores/__init__.py` and add a section to `config.yaml`.
