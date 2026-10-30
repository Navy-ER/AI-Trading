#!/usr/bin/env python3
"""
Acerver Professional AI Trading Dashboard & Control Center
- Pure Python standard library (no extra pip deps)
- Real-time system monitoring (CPU Temp, Load, RAM, Disk, Uptime)
- Full trading telemetry (Open positions with live PnL, AI reasons, trades)
- Safe control API (Start / Emergency Stop)
"""
import os
import json
import wallet_manager
import time
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
TRADES_FILE = os.path.join(BASE_DIR, "trades.jsonl")
SIGNALS_FILE = os.path.join(BASE_DIR, "signals.jsonl")
OPENS_DRY = os.path.join(BASE_DIR, "open_positions.dry_run.jsonl")
OPENS_LIVE = os.path.join(BASE_DIR, "open_positions.live.jsonl")
CIRCUIT_FILE = os.path.join(BASE_DIR, "circuit_state.json")

def load_jsonl(path, max_items=50):
    if not os.path.exists(path):
        return []
    items = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass
        return items[-max_items:][::-1]
    except Exception:
        return []

def get_cpu_temp():
    temps = []
    for zone in ["/sys/class/thermal/thermal_zone0/temp",
                 "/sys/class/thermal/thermal_zone1/temp"]:
        try:
            with open(zone) as f:
                t = int(f.read().strip()) / 1000.0
                if 0 < t < 150:
                    temps.append(t)
        except Exception:
            pass
    return round(max(temps), 1) if temps else None

def get_cpu_usage():
    def read_stat():
        try:
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            idle = vals[3]
            total = sum(vals)
            return idle, total
        except Exception:
            return None, None

    idle1, total1 = read_stat()
    time.sleep(0.12)
    idle2, total2 = read_stat()
    if None in (idle1, total1, idle2, total2) or (total2 - total1) == 0:
        return None
    usage = 100.0 * (1.0 - (idle2 - idle1) / (total2 - total1))
    return round(usage, 1)

def get_ram():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
    except Exception:
        return None
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    if total == 0:
        return None
    return {
        "total_mb": round(total / 1024),
        "used_mb": round(used / 1024),
        "free_mb": round(available / 1024),
        "percent": round(used / total * 100, 1)
    }

def get_disk():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "free_gb": round(free / 1e9, 1),
            "percent": round(used / total * 100, 1)
        }
    except Exception:
        return None

def get_net_stats():
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if "wlan0" in line or "enp" in line:
                    parts = line.split()
                    iface = parts[0].strip(":")
                    rx_mb = round(int(parts[1]) / 1e6, 1)
                    tx_mb = round(int(parts[9]) / 1e6, 1)
                    return {"iface": iface, "rx_mb": rx_mb, "tx_mb": tx_mb}
    except Exception:
        pass
    return None

