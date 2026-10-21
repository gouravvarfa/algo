# Deploying to AWS (paper trading only, no broker orders)

## Which AWS option
| | Cost (approx.) | Good for |
|---|---|---|
| **EC2 t3.small, ap-south-1 (Mumbai)** | ~$15/month | Recommended: 2 GB RAM is enough for the app + SQLite data |
| **Lightsail 2 GB, Mumbai** | ~$12/month fixed | Simplest; includes static IP |
| Disk | 30-40 GB | Bhavcopy DB ~0.5 GB + raw files; intraday DB grows ~1 GB/month |
Prices change: check the AWS pricing page before creating anything.

## Steps (Ubuntu 22.04/24.04)
1. Create the instance in **ap-south-1**; attach a static (Elastic) IP. Security group: allow 22 (SSH) and 443 only **from your own IP**.
2. `sudo adduser --system --group algo && sudo apt install -y python3-venv nginx certbot python3-certbot-nginx`
3. Copy the project to `/opt/algo-trading` (not `.env`, `.venv`, logs). Copy `data/` once (it is large) with `scp`/`rsync`.
4. `cd /opt/algo-trading && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
5. Login settings: on your laptop run `python scripts/make_login.py <username>`; put the three lines it prints into `/etc/algo-trading.env` (`chmod 600`, owner root). Without them the app has NO login: never expose it that way.
6. Angel One credentials: create `angelone_credentials.json` on the server directly (chmod 600), never in git/chat.
7. `sudo cp deploy/algo-trading.service /etc/systemd/system/ && sudo systemctl enable --now algo-trading`
8. `sudo cp deploy/nginx-algo.conf /etc/nginx/sites-available/algo` (edit YOUR.DOMAIN), enable it, then `sudo certbot --nginx -d YOUR.DOMAIN`.
9. Check: `curl https://YOUR.DOMAIN/healthz` and `journalctl -u algo-trading -f`.

## Daily operation
- Backups: `rsync` of `data/` (watchlist.json, paper_trading/journal.jsonl, strategies.db, backtests/) to S3 or another disk nightly.
- Angel One allows one active API session per account: do not run the app locally and on AWS with the same login at the same time.
- Market data refresh: the paper engine re-evaluates every 20 s while the market is open.

## NOT included on purpose
Live order placement. Going from paper to real orders needs its own plan (order types, max lots, daily-loss limit, kill switch, reconciliation) and your explicit approval.
