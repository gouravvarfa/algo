#!/bin/bash
# Runs ON the server as root, installed as /usr/local/sbin/algo-deploy (a root-owned copy: the app user must not be able to edit what root runs).
# Two callers:
#   1. the GitHub Deploy workflow:   sudo /usr/local/sbin/algo-deploy <commit>     (after CI passed; also uploads the built frontend to /tmp/algo-dist.tgz)
#   2. the 2-minute systemd timer (algo-deploy.timer), with NO argument: it only deploys origin/main when that commit's CI checks have all passed
#      AND the frontend did not change (a frontend change needs the build that only the workflow brings).
# Rules that keep the site from flickering: if the server already runs the target commit and no new build was uploaded, it does NOTHING (no restart).
# On a failed health check it goes back to the previous commit. It never touches data/, /etc/algo-trading.env or angelone_credentials.json.
set -euo pipefail
APP=/opt/algo-trading
SHA="${1:-}"
SVC=algo-trading
REPO=gouravvarfa/algo

as_app() { sudo -u algo -H "$@"; }

exec 9>/var/lock/algo-deploy.lock
flock -n 9 || { echo "another deploy is running: skipping"; exit 0; }

cd "$APP"
PREV=$(as_app git rev-parse HEAD)
as_app git fetch --quiet origin main
TARGET=$SHA
[ -n "$TARGET" ] || TARGET=$(as_app git rev-parse origin/main)
TARGET=$(as_app git rev-parse "$TARGET")

# ---- nothing new: do nothing (this is what stops needless restarts) ----
if [ "$TARGET" = "$PREV" ] && [ ! -f /tmp/algo-dist.tgz ]; then
  echo "already on ${PREV:0:7}: nothing to do"
  exit 0
fi

# ---- timer path (no commit given): only a CI-green commit, and only if no frontend build is needed ----
if [ -z "$SHA" ]; then
  verdict=$(curl -fsS -m 15 -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/commits/$TARGET/check-runs" 2>/dev/null | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("check_runs", [])
except Exception:
    print("unknown"); sys.exit()
if not runs: print("none")
elif any(r["status"] != "completed" for r in runs): print("running")
elif all(r["conclusion"] in ("success", "skipped", "neutral") for r in runs): print("green")
else: print("red")
' || echo unknown)
  if [ "$verdict" != "green" ]; then echo "commit ${TARGET:0:7}: CI is '$verdict': not deploying (will look again)"; exit 0; fi
  if ! as_app git diff --quiet "$PREV" "$TARGET" -- frontend; then
    echo "commit ${TARGET:0:7}: the frontend changed, so it needs the build from the Deploy workflow: leaving it to that"; exit 0
  fi
fi

echo "== deploying ${PREV:0:7} -> ${TARGET:0:7} =="
echo "== 1. check out the new version =="
as_app git checkout --quiet --detach "$TARGET"

rollback() {
  echo "!! deploy failed: going back to ${PREV:0:7}"
  as_app git checkout --quiet --detach "$PREV"
  as_app "$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt" || true
  sudo systemctl restart "$SVC" || true
  exit 1
}
trap rollback ERR

echo "== 2. python packages (only if requirements.txt changed) =="
if ! as_app git diff --quiet "$PREV" "$TARGET" -- requirements.txt; then
  as_app "$APP/.venv/bin/pip" install --quiet -r requirements.txt
else
  echo "   unchanged"
fi

echo "== 3. frontend (built by CI, uploaded to /tmp/algo-dist.tgz) =="
if [ -f /tmp/algo-dist.tgz ]; then
  TMP=$(mktemp -d)
  tar -xzf /tmp/algo-dist.tgz -C "$TMP"
  sudo rm -rf "$APP/frontend/dist.new"
  sudo mv "$TMP/dist" "$APP/frontend/dist.new"
  sudo chown -R algo:algo "$APP/frontend/dist.new"
  sudo rm -rf "$APP/frontend/dist.old"
  [ -d "$APP/frontend/dist" ] && sudo mv "$APP/frontend/dist" "$APP/frontend/dist.old"
  sudo mv "$APP/frontend/dist.new" "$APP/frontend/dist"
  rm -rf "$TMP" /tmp/algo-dist.tgz
else
  echo "   no new build uploaded: keeping the current frontend"
fi

echo "== 4. restart and health check =="
sudo systemctl restart "$SVC"
ok=0
for i in $(seq 1 20); do
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8020/healthz || true)
  if [ "$code" = "200" ]; then ok=1; break; fi
done
[ "$ok" = "1" ] || false          # triggers the rollback above
trap - ERR
echo "== deployed ${TARGET:0:7} =="
