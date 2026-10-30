# Go-Live Checklist — AI Trading Bot (acerver)

Status sekarang: **DRY_RUN**. Jangan ubah `TKO_DRY_RUN` sampai semua gate di
bawah terpenuhi dan ada keputusan eksplisit untuk pakai uang beneran.

## 0. Sebelum mulai

```bash
cd /home/server/trading-bot
systemctl --user status trading-bot --no-pager | head -5   # harus active
grep -E '^TKO_DRY_RUN=' .env                              # harus 1
pgrep -af bridge.py                                        # HARUS cuma 1 proses
sha256sum bridge.py                                        # cocokkan dengan catatan deploy
```

Kalau `pgrep` menunjukkan >1 proses, hentikan semuanya dulu. Instance lock
(`[LOCK] Instance bridge lain sudah jalan`) akan menolak instance kedua, tapi
lebih aman dibereskan manual.

## 1. Gate data (minimal sebelum LIVE)

Jalankan di acerver, setelah minimal **7 hari** atau **30 trade closed**,
mana yang lebih dulu:

```bash
python3 winrate.py          # winrate + total PnL closed TP/SL
python3 eval_accuracy.py signals.jsonl
```

Gate yang harus lolos **semua**:

- [ ] Winrate ≥ 45% (dengan R:R ≥ 1.5, break-even sekitar 40%).
- [ ] Total PnL DRY_RUN **positif setelah fee** (asumsi fee taker 0.1% × 2 sisi).
- [ ] Profit factor ≥ 1.3 (gross win / gross loss).
- [ ] Max drawdown beruntun ≤ `TKO_DAILY_LOSS_LIMIT` (2.40 USDT) — kalau sering kena circuit, filter terlalu longgar.
- [ ] `eval_accuracy.py`: P(TP first) lebih tinggi dari baseline random walk.
- [ ] Tidak ada hari dengan >2 entry yang kena `price_sanity` / `no_price` (artinya data harga bermasalah).

Cek statistik alasan reject (tuning filter pakai data):

```bash
tail -n 50 bridge.log | grep '\[STATS\]'
python3 - <<'PY'
import json, collections
c = collections.Counter()
for line in open('reject_stats.jsonl'):
    rec = json.loads(line)
    c.update(rec.get('counts', {}))
for k, v in c.most_common():
    print(f'{k:16} {v}')
PY
```

Kalau `cooldown`/`adx_low` mendominasi, itu wajar. Kalau `price_sanity` besar,
jangan live dulu — cek koneksi/data feed.

## 2. Gate keamanan exchange

- [ ] API key Tokocrypto **tanpa izin withdraw**, hanya spot trade.
- [ ] IP whitelist diisi IP acerver (kalau exchange mendukung).
- [ ] Saldo akun = uang yang siap hilang. `TKO_MAX_POSITION_USDT` × `TKO_MAX_SLOTS` ≤ 25 USDT.
- [ ] `TKO_DAILY_LOSS_LIMIT` dan `TKO_MAX_CONSECUTIVE_LOSSES` sudah diisi realistis.
- [ ] Restart test: `systemctl --user restart trading-bot` lalu pastikan posisi lama terbaca ulang (`[POS] ... open`), tidak ada posisi yatim.

## 3. Prosedur flip ke LIVE

```bash
cd /home/server/trading-bot
cp -p .env ".env.bak.$(date +%Y%m%d_%H%M%S)"
sed -i 's/^TKO_DRY_RUN=.*/TKO_DRY_RUN=0/' .env
grep -E '^TKO_DRY_RUN=' .env          # harus 0
systemctl --user restart trading-bot
sleep 5
systemctl --user status trading-bot --no-pager | head -5
tail -n 20 bridge.log                 # harus "TRADING_MODE=LIVE"
```

Ulangi dengan modal minimum (`TKO_MAX_POSITION_USDT=1.20`, `TKO_MAX_SLOTS=3`)
untuk 10 trade pertama. Baru naikkan setelah evaluasi.

## 4. Rollback (kalau ada yang aneh)

```bash
cd /home/server/trading-bot
sed -i 's/^TKO_DRY_RUN=.*/TKO_DRY_RUN=1/' .env
systemctl --user restart trading-bot
```

Rollback mode **tidak** menutup posisi yang sudah terbuka. Posisi LIVE tetap
dikelola (TP/SL) oleh proses yang sama; kalau perlu tutup manual di exchange,
lakukan lalu pastikan `open_positions.live.jsonl` konsisten.

## 5. Monitoring harian

- Dashboard: `http://192.168.18.188:5000`
- Log: `tail -f /home/server/trading-bot/bridge.log`
- Alert Discord: `Bridge DOWN`, `Tidak ada kandidat lolos`, circuit breaker.
- Cek harian: `python3 winrate.py`, `grep '\[STATS\]' bridge.log | tail`.

## 6. Deploy ulang kode

Dari mesin lokal (Git Bash):

```bash
./deploy.sh
```

Script akan compile check, backup, install, restart, health check, dan
**otomatis rollback** kalau service tidak hidup. Backup manual tersimpan di
`/home/server/trading-bot/.backup-<timestamp>/`.
