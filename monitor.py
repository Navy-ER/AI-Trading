#!/usr/bin/env python3
"""Monitor AI trading bot: bridge alive, open positions, closed trade stats.
Appends snapshot to E:/Project AI/monitoring.log, prints one-line summary.
Pushes Discord embeds (trade close, winrate update, bridge DOWN warning).
"""
import json, os, subprocess, sys
from datetime import datetime, timezone

import requests  # same dep as bridge.py
from execution import trading_mode

BASE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(BASE, "trades.jsonl")
OPENS = {
    "DRY_RUN": os.path.join(BASE, "open_positions.dry_run.jsonl"),
    "LIVE": os.path.join(BASE, "open_positions.live.jsonl"),
}
STATE = os.path.join(BASE, "monitor_state.json")
LOG = os.path.join(BASE, "monitoring.log")
PYTHON = sys.executable
BRIDGE_PY = os.path.join(BASE, "bridge.py")
WEBHOOK_URL = os.getenv("DISCORD_MONITOR_WEBHOOK_URL", "")

GREEN, BLUE, RED = 0x2ECC71, 0x3498DB, 0xE74C3C


def send_embed(embed):
    if not WEBHOOK_URL:
        print("[DISCORD] monitor webhook is not configured")
        return False
    try:
        r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        ok = r.status_code in (200, 204)
        if not ok:
            print("[DISCORD] HTTP %s" % r.status_code)
        return ok
    except Exception as e:
        print("[DISCORD] Error: %s" % e)
        return False


def make_embed(title, color, fields, desc=None):
    e = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": "Monitor Bot"},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if desc:
        e["description"] = desc
    return e


def bridge_alive():
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like '%python%'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, timeout=20).stdout
        return any("bridge.py" in line for line in out.splitlines())
    except Exception:
        return None  # unknown


def restart_bridge():
    """Start bridge.py detached; position records are stored by trading mode."""
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        out = open(os.path.join(BASE, "bridge_restart.log"), "a", encoding="utf-8")
        proc = subprocess.Popen(
            [PYTHON, BRIDGE_PY], cwd=BASE, stdout=out, stderr=subprocess.STDOUT,
            creationflags=flags)
        with open(os.path.join(BASE, "bridge_restart.log"), "a", encoding="utf-8") as f:
            f.write("[%s] restart pid=%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), proc.pid))
        return proc.pid
    except Exception as e:
        print("[RESTART] Error: %s" % e)
        return None


def closed_stats():
    w = l = 0
    net = 0.0
    last_ts = None
    for line in open(TRADES, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        if t.get("status") == "CLOSED":
            p = t.get("pnl_usdt", 0.0)
            if t.get("corrupt") or abs(p) > 0.5:  # glitch price trades (size 1.2 -> |pnl| > 0.5 mustahil)
                continue
            net += p
            if p > 0:
                w += 1
            else:
                l += 1
            ts = t.get("closed_at", "")
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
    return w + l, w, l, net, last_ts


def open_info():
    n = 0
    fsum = 0.0
    entries = []
    mode = trading_mode()
    path = OPENS.get(mode)
    if path is None or not os.path.exists(path):
        return n, fsum, entries
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        t = json.loads(line)
        if not isinstance(t, dict) or t.get("mode") != mode:
            continue
        n += 1
        fsum += t.get("floating_pnl", 0.0)
        entries.append((t["asset"], t.get("floating_pct", 0.0)))
    return n, fsum, entries


def notify(alive, total, w, l, net, last_ts, n, fsum, state, first_run, new_closes):
    wr = 100.0 * w / total if total else 0.0
    fields = [
        {"name": "Closed", "value": "%d (%dW/%dL)" % (total, w, l), "inline": True},
        {"name": "Winrate", "value": "%.1f%%" % wr, "inline": True},
        {"name": "Net PnL", "value": "%+.4f USDT" % net, "inline": True},
        {"name": "Open", "value": "%d pos (float %+.4f)" % (n, fsum), "inline": True},
    ]
    if alive is False and state.get("alive", True):
        send_embed(make_embed("⚠️ Bridge DOWN", RED, fields,
                              "bridge.py tidak berjalan! Cek proses / restart."))
    elif not first_run and alive and not state.get("alive", True):
        send_embed(make_embed("✅ Bridge RECOVERED", GREEN, fields,
                              "bridge.py sudah online lagi!"))
    elif not first_run:
        if new_closes > 0:
            send_embed(make_embed("✅ Trade Closed", GREEN, fields,
                                  "%d posisi baru ditutup (terakhir %s)" % (new_closes, last_ts)))
        elif wr != state.get("wr") or abs(net - state.get("net", net)) > 1e-9:
            send_embed(make_embed("📊 Winrate Update", BLUE, fields,
                                  "winrate %.1f%% -> %.1f%% | net %+.4f -> %+.4f USDT"
                                  % (state.get("wr", 0), wr, state.get("net", 0), net)))


def main():
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            state = {}

    alive = bridge_alive()
    total, w, l, net, last_ts = closed_stats()
    n, fsum, entries = open_info()
    wr = 100.0 * w / total if total else 0.0

    first_run = "total" not in state
    new_closes = 0 if first_run else total - state.get("total", total)

    msgs = []
    if alive is None:
        msgs.append("bridge: ?")
    elif alive:
        msgs.append("bridge: RUNNING")
    else:
        # P1: bridge DOWN -> auto-restart (positions persist by trading mode)
        pid = restart_bridge()
        if pid:
            alive = True
            msgs.append("bridge: DOWN -> AUTO-RESTARTED (pid %s)" % pid)
            send_embed(make_embed("🔄 Bridge DOWN → Auto-Restarted", RED,
                                  [{"name": "PID", "value": str(pid), "inline": True},
                                   {"name": "Open", "value": "%d pos (float %+.4f)" % (n, fsum), "inline": True}],
                                  "bridge.py di-restart otomatis. Posisi tersimpan terpisah menurut mode."))
        else:
            msgs.append("bridge: DOWN!! (restart gagal)")
    msgs.append("%d closed %dW/%dL %.1f%% net %+.4f USDT" % (total, w, l, wr, net))
    msgs.append("%d open floating %+.4f USDT" % (n, fsum))
    if new_closes > 0:
        msgs.append("NEW CLOSES: %d (last %s)" % (new_closes, last_ts))
    if not first_run:
        if wr != state.get("wr"):
            msgs.append("winrate %.1f%% -> %.1f%%" % (state.get("wr", 0), wr))
        if abs(net - state.get("net", net)) > 1e-9:
            msgs.append("net %+.4f -> %+.4f" % (state.get("net", 0), net))

    notify(alive, total, w, l, net, last_ts, n, fsum, state, first_run, new_closes)

    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), " | ".join(msgs))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    json.dump({"total": total, "wr": wr, "net": net, "n": n, "fsum": fsum,
               "last_close_ts": last_ts, "alive": alive},
              open(STATE, "w", encoding="utf-8"))

    print(line)
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        ok = send_embed(make_embed("🧪 Test Webhook", BLUE,
                                   [{"name": "Status", "value": "Monitor OK", "inline": True}],
                                   "Pesan tes dari monitor.py"))
        sys.exit(0 if ok else 1)
    sys.exit(main())
