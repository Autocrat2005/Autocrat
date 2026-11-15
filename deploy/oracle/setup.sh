#!/usr/bin/env bash
# Autocrat core — Oracle Cloud Always Free (Ampere A1, Ubuntu 22.04/24.04 ARM64) bootstrap.
#
# Run on the VM as the default `ubuntu` user, from the repo root:
#   git clone https://github.com/Autocrat2005/Autocrat.git ~/autocrat && cd ~/autocrat
#   bash deploy/oracle/setup.sh
# Then copy your .env from the laptop (never commit it):
#   scp .env ubuntu@<vm-ip>:~/autocrat/.env
#
# Safe to re-run. Installs: Python venv + core deps, Playwright Chromium, Ollama +
# llama3.2:3b (free local fallback LLM), systemd service, nightly SQLite backups,
# a 2 GB swap file, and opens ports 80/443 in the instance firewall.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGINS_DIR="$(dirname "$REPO_DIR")/autocrat-revenue-plugins"
LOCAL_MODEL="${LOCAL_MODEL:-llama3.2:3b}"
RUN_USER="${SUDO_USER:-$USER}"

echo "==> Autocrat core setup in $REPO_DIR (user: $RUN_USER)"

echo "==> System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip git sqlite3 curl ca-certificates iptables-persistent

echo "==> 2 GB swap (headroom for demo builds on a 12 GB instance)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> Open 80/443 in the instance firewall (OCI Ubuntu images REJECT them by default)"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 6 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
done
sudo netfilter-persistent save
echo "    Also add ingress rules for TCP 80 and 443 in the VCN Security List (OCI console)."

echo "==> Python virtualenv + core dependencies"
python3 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements-core.txt"

echo "==> Playwright Chromium (demo screenshots, WhatsApp alerts)"
sudo "$REPO_DIR/.venv/bin/python" -m playwright install-deps chromium
"$REPO_DIR/.venv/bin/python" -m playwright install chromium

echo "==> Ollama + $LOCAL_MODEL (free local fallback LLM)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama
ollama pull "$LOCAL_MODEL"

echo "==> Private revenue plugins"
if [ -d "$PLUGINS_DIR/plugins" ]; then
  echo "    found $PLUGINS_DIR"
else
  echo "    NOT FOUND: clone your private repo to $PLUGINS_DIR (deploy key), e.g."
  echo "    git clone git@github.com:<you>/autocrat-revenue-plugins.git $PLUGINS_DIR"
fi

echo "==> .env"
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  echo "    created .env from .env.example — copy your real one over it (scp) before starting"
fi
chmod 600 "$REPO_DIR/.env"
mkdir -p "$REPO_DIR/.autocrat" "$HOME/backups/autocrat"

echo "==> systemd service: autocrat-core"
sed -e "s#__REPO_DIR__#$REPO_DIR#g" -e "s#__USER__#$RUN_USER#g" \
  "$REPO_DIR/deploy/oracle/autocrat-core.service" | sudo tee /etc/systemd/system/autocrat-core.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable autocrat-core

echo "==> Nightly backup at 03:30 (keeps 14 days)"
CRON_LINE="30 3 * * * REPO_DIR=$REPO_DIR bash $REPO_DIR/deploy/oracle/backup.sh >> $HOME/backups/autocrat/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'deploy/oracle/backup.sh' ; echo "$CRON_LINE" ) | crontab -

cat <<EOF

Done. Next:
  1. scp your .env to $REPO_DIR/.env  (set AUTOCRAT_PROFILE=core, AUTOCRAT_BRAIN=0)
  2. sudo systemctl start autocrat-core && journalctl -u autocrat-core -f
  3. WhatsApp alerts (optional): $REPO_DIR/.venv/bin/python -m nexus.integrations.whatsapp_alerts login --headless
     then on the laptop: scp ubuntu@<vm-ip>:$REPO_DIR/.autocrat/whatsapp-qr.png .   (scan with the SENDER phone)
  4. Public HTTPS (optional, Phase 3): install Caddy and use deploy/oracle/Caddyfile
EOF
