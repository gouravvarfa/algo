#!/bin/bash
# Runs ON the server as root, installed once as /usr/local/sbin/algo-deploy (a root-owned copy: the app user must not be able to edit what root runs).
# Called by the Deploy workflow (sudo /usr/local/sbin/algo-deploy <commit>), or by hand.
# Pull the commit, install what changed, put in the frontend that CI built, restart, check health; go back to the previous commit if the check fails.
# It never touches data/, /etc/algo-trading.env or angelone_credentials.json.
set -euo pipefail
APP=/opt/algo-trading
SHA="${1:-}"
SVC=algo-trading

as_app() { sudo -u algo -H "$@"; }

cd "$APP"
PREV=$(as_app git rev-parse HEAD)
echo "== current: $PREV"

echo "== 1. fetch and check out the new version =="
as_app git fetch --quiet origin main
if [ -n "$SHA" ]; then as_app git checkout --quiet --detach "$SHA"; else as_app git checkout --quiet --detach origin/main; fi
NEW=$(as_app git rev-parse HEAD)
echo "   now: $NEW"

rollback() {
  echo "!! deploy failed: going back to $PREV"
  as_app git checkout --quiet --detach "$PREV"
  as_app "$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt" || true
  sudo systemctl restart "$SVC" || true
  exit 1
}
trap rollback ERR

echo "== 2. python packages =="
as_app "$APP/.venv/bin/pip" install --quiet -r requirements.txt

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
echo "== deployed $NEW =="