def get_uptime():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d = int(secs // 86400)
        h = int((secs % 86400) // 3600)
        m = int((secs % 3600) // 60)
        return f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m"
    except Exception:
        return "N/A"

def get_load_avg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {"1m": parts[0], "5m": parts[1], "15m": parts[2]}
    except Exception:
        return None

def get_stats():
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            trades.append(json.loads(line.strip()))
                        except Exception:
                            pass
        except Exception:
            pass

    # Filter hanya trade yang sudah CLOSED (bukan saat OPEN)
    closed_trades = [t for t in trades if t.get("status") == "CLOSED" or "pnl_usdt" in t]
    total_trades = len(closed_trades)
    wins = [t for t in closed_trades if t.get("pnl_usdt", 0) > 0 or t.get("result") == "WIN"]
    losses = [t for t in closed_trades if t.get("pnl_usdt", 0) < 0 or t.get("result") == "LOSS"]
    realized_pnl = sum(t.get("pnl_usdt", 0) for t in closed_trades)
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0

    open_pos = []
    for p in (OPENS_DRY, OPENS_LIVE):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            open_pos.append(json.loads(line.strip()))
            except Exception:
                pass

    total_floating = sum(p.get("floating_pnl", 0) for p in open_pos)
    total_pnl = realized_pnl

    circuit = {"date": "-", "realised_loss": 0.0, "paused": False}
    if os.path.exists(CIRCUIT_FILE):
        try:
            with open(CIRCUIT_FILE, "r", encoding="utf-8") as f:
                circuit = json.load(f)
        except Exception:
            pass

    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                logs = [line.strip() for line in f if line.strip()][-40:]
        except Exception:
            pass

    bot_alive = False
    bot_uptime = None
    try:
        sc = subprocess.run(["systemctl", "--user", "is-active", "trading-bot"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if sc.stdout.strip() == "active":
            bot_alive = True

        pg = subprocess.run(["pgrep", "-f", "bridge.py"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids = pg.stdout.strip().split()
        if pids:
            bot_alive = True
            ps = subprocess.run(["ps", "-o", "etimes=", "-p", pids[0]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                secs = int(ps.stdout.strip())
                h = secs // 3600
                m = (secs % 3600) // 60
                s = secs % 60
                bot_uptime = f"{h}h {m}m {s}s"
            except Exception:
                pass
    except Exception:
        pass

    cpu_temp = get_cpu_temp()
    ram = get_ram()
    disk = get_disk()
    net = get_net_stats()
    load = get_load_avg()
    cpu_pct = get_cpu_usage()

    temp_status = "NORMAL"
    if cpu_temp is not None:
        if cpu_temp >= 85:
            temp_status = "CRITICAL"
        elif cpu_temp >= 70:
            temp_status = "HOT"
        elif cpu_temp >= 55:
            temp_status = "WARM"

    return {
        "status": "RUNNING" if bot_alive else "STOPPED",
        "ai_primary": os.getenv("AI_MODEL", "qwen/qwen3.8-27b (Groq)"),
        "ai_fallback": os.getenv("FALLBACK_AI_MODEL", "nvidia/nemotron-3.5-lightning:free (OpenRouter)"),
        "uptime": get_uptime(),
        "bot_uptime": bot_uptime,
        "circuit": circuit,
        "wallet": wallet_manager.get_wallet_summary(open_pos),
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "realized_pnl": round(realized_pnl, 4),
        "total_floating": round(total_floating, 4),
        "total_pnl": round(realized_pnl + total_floating, 4),
        "open_positions": open_pos,
        "recent_signals": load_jsonl(SIGNALS_FILE, 15),
        "recent_trades": load_jsonl(TRADES_FILE, 15),
        "logs": logs,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system": {
            "cpu_temp": cpu_temp,
            "temp_status": temp_status,
            "cpu_percent": cpu_pct,
            "ram": ram,
            "disk": disk,
            "net": net,
            "load_avg": load,
        }
    }

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Acerver AI Trading Control Center</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #070b13;
  --bg-card: #0d1424;
  --bg-card-hover: #111a30;
  --border: #1a253c;
  --border-focus: #3b82f6;
  --text: #f1f5f9;
  --text-muted: #64748b;
  --accent: #38bdf8;
  --primary: #3b82f6;
  --green: #10b981;
  --green-glow: rgba(16, 185, 129, 0.2);
  --red: #ef4444;
  --red-glow: rgba(239, 68, 68, 0.2);
  --yellow: #f59e0b;
  --purple: #a855f7;
  --font-sans: 'Plus Jakarta Sans', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  min-height: 100vh;
  padding-bottom: 40px;
  -webkit-font-smoothing: antialiased;
}

/* NAVBAR */
header {
  background: rgba(13, 20, 36, 0.85);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  padding: 14px 28px;
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
}
.brand { display: flex; align-items: center; gap: 14px; }
.brand-badge {
  width: 40px; height: 40px;
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px;
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);
}
.brand-title h1 {
  font-size: 16px; font-weight: 800; letter-spacing: -0.3px;
  background: linear-gradient(90deg, #60a5fa, #c084fc);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.brand-title p {
  font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); margin-top: 1px;
}
.header-actions { display: flex; align-items: center; gap: 12px; }

.status-indicator {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 30px;
  font-size: 11px; font-weight: 700; font-family: var(--font-mono);
  border: 1px solid transparent;
}
.status-active {
  background: rgba(16, 185, 129, 0.12);
  color: var(--green);
  border-color: rgba(16, 185, 129, 0.3);
}
.status-stopped {
  background: rgba(239, 68, 68, 0.12);
  color: var(--red);
  border-color: rgba(239, 68, 68, 0.3);
}
.dot {
  width: 7px; height: 7px; border-radius: 50%;
}
.dot-green {
  background: var(--green);
  box-shadow: 0 0 10px var(--green);
  animation: pulse 2s infinite;
}
.dot-red {
  background: var(--red);
  box-shadow: 0 0 10px var(--red);
}
@keyframes pulse {
  0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
  70% { transform: scale(1.1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
  100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
}

.btn-control {
  cursor: pointer;
  padding: 7px 14px;
  border-radius: 8px;
  font-weight: 700;
  font-size: 11px;
  font-family: var(--font-mono);
  border: 1px solid transparent;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  display: flex; align-items: center; gap: 6px;
}
.btn-control:active { transform: scale(0.96); }
.btn-stop {
  background: rgba(239, 68, 68, 0.15);
  color: var(--red);
  border-color: rgba(239, 68, 68, 0.4);
}
.btn-stop:hover {
  background: var(--red);
  color: #fff;
  box-shadow: 0 4px 14px var(--red-glow);
}
.btn-start {
  background: rgba(16, 185, 129, 0.15);
  color: var(--green);
  border-color: rgba(16, 185, 129, 0.4);
}
.btn-start:hover {
  background: var(--green);
  color: #fff;
  box-shadow: 0 4px 14px var(--green-glow);
}

.time-sync {
  font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);
}

/* CONTAINER */
.container {
  max-width: 1440px; margin: 0 auto; padding: 24px;
  display: flex; flex-direction: column; gap: 20px;
}

/* SECTION HEADERS */
.section-title {
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  color: var(--text-muted); letter-spacing: 0.8px;
  display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
}
.section-title::before {
  content: ''; display: inline-block; width: 4px; height: 12px;
  background: var(--accent); border-radius: 2px;
}

/* HEALTH BAR GRID */
.health-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.health-tile {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  transition: border-color 0.2s, background 0.2s;
  position: relative; overflow: hidden;
}
.health-tile:hover {
  border-color: #243552;
  background: var(--bg-card-hover);
}
.tile-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.tile-label { font-size: 10px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.tile-icon { font-size: 16px; }
.tile-val { font-size: 20px; font-weight: 800; font-family: var(--font-mono); }
.tile-sub { font-size: 11px; color: var(--text-muted); margin-top: 3px; font-family: var(--font-mono); }
.progress-track {
  height: 4px; background: rgba(255, 255, 255, 0.06);
  border-radius: 4px; margin-top: 10px; overflow: hidden;
}
.progress-fill { height: 100%; border-radius: 4px; transition: width 0.4s ease; }

/* TRADING STATS ROW */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.metric-box {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  display: flex; flex-direction: column; gap: 4px;
}
.metric-name { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
.metric-val { font-size: 26px; font-weight: 800; font-family: var(--font-mono); }
.metric-desc { font-size: 11px; color: var(--text-muted); }

/* DATA TABLES */
.card-table {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  overflow: hidden;
}
.table-wrapper { overflow-x: auto; margin-top: 10px; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th {
  font-size: 10.5px; font-weight: 700; text-transform: uppercase;
  color: var(--text-muted); letter-spacing: 0.5px;
  padding: 10px 12px; border-bottom: 1px solid var(--border);
}
td {
  padding: 10px 12px; border-bottom: 1px solid rgba(26, 37, 60, 0.6);
  font-family: var(--font-mono); font-size: 12px;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(255, 255, 255, 0.02); }

.tag-side {
  padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; display: inline-block;
}
.tag-buy { background: rgba(16, 185, 129, 0.15); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }
.tag-sell { background: rgba(239, 68, 68, 0.15); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.3); }

/* SPLIT 2 COLUMN */
.two-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 960px) { .two-cols { grid-template-columns: 1fr; } }

/* LOG CONSOLE */
.console-box {
  background: #040810;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  height: 280px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.8;
  margin-top: 10px;
}
.log-row { padding: 1px 0; }

footer {
  text-align: center; color: var(--text-muted); font-size: 11px;
  font-family: var(--font-mono); margin-top: 30px;
}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-badge">⚡</div>
    <div class="brand-title">
      <h1>ACERVER TRADING INTELLIGENCE</h1>
      <p id="sub-header">192.168.18.188 · Tokocrypto · DRY_RUN · Groq (Qwen 27B) + OpenRouter</p>
    </div>
  </div>
  <div class="header-actions">
    <div class="status-indicator status-active" id="status-pill">
      <span class="dot dot-green" id="status-dot"></span>
      <span id="status-text">INITIALIZING...</span>
    </div>
    <button class="btn-control btn-stop" id="btn-ctrl" onclick="toggleBot()">🛑 EMERGENCY STOP</button>
    <span class="time-sync" id="time-sync">--:--:--</span>
  </div>
</header>

<div class="container">

  <!-- SYSTEM HEALTH TILES -->
  <div>
    <div class="section-title">Hardware Telemetry & Vital Status</div>
    <div class="health-row">
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">CPU Temp</span><span class="tile-icon">🌡️</span></div>
        <div class="tile-val" id="val-temp">--°C</div>
        <div class="tile-sub" id="sub-temp">Loading...</div>
        <div class="progress-track"><div class="progress-fill" id="bar-temp" style="width:0%;background:var(--green)"></div></div>
      </div>
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">CPU Load</span><span class="tile-icon">⚡</span></div>
        <div class="tile-val" style="color:var(--accent)" id="val-cpu">--%</div>
        <div class="tile-sub" id="sub-load">1m: -.--</div>
        <div class="progress-track"><div class="progress-fill" id="bar-cpu" style="width:0%;background:var(--accent)"></div></div>
      </div>
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">Memory</span><span class="tile-icon">🧠</span></div>
        <div class="tile-val" style="color:var(--purple)" id="val-ram">-- MB</div>
        <div class="tile-sub" id="sub-ram">-- / -- MB</div>
        <div class="progress-track"><div class="progress-fill" id="bar-ram" style="width:0%;background:var(--purple)"></div></div>
      </div>
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">Disk Storage</span><span class="tile-icon">💾</span></div>
        <div class="tile-val" style="color:#fb923c" id="val-disk">-- GB</div>
        <div class="tile-sub" id="sub-disk">-- / -- GB</div>
        <div class="progress-track"><div class="progress-fill" id="bar-disk" style="width:0%;background:#fb923c"></div></div>
      </div>
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">Network I/O</span><span class="tile-icon">📡</span></div>
        <div class="tile-val" style="color:var(--green)" id="val-net">-- MB</div>
        <div class="tile-sub" id="sub-net">↑ TX: -- MB</div>
        <div class="progress-track"><div class="progress-fill" style="width:50%;background:var(--green)"></div></div>
      </div>
      <div class="health-tile">
        <div class="tile-head"><span class="tile-label">Engine Uptime</span><span class="tile-icon">⏱️</span></div>
        <div class="tile-val" style="color:var(--yellow)" id="val-bot-up">--</div>
        <div class="tile-sub" id="sub-server-up">server: --</div>
        <div class="progress-track"><div class="progress-fill" style="width:100%;background:var(--yellow);opacity:0.3"></div></div>
      </div>
    </div>
  </div>

  <!-- 48H VIRTUAL CAPITAL SIMULATION ROW -->
  <div>
    <div class="section-title">🚀 48-Hour Virtual Capital Challenge (Budget: Rp 250.000)</div>
    <div class="metrics-grid">
      <div class="metric-box" style="border-color: rgba(99,102,241,0.4); background: linear-gradient(135deg, rgba(99,102,241,0.08), rgba(15,23,42,0.6));">
        <div class="metric-name">Total Account Equity</div>
        <div class="metric-val" id="wallet-equity" style="color:var(--accent)">15.625 USDT</div>
        <div class="metric-desc" id="wallet-equity-idr" style="color:var(--text);font-weight:600">Rp 250.000</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">48H Simulation ROI</div>
        <div class="metric-val" id="wallet-roi" style="color:var(--green)">+0.00%</div>
        <div class="metric-desc" id="wallet-pnl-idr">Profit: +Rp 0</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">Available Cash (Kas Siap Pakai)</div>
        <div class="metric-val" id="wallet-cash" style="color:var(--yellow)">14.43 USDT</div>
        <div class="metric-desc" id="wallet-cash-desc">In-Trade: 1.20 USDT</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">Countdown 48 Jam</div>
        <div class="metric-val" id="wallet-countdown" style="color:var(--purple);font-size:18px">47h 55m</div>
        <div class="metric-desc" id="wallet-target-time">Target: 30 Okt 19:57 WIB</div>
      </div>
    </div>
  </div>

  <!-- TRADING PERFORMANCE ROW -->
  <div>
    <div class="section-title">Trading Performance Telemetry</div>
    <div class="metrics-grid">
      <div class="metric-box">
        <div class="metric-name">Total Net PnL (Realized + Floating)</div>
        <div class="metric-val" id="stat-pnl" style="color:var(--green)">+0.0000 USDT</div>
        <div class="metric-desc" id="stat-pnl-desc">Realized: 0.00 USDT · Floating: 0.00 USDT</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">Win Rate</div>
        <div class="metric-val" id="stat-winrate" style="color:var(--accent)">0.0%</div>
        <div class="metric-desc" id="stat-wr-detail">0 Wins · 0 Losses</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">Total Executions</div>
        <div class="metric-val" id="stat-trades">0</div>
        <div class="metric-desc">Executed trade rounds</div>
      </div>
      <div class="metric-box">
        <div class="metric-name">Circuit Guard</div>
        <div class="metric-val" id="stat-circuit" style="color:var(--yellow)">0.00 / 2.40</div>
        <div class="metric-desc" id="stat-circuit-desc" style="color:var(--green)">Active · Safe daily boundary</div>
      </div>
    </div>
  </div>

  <!-- LIVE POSITIONS (FULL WIDTH) -->
  <div class="card-table">
    <div class="section-title" style="margin-bottom:0">Active Open Positions (Floating Telemetry)</div>
    <div class="table-wrapper">
      <table>
        <thead>
          <tr>
            <th>Asset Pair</th>
            <th>Side</th>
            <th>Entry Price</th>
            <th>Current Price</th>
            <th>Target TP / SL</th>
            <th>Confidence</th>
            <th>Floating PnL</th>
          </tr>
        </thead>
        <tbody id="pos-tbody">
          <tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px">Loading open positions...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 2 COL: AI SIGNALS + RECENT TRADES -->
  <div class="two-cols">
    <div class="card-table">
      <div class="section-title" style="margin-bottom:0">Latest AI Signals & Decision Logic</div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Coin</th>
              <th>Signal</th>
              <th>Conf</th>
              <th>Price</th>
              <th>AI Reason</th>
            </tr>
          </thead>
          <tbody id="signal-tbody">
            <tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px">No signals recorded yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card-table">
      <div class="section-title" style="margin-bottom:0">Recent Closed Trades History</div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Asset</th>
              <th>Side</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>PnL</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody id="trade-tbody">
            <tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:20px">No closed trades yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- LIVE LOG CONSOLE -->
  <div class="card-table">
    <div class="section-title" style="margin-bottom:0">
      Live Engine Stream (bridge.log)
      <span style="margin-left:auto;font-size:10px;color:var(--text-muted);font-family:var(--font-mono)" id="log-count">0 lines</span>
    </div>
    <div class="console-box" id="log-box">Streaming logs...</div>
  </div>

</div>

<footer>Acerver AI Trading Studio · Tokocrypto & Binance API via ccxt · Auto-refresh 3s · Built for Acer 24/7 Server</footer>

<script>
let isBotActive = false;
const RATE_USDT_IDR = 16000;

function formatIDR(valUSDT) {
  const idr = Math.round(valUSDT * RATE_USDT_IDR);
  return 'Rp ' + idr.toLocaleString('id-ID');
}


function getTempColor(t) {
  if (t === null || t === undefined) return 'var(--text-muted)';
  if (t >= 85) return 'var(--red)';
  if (t >= 70) return '#fb923c';
  if (t >= 55) return 'var(--yellow)';
  return 'var(--green)';
}

async function updateDashboard() {
  try {
    const res = await fetch('/api/stats');
    const d = await res.json();

    // STATUS & BUTTON
    isBotActive = d.status === 'RUNNING';
    const pill = document.getElementById('status-pill');
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    const btn = document.getElementById('btn-ctrl');

    if (isBotActive) {
      pill.className = 'status-indicator status-active';
      dot.className = 'dot dot-green';
      text.textContent = '● BOT ACTIVE';
      btn.className = 'btn-control btn-stop';
      btn.textContent = '🛑 EMERGENCY STOP';
    } else {
      pill.className = 'status-indicator status-stopped';
      dot.className = 'dot dot-red';
      text.textContent = '○ BOT STOPPED';
      btn.className = 'btn-control btn-start';
      btn.textContent = '▶ START BOT';
    }

    document.getElementById('time-sync').textContent = '↻ ' + (d.updated_at ? d.updated_at.split(' ')[1] : '--:--:--');

    // HARDWARE STATS
    const s = d.system || {};
    const temp = s.cpu_temp;
    document.getElementById('val-temp').textContent = temp !== null && temp !== undefined ? temp + '°C' : '--°C';
    document.getElementById('val-temp').style.color = getTempColor(temp);
    document.getElementById('sub-temp').textContent = s.temp_status || 'NORMAL';
    const barW = Math.min(100, Math.max(0, ((temp || 30) - 30) / 70 * 100));
    document.getElementById('bar-temp').style.width = barW + '%';
    document.getElementById('bar-temp').style.background = getTempColor(temp);

    const cpu = s.cpu_percent;
    document.getElementById('val-cpu').textContent = cpu !== null && cpu !== undefined ? cpu + '%' : '--%';
    document.getElementById('bar-cpu').style.width = (cpu || 0) + '%';
    if (s.load_avg) document.getElementById('sub-load').textContent = '1m load: ' + s.load_avg['1m'];

    if (s.ram) {
      document.getElementById('val-ram').textContent = s.ram.used_mb + ' MB';
      document.getElementById('sub-ram').textContent = s.ram.used_mb + ' / ' + s.ram.total_mb + ' MB (' + s.ram.percent + '%)';
      document.getElementById('bar-ram').style.width = s.ram.percent + '%';
    }

    if (s.disk) {
      document.getElementById('val-disk').textContent = s.disk.used_gb + ' GB';
      document.getElementById('sub-disk').textContent = s.disk.used_gb + ' / ' + s.disk.total_gb + ' GB (' + s.disk.percent + '%)';
      document.getElementById('bar-disk').style.width = s.disk.percent + '%';
    }

    if (s.net) {
      document.getElementById('val-net').textContent = '↓ ' + s.net.rx_mb + ' MB';
      document.getElementById('sub-net').textContent = '↑ TX: ' + s.net.tx_mb + ' MB (' + s.net.iface + ')';
    }

    document.getElementById('val-bot-up').textContent = d.bot_uptime || (isBotActive ? 'active' : 'stopped');
    document.getElementById('sub-server-up').textContent = 'server: ' + (d.uptime || '--');

    // VIRTUAL WALLET 48H CHALLENGE
    const w = d.wallet || {};
    if (w.total_equity !== undefined) {
      const eqEl = document.getElementById('wallet-equity');
      if (eqEl) eqEl.textContent = (w.total_equity || 0).toFixed(3) + ' USDT';
      const eqIdrEl = document.getElementById('wallet-equity-idr');
      if (eqIdrEl) eqIdrEl.textContent = 'Rp ' + Number(w.equity_idr || 250000).toLocaleString('id-ID');

      const roiEl = document.getElementById('wallet-roi');
      const roi = w.roi_pct || 0;
      if (roiEl) {
        roiEl.textContent = (roi >= 0 ? '+' : '') + roi.toFixed(2) + '%';
        roiEl.style.color = roi >= 0 ? 'var(--green)' : 'var(--red)';
      }

      const pnlIdrEl = document.getElementById('wallet-pnl-idr');
      const pnlIdr = w.pnl_idr || 0;
      if (pnlIdrEl) {
        pnlIdrEl.textContent = (pnlIdr >= 0 ? 'Profit: +Rp ' : 'Loss: -Rp ') + Math.abs(Math.round(pnlIdr)).toLocaleString('id-ID');
        pnlIdrEl.style.color = pnlIdr >= 0 ? 'var(--green)' : 'var(--red)';
      }

      const cashEl = document.getElementById('wallet-cash');
      if (cashEl) cashEl.textContent = (w.cash_balance || 0).toFixed(2) + ' USDT';
      const cashDescEl = document.getElementById('wallet-cash-desc');
      if (cashDescEl) cashDescEl.textContent = 'In-Trade: ' + (w.in_trade || 0).toFixed(2) + ' USDT (Rp ' + Number(Math.round((w.in_trade || 0) * 16000)).toLocaleString('id-ID') + ')';

      // Countdown calculator
      if (w.target_end_time) {
        const targetMs = new Date(w.target_end_time).getTime();
        const nowMs = Date.now();
        const diffMs = Math.max(0, targetMs - nowMs);
        const remH = Math.floor(diffMs / 3600000);
        const remM = Math.floor((diffMs % 3600000) / 60000);
        const cdEl = document.getElementById('wallet-countdown');
        if (cdEl) cdEl.textContent = remH + 'h ' + remM + 'm remaining';
      }
    }

    // PERFORMANCE STATS
    const netPnl = d.total_pnl !== undefined ? d.total_pnl : 0;
    const realPnl = d.realized_pnl !== undefined ? d.realized_pnl : 0;
    const floatPnl = d.total_floating !== undefined ? d.total_floating : 0;
    const pnlEl = document.getElementById('stat-pnl');
    const pnlSign = netPnl >= 0 ? '+' : '';
    pnlEl.innerHTML = `${pnlSign}${netPnl.toFixed(4)} USDT <span style="font-size:13px;font-weight:600;opacity:0.85;display:block">(${pnlSign}${formatIDR(netPnl)})</span>`;
    pnlEl.style.color = netPnl >= 0 ? 'var(--green)' : 'var(--red)';

    const pnlDescEl = document.getElementById('stat-pnl-desc');
    if (pnlDescEl) {
      pnlDescEl.innerHTML = `Realized: <b>${realPnl >= 0 ? '+' : ''}${realPnl.toFixed(4)}</b> (${formatIDR(realPnl)}) · Floating: <b>${floatPnl >= 0 ? '+' : ''}${floatPnl.toFixed(4)}</b> (${formatIDR(floatPnl)})`;
    }

    document.getElementById('stat-winrate').textContent = (d.win_rate || 0) + '%';
    document.getElementById('stat-wr-detail').textContent = (d.wins || 0) + ' Wins · ' + (d.losses || 0) + ' Losses';
    document.getElementById('stat-trades').textContent = d.total_trades || 0;

    const loss = (d.circuit || {}).realised_loss || 0;
    document.getElementById('stat-circuit').textContent = loss.toFixed(2) + ' / 2.40 USDT';
    const cDesc = document.getElementById('stat-circuit-desc');
    if ((d.circuit || {}).paused) {
      cDesc.textContent = '⛔ PAUSED · Daily loss trigger hit';
      cDesc.style.color = 'var(--red)';
    } else {
      cDesc.textContent = 'Active · Safe daily boundary';
      cDesc.style.color = 'var(--green)';
    }

    // OPEN POSITIONS
    const posTbody = document.getElementById('pos-tbody');
    if (!d.open_positions || d.open_positions.length === 0) {
      posTbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px">⏳ No open positions (Scanning markets...)</td></tr>';
    } else {
      posTbody.innerHTML = d.open_positions.map(p => {
        const fpnl = p.floating_pnl !== undefined ? p.floating_pnl : 0;
        const fpct = p.floating_pct !== undefined ? p.floating_pct : 0;
        const isWin = fpnl >= 0;
        const pCol = isWin ? 'var(--green)' : 'var(--red)';
        const curP = p.current_price !== undefined ? p.current_price : (p.entry_price || '-');
        const sideClass = p.action === 'BUY' ? 'tag-buy' : 'tag-sell';
        return `
          <tr>
            <td><b style="color:#fff">${p.asset}</b></td>
            <td><span class="tag-side ${sideClass}">${p.action}</span></td>
            <td>${p.entry_price || '-'}</td>
            <td style="color:var(--accent);font-weight:700">${curP}</td>
            <td style="font-size:11px"><span style="color:var(--green)">TP:${p.tp || '-'}</span><br><span style="color:var(--red)">SL:${p.sl || '-'}</span></td>
            <td><span style="color:var(--purple);font-weight:600">${p.confidence || '-'}</span></td>
            <td style="color:${pCol};font-weight:800">${isWin ? '+' : ''}${fpnl} USDT <span style="font-size:11px;opacity:0.9">(${isWin ? '+' : ''}${formatIDR(fpnl)})</span><br><span style="font-size:10.5px">(${isWin ? '+' : ''}${fpct}%)</span></td>
          </tr>
        `;
      }).join('');
    }

    // SIGNALS
    const sigTbody = document.getElementById('signal-tbody');
    if (!d.recent_signals || d.recent_signals.length === 0) {
      sigTbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px">No signals recorded</td></tr>';
    } else {
      sigTbody.innerHTML = d.recent_signals.slice(0, 7).map(s => {
        const t = (s.timestamp || '').split('T')[1]?.split('.')[0] || '-';
        const sideClass = s.action === 'BUY' ? 'tag-buy' : 'tag-sell';
        const reason = s.reason ? `<span style="font-size:11px;color:#cbd5e1" title="${s.reason}">${s.reason.length > 45 ? s.reason.substring(0, 42) + '...' : s.reason}</span>` : '-';
        return `
          <tr>
            <td style="color:var(--text-muted)">${t}</td>
            <td><b>${s.asset}</b></td>
            <td><span class="tag-side ${sideClass}">${s.action}</span></td>
            <td>${s.confidence}</td>
            <td>${s.price || s.entry_price || '-'}</td>
            <td>${reason}</td>
          </tr>
        `;
      }).join('');
    }

    // CLOSED TRADES
    const tradeTbody = document.getElementById('trade-tbody');
    if (!d.recent_trades || d.recent_trades.length === 0) {
      tradeTbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:20px">No trades closed yet</td></tr>';
    } else {
      tradeTbody.innerHTML = d.recent_trades.slice(0, 8).map(t => {
        const pnl = t.pnl_usdt || 0;
        const isWin = pnl > 0 || t.result === 'WIN';
        const sideClass = t.action === 'BUY' ? 'tag-buy' : 'tag-sell';
        return `
          <tr>
            <td style="color:var(--text-muted)">${(t.timestamp || t.closed_at || '').toString().split('T')[1]?.substring(0,8) || '-'}</td>
            <td><b>${t.asset || '-'}</b></td>
            <td><span class="tag-side ${sideClass}">${t.action || '-'}</span></td>
            <td>${t.entry_price || '-'}</td>
            <td>${t.exit_price || '-'}</td>
            <td style="color:${isWin?'var(--green)':'var(--red)'};font-weight:700">${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}<br><span style="font-size:10px">(${pnl >= 0 ? '+' : ''}${formatIDR(pnl)})</span></td>
            <td style="color:${isWin?'var(--green)':'var(--red)'}">${t.result || (isWin?'WIN':'LOSS')}</td>
          </tr>
        `;
      }).join('');
    }

    // LOGS
    const logBox = document.getElementById('log-box');
    const atBottom = logBox.scrollHeight - logBox.clientHeight <= logBox.scrollTop + 30;
    document.getElementById('log-count').textContent = d.logs.length + ' lines';
    logBox.innerHTML = d.logs.map(l => {
      let col = '#94a3b8';
      if (l.includes('[FILTER]') || l.includes('reject') || l.includes('skip')) col = '#f59e0b';
      if (l.includes('[AI]')) col = '#38bdf8';
      if (l.includes('CRASH') || l.includes('Error') || l.includes('ERROR')) col = '#ef4444';
      if (l.includes('BUY') || l.includes('WIN') || l.includes('Opened')) col = '#10b981';
      if (l.includes('SELL') || l.includes('LOSS')) col = '#f87171';
      if (l.includes('[BRIDGE]')) col = '#a78bfa';
      if (l.includes('DRY-RUN')) col = '#fb923c';
      return `<div class="log-row" style="color:${col}">${l}</div>`;
    }).join('');
    if (atBottom) logBox.scrollTop = logBox.scrollHeight;

  } catch (err) {
    console.error('Failed to update stats:', err);
  }
}

async function toggleBot() {
  const btn = document.getElementById('btn-ctrl');
  const action = isBotActive ? 'stop' : 'start';
  if (isBotActive && !confirm('Yakin ingin EMERGENCY STOP trading bot?')) return;
  btn.disabled = true;
  btn.style.opacity = '0.5';
  btn.textContent = 'WAIT...';
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    const result = await res.json();
    alert(result.message || 'Action executed: ' + action);
  } catch (e) {
    alert('Failed to send command: ' + e);
  } finally {
    btn.disabled = false;
    btn.style.opacity = '1';
    updateDashboard();
  }
}

updateDashboard();
setInterval(updateDashboard, 3000);
</script>
</body>
</html>
"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = get_stats()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/control":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
            try:
                data = json.loads(body)
                action = data.get('action')
                if action == 'stop':
                    subprocess.run(['systemctl', '--user', 'stop', 'trading-bot'], timeout=5)
                    resp = {'status': 'success', 'message': 'Bot successfully stopped!'}
                elif action == 'start':
                    subprocess.run(['systemctl', '--user', 'start', 'trading-bot'], timeout=5)
                    resp = {'status': 'success', 'message': 'Bot successfully started!'}
                else:
                    resp = {'status': 'error', 'message': 'Invalid action'}
            except Exception as e:
                resp = {'status': 'error', 'message': str(e)}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    PORT = 5000
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"[DASHBOARD] Clean Pro Trading Dashboard running on http://0.0.0.0:{PORT}")
    server.serve_forever()
