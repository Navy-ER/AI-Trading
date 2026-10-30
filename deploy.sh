#!/usr/bin/env bash
# Deploy AI Trading Bot ke acerver lewat SSH.
#
# Pemakaian (dari Git Bash / WSL / Linux):
#   ./deploy.sh
#   HOST=root@192.168.18.188 KEY=~/.ssh/id_ed25519 ./deploy.sh
#
# Alur: compile check -> tar -> scp -> backup -> install -> restart ->
# health check. Kalau health check gagal, otomatis rollback ke versi lama.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-root@192.168.18.188}"
KEY="${KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-/home/server/trading-bot}"
OWNER="${OWNER:-server:server}"
SERVICES="${SERVICES:-trading-bot trading-dashboard}"
PYTHON="${PYTHON:-python3}"

# File runtime yang ikut dikirim. File konfigurasi (.env, state, jsonl) TIDAK disentuh.
FILES=(
  bridge.py
  execution.py
  robust_http.py
  wallet_manager.py
  recovery_policy.py
  journal_policy.py
  position_snapshot.py
  health.py
  monitor.py
  dashboard_server.py
  winrate.py
  eval_accuracy.py
  requirements.txt
  deploy/systemd/trading-bot.service
  deploy/systemd/trading-dashboard.service
)

for f in "${FILES[@]}"; do
  [[ -f "$f" ]] || { echo "ERROR: file tidak ditemukan: $f" >&2; exit 1; }
done

echo "== compile check lokal =="
"$PYTHON" -m py_compile bridge.py execution.py robust_http.py wallet_manager.py \
  recovery_policy.py journal_policy.py position_snapshot.py health.py \
  dashboard_server.py monitor.py

STAMP="$(date +%Y%m%d_%H%M%S)"
TARBALL="${TMPDIR:-/tmp}/acerver-$STAMP.tar.gz"
trap 'rm -f "$TARBALL"' EXIT

echo "== pack + upload ($STAMP) =="
tar -czf "$TARBALL" -- "${FILES[@]}"
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new \
  "$TARBALL" "$HOST:/tmp/acerver-$STAMP.tar.gz"

echo "== remote deploy + health check =="
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' OWNER='$OWNER' SERVICES='$SERVICES' STAMP='$STAMP' bash -s" <<'REMOTE'
set -euo pipefail
STAGE="$REMOTE_DIR/.deploy-$STAMP"
BACKUP="$REMOTE_DIR/.backup-$STAMP"
RUNAS="${OWNER%:*}"
RUNAS_GROUP="${OWNER#*:}"
RUID="$(id -u "$RUNAS")"
export XDG_RUNTIME_DIR="/run/user/$RUID"
AS_USER=(runuser -u "$RUNAS" -- env "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR")

die() { echo "ERROR: $*" >&2; exit 1; }

mkdir -p "$STAGE" "$BACKUP"
tar -xzf "/tmp/acerver-$STAMP.tar.gz" -C "$STAGE"
trap 'rm -rf "$STAGE" "/tmp/acerver-$STAMP.tar.gz"' EXIT

mapfile -t FILES < <(cd "$STAGE" && find . -maxdepth 1 -type f -printf '%f\n')
(( ${#FILES[@]} > 0 )) || die "staging kosong"

echo "-- backup ke $BACKUP"
for f in "${FILES[@]}"; do
  [[ -f "$REMOTE_DIR/$f" ]] && cp -p "$REMOTE_DIR/$f" "$BACKUP/$f"
done

echo "-- compile check di server"
python3 -m py_compile "$STAGE"/*.py || die "py_compile gagal, belum ada file yang diubah"

echo "-- install"
for f in "${FILES[@]}"; do
  install -o "$RUNAS" -g "$RUNAS_GROUP" -m 644 "$STAGE/$f" "$REMOTE_DIR/$f"
done

if [[ -d "$STAGE/deploy/systemd" ]]; then
  UNIT_DIR="$(getent passwd "$RUNAS" | cut -d: -f6)/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  for u in "$STAGE"/deploy/systemd/*.service; do
    install -o "$RUNAS" -g "$RUNAS_GROUP" -m 644 "$u" "$UNIT_DIR/$(basename "$u")"
  done
  "${AS_USER[@]}" systemctl --user daemon-reload
  echo "   systemd units updated"
fi

restart_services() {
  local units
  units="$("${AS_USER[@]}" systemctl --user list-unit-files 2>/dev/null || true)"
  for svc in $SERVICES; do
    if grep -q "^$svc\.service" <<<"$units"; then
      "${AS_USER[@]}" systemctl --user restart "$svc" || return 1
      echo "   restarted $svc"
    else
      echo "   (skip $svc: unit tidak ada)"
    fi
  done
}

rollback() {
  echo "!! ROLLBACK dari $BACKUP" >&2
  for f in "${FILES[@]}"; do
    [[ -f "$BACKUP/$f" ]] && install -o "$RUNAS" -g "$RUNAS_GROUP" -m 644 "$BACKUP/$f" "$REMOTE_DIR/$f"
  done
  restart_services || true
}

echo "-- restart"
before_lines=$(wc -l < "$REMOTE_DIR/bridge.log" 2>/dev/null || echo 0)
restart_services || { rollback; die "restart gagal"; }

echo "-- health check (maks 45s)"
ok=0
for _ in $(seq 1 15); do
  sleep 3
  if "${AS_USER[@]}" systemctl --user is-active --quiet trading-bot; then
    new_log="$(tail -n +$((before_lines + 1)) "$REMOTE_DIR/bridge.log" 2>/dev/null || true)"
    if [[ "$new_log" == *"[BRIDGE] Started"* ]]; then
      ok=1
      break
    fi
  else
    break
  fi
done

if (( ok == 0 )); then
  echo "-- log terakhir:" >&2
  tail -n 20 "$REMOTE_DIR/bridge.log" >&2 || true
  rollback
  die "health check gagal (service mati / tidak start)"
fi

# Simpan 3 backup terakhir saja
ls -1dt "$REMOTE_DIR"/.backup-* 2>/dev/null | tail -n +4 | xargs -r rm -rf

echo "-- versi aktif"
sha256sum "$REMOTE_DIR/bridge.py"
tail -n 3 "$REMOTE_DIR/bridge.log"
echo "OK: deploy $STAMP sukses"
REMOTE
