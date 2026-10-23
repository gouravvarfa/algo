# Algo Trading: research and paper trading

A research and **paper trading** app. It never sends an order to a broker.

* **Top Bottom**: a rule-based strategy on BankNifty futures, evaluated live (paper) from Angel One data, with contract roll.
* **Options Simulator**: any past day of NIFTY / BANKNIFTY options (end of day or 1-minute), a LIVE mode from WebSocket ticks, positions, payoff, margin, what-if.
* **Strategy Builder / Backtesting / History**: rule-based option strategies tested on NSE Bhavcopy data.
* **Option Chain**: live chain from Angel One and the stored end-of-day data.
* **Accounts** (optional): password + authenticator code, one device per account, admin panel (add / remove users, read-only "view as"), developer panel (system health, fault isolation).

## Run it

```
# backend (Python 3.12+)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# frontend (Node 20+), built once and served by the backend under /app
cd frontend && npm install && npm run build && cd ..
start.bat                      # http://127.0.0.1:8020/app/
```

Angel One keys: `angelone_credentials.json` or `.env` (never commit them; both are git-ignored).

## Accounts

Without any account the app runs open on your own computer. To switch on accounts create the owner (the first developer):

```
python scripts/create_owner.py <username> "<full name>"     # prints a one-time setup link
```

From then on everyone signs in with password + authenticator code. The owner adds admins and developers; admins add users. Nobody can sign up on their own.

## Tests

```
python -m pytest tests -q            # backend
cd frontend && npm test              # React app (vitest) and  npx tsc --noEmit
```

## Layout

| Folder | What is in it |
|---|---|
| `app/` | FastAPI backend: `main.py` (routes), `paper_trading/` (live engine), `strategy/top_bottom/` (the rules), `live/` (ticks, contract roll), `options/` (simulator, builder, collector), `security/` + `auth_api.py` (accounts), `ops/` (developer panel: health, fault isolation) |
| `frontend/` | React + TypeScript app (Vite), one folder per feature under `src/features/` |
| `data/` | databases and downloaded data (git-ignored) |
| `scripts/` | loaders and account tools |
| `deploy/`, `docs/` | AWS / systemd / nginx files and notes |

Paper only until a written plan for real orders is approved (shadow mode first, risk limits, kill switch).
