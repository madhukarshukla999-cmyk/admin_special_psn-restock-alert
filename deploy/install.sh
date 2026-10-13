#!/usr/bin/env bash
# One-time setup on a Debian/Ubuntu VM (e.g. Google Cloud Compute Engine).
# Run from inside the project folder:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/psn-stock-bot
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi

apt-get update -y
apt-get install -y python3 python3-venv rsync

id psnbot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin psnbot

mkdir -p "$APP_DIR/data"
rsync -a --delete --exclude .venv --exclude data --exclude .env "$SRC_DIR/" "$APP_DIR/"
[[ -f "$SRC_DIR/.env" ]] && cp "$SRC_DIR/.env" "$APP_DIR/.env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo ">> Edit $APP_DIR/.env with your Telegram token and chat id, then run:"
  echo "   sudo systemctl restart psn-bot"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R psnbot:psnbot "$APP_DIR"
chmod 600 "$APP_DIR/.env"

cp "$APP_DIR/deploy/psn-bot.service" /etc/systemd/system/psn-bot.service
systemctl daemon-reload
systemctl enable psn-bot
systemctl restart psn-bot

echo ">> Installed. Check logs with:  sudo journalctl -u psn-bot -f"
