#!/usr/bin/env python3
"""P2 item 3: AI accuracy eval — offline, tidak menyentuh bridge.

Untuk tiap sinyal unik di signals.jsonl: TP/SL mana yang kena duluan
(1h klines dari data-api.binance.vision, window 48h). Bandingkan
dengan baseline random-walk P(TP first) = d_sl / (d_tp + d_sl).
"""
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://data-api.binance.vision/api/v3/klines"
WINDOW_MS = 48 * 3600 * 1000

def load_signals(path):
    sigs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            tp = s.get("take_profit")
            sl = s.get("stop_loss")
            try:
                entry = float(s.get("entry_price"))
                tp = float(tp)
                sl = float(sl)
            except (TypeError, ValueError):
                continue
            if not (entry and tp and sl):
                continue
            # Level posisi ASLI (mirror add_position): ATR-based, bukan statis AI.
            # BUY: tp=entry+1.5*atr, sl=entry-1.0*atr; SELL: kebalikan.
            atr = s.get("atr")
            if atr:
                try:
                    atr = float(atr)
                    action = s.get("action", "BUY")
                    if action == "BUY":
                        s["take_profit"] = round(entry + 1.5 * atr, 8)
                        s["stop_loss"] = round(entry - 1.0 * atr, 8)
                    else:
                        s["take_profit"] = round(entry - 1.5 * atr, 8)
                        s["stop_loss"] = round(entry + 1.0 * atr, 8)
                except (TypeError, ValueError):
                    pass
            s["entry_price"] = entry
            if s.get("dynamic_tp"):
                s["dynamic_tp"] = tp
            else:
                s["take_profit"] = tp
            if s.get("dynamic_sl"):
                s["dynamic_sl"] = sl
            else:
                s["stop_loss"] = sl
            sigs.append(s)
    return sigs

def dedupe(sigs):
    seen, out = set(), []
    for s in sigs:
        key = (s["asset"], s.get("action"), round(float(s["entry_price"]), 8),
               round(float(s["take_profit"]), 8),
               round(float(s["stop_loss"]), 8))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out

def fetch_klines(asset):
    try:
        r = requests.get(BASE, params={"symbol": asset, "interval": "1h", "limit": 1000},
                         timeout=15)
        r.raise_for_status()
        return asset, r.json()
    except Exception as e:
        return asset, None

def outcome(s, klines):
    """Return 'WIN' / 'LOSS' / 'OPEN' untuk satu sinyal."""
    if not klines:
        return "SKIP"
    entry = s["entry_price"]
    action = s.get("action", "BUY")
    tp = float(s["take_profit"])
    sl = float(s["stop_loss"])
    t0 = s.get("timestamp_epoch")
    if not t0:
        return "SKIP"
    t0_ms = t0 * 1000
    t_end = t0_ms + WINDOW_MS
    for k in klines:
        t = k[0]
        if t < t0_ms:
            continue
        if t > t_end:
            break
        high, low = float(k[2]), float(k[3])
        if action == "BUY":
            if low <= sl:
                return "LOSS"
            if high >= tp:
                return "WIN"
        else:
            if high >= sl:
                return "LOSS"
            if low <= tp:
                return "WIN"
    return "OPEN"

def rand_p(s):
    entry = s["entry_price"]
    tp = float(s["take_profit"])
    sl = float(s["stop_loss"])
    d_tp = abs(tp - entry)
    d_sl = abs(sl - entry)
    if d_tp + d_sl == 0:
        return None
    return d_sl / (d_tp + d_sl)

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "signals.jsonl"
    sigs = dedupe(load_signals(path))
    print(f"signals: {len(sigs)} unik (setelah dedupe setup identik)")

    assets = sorted({s["asset"] for s in sigs})
    klines = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for asset, data in ex.map(fetch_klines, assets):
            if data is None:
                print(f"  fetch FAIL {asset}")
            klines[asset] = data
    print(f"assets: {len(assets)} (fetch OK {sum(1 for v in klines.values() if v)})")

    res = Counter()
    rand_ps = []
    by_conf = defaultdict(Counter)
    by_eval = defaultdict(Counter)
    for s in sigs:
        o = outcome(s, klines.get(s["asset"]))
        res[o] += 1
        rp = rand_p(s)
        if o in ("WIN", "LOSS") and rp is not None:
            rand_ps.append(rp)
        conf = s.get("confidence", "?")
        tier = "80+" if conf in ("90%", "85%", "80%") else ("75-79" if conf in ("79%", "78%", "77%", "76%", "75%") else "low")
        if o in ("WIN", "LOSS"):
            by_conf[tier][o] += 1
        ev = s.get("evaluated", "?")
        if o in ("WIN", "LOSS"):
            by_eval[ev][o] += 1

    decided = res["WIN"] + res["LOSS"]
    acc = res["WIN"] / decided if decided else 0
    rand_avg = sum(rand_ps) / len(rand_ps) if rand_ps else 0
    print(f"\noutcome: {dict(res)}")
    print(f"accuracy (TP-first hit rate): {acc:.1%}  ({res['WIN']}/{decided})")
    print(f"random baseline (mean P TP-first): {rand_avg:.1%}")
    print(f"edge vs random: {(acc - rand_avg):+.1%}")
    print("\nby confidence tier (WIN/LOSS/acc):")
    for tier in sorted(by_conf):
        c = by_conf[tier]
        d = c["WIN"] + c["LOSS"]
        print(f"  {tier}: {c['WIN']}/{c['LOSS']}  acc {c['WIN']/d:.1%}" if d else f"  {tier}: none")
    print("\nby evaluated:")
    for ev in sorted(by_eval):
        c = by_eval[ev]
        d = c["WIN"] + c["LOSS"]
        print(f"  {ev}: {c['WIN']}/{c['LOSS']}  acc {c['WIN']/d:.1%}" if d else f"  {ev}: none")

if __name__ == "__main__":
    main()
