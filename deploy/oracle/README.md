# Autocrat core on Oracle Cloud Always Free

Runs the always-on core (pipeline, workers, alerts, `/ws/v2`) on an Ampere A1 VM.
As of Nov 2026 Oracle's docs give Always Free A1 = 1,500 OCPU-hours + 9,000 GB-hours
a month, i.e. **2 OCPUs / 12 GB RAM** — enough for the core, Chromium and a 3B local model.

## 1. Create the VM (you, in the OCI console)

1. Compute → Instances → Create. Image **Ubuntu 24.04**, shape **VM.Standard.A1.Flex**,
   **2 OCPU / 12 GB** (don't exceed the free allowance — over-limit instances get terminated).
   Boot volume 50–100 GB (200 GB total is free).
2. Add your SSH public key. Note the public IP.
3. "Out of host capacity"? Try another availability domain or retry later. Upgrading the
   account to Pay As You Go also helps and stays $0 within the free limits — set a $1
   budget alert first.
4. Networking → your VCN → Security List → add ingress TCP 80 and 443 from 0.0.0.0/0
   (only needed once you expose HTTPS).

## 2. Bootstrap

```bash
ssh ubuntu@<vm-ip>
git clone https://github.com/Autocrat2005/Autocrat.git ~/autocrat
git clone git@github.com:<you>/autocrat-revenue-plugins.git ~/autocrat-revenue-plugins   # deploy key
cd ~/autocrat && bash deploy/oracle/setup.sh
```

From the laptop, copy secrets (never commit them):

```bash
scp .env ubuntu@<vm-ip>:~/autocrat/.env
```

On the VM set in `.env`: `AUTOCRAT_PROFILE=core`, `AUTOCRAT_BRAIN=0`,
`AUTOCRAT_PLUGIN_PATHS=../autocrat-revenue-plugins/plugins`, `RUN_MODE=dry_run` until you're ready.

```bash
sudo systemctl start autocrat-core
journalctl -u autocrat-core -f
```

## 3. WhatsApp alerts from the VM (optional)

```bash
~/autocrat/.venv/bin/python -m nexus.integrations.whatsapp_alerts login --headless
# laptop:  scp ubuntu@<vm-ip>:~/autocrat/.autocrat/whatsapp-qr.png .   → scan with the SENDER phone
~/autocrat/.venv/bin/python -m nexus.integrations.whatsapp_alerts test
```

Then `WHATSAPP_ALERTS_ENABLED=1` in `.env` and restart the service.

## What runs where

| Piece | Where | Cost |
|---|---|---|
| Core API, store, workers, alerts | VM (systemd `autocrat-core`) | $0 |
| Local fallback LLM (`llama3.2:3b` via Ollama) | VM | $0 |
| Claude Haiku 5.5 / DeepSeek Flash | APIs, paced by `llm.provider_budgets` | your credit |
| SQLite backups | VM nightly, optional rclone → R2 free tier | $0 |
| HUD, desktop plugins, desk voice | laptop | $0 |

## Keeping it alive

Oracle may reclaim an Always Free instance if CPU p95, network and memory all stay
under 20% for 7 days. A running core with Ollama and the embedding model loaded uses
real memory; do not add synthetic load to game the check. Backups + `Restart=always`
mean a reclaimed VM is a re-run of this guide, and the same setup runs unchanged on a
~$5 VPS if needed.
