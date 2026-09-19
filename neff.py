#!/usr/bin/env python3
"""
OKX -> N_eff: polygon-equivalent smoothness of a price curve.

Zero dependencies (stdlib only). Fetches closed candles from OKX public
market-data API for one or more instruments, normalises each path, and derives:

  kappa   = raw path length / smoothed path length   (circumscribed-polygon ratio)
  N_eff   = numerical inversion of kappa(N)=(N/pi)tan(pi/N)
  N_turn  = 2*pi / mean(|turning angle|)             (cross-check)
  C       = |sum(tau)| / sum(|tau|)                  (turn-direction consistency)
  c_null  = E[C] for an iid path of the same length  (the "no persistence" baseline)
  c_ratio = C / c_null                               (C is meaningless without this)
  ER      = |net move| / sum(|move|)                 (Kaufman efficiency ratio)

Env vars:
  OKX_BASE   comma-separated API hosts to try in order
  INST_IDS   comma-separated instruments, e.g. BTC-USDT,BTC-USDT-SWAP,ETH-USDT-SWAP
             (INST_ID is still honoured for backwards compatibility)
  BARS       comma-separated timeframes, default 5m,15m,1H,4H,1D
  WINDOW     bars per analysis window, default 60
  SMOOTH     centred moving-average width used as the smooth baseline, default 5
  REQ_DELAY  seconds to sleep between API calls, default 0.15
  OUT_DIR    output directory, default data

Modes:
  python neff.py                 fetch and write data/
  python neff.py --selftest      synthetic-data sanity check, no network
  python neff.py --list-swaps    print the live USDT-settled perpetuals you can
                                 put in INST_IDS (they end in -SWAP)
"""

import csv
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASES = [b.strip() for b in os.getenv(
    "OKX_BASE",
    "https://www.okx.com,https://aws.okx.com,https://openapi.okx.com",
).split(",") if b.strip()]

# INST_IDS is the new plural form; INST_ID stays valid so existing configs work.
INSTS = [s.strip() for s in os.getenv(
    "INST_IDS", os.getenv("INST_ID", "BTC-USDT")).split(",") if s.strip()]

BARS = [b.strip() for b in os.getenv("BARS", "5m,15m,1H,4H,1D").split(",") if b.strip()]
WINDOW = int(os.getenv("WINDOW", "60"))
SMOOTH = int(os.getenv("SMOOTH", "5"))
REQ_DELAY = float(os.getenv("REQ_DELAY", "0.15"))
OUT = Path(os.getenv("OUT_DIR", "data"))


# ---------- fetch ----------

def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "neff-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def okx_get(path, params, timeout=20):
    """GET a public endpoint, trying each host in turn."""
    q = urllib.parse.urlencode(params)
    err = "no host tried"
    for base in BASES:
        try:
            j = http_json(f"{base}{path}?{q}", timeout=timeout)
        except Exception as e:
            err = f"{base} -> {e!r}"
            continue
        if j.get("code") != "0":
            err = f"{base} -> code={j.get('code')} msg={j.get('msg')}"
            continue
        return j.get("data", [])
    raise RuntimeError(f"OKX {path} failed: {err}")


def fetch_closes(inst, bar, limit):
    """Return (timestamps_ms, closes) ascending, confirmed candles only."""
    rows = okx_get("/api/v5/market/candles",
                   {"instId": inst, "bar": bar, "limit": limit})
    rows = [r for r in rows if len(r) < 9 or r[8] == "1"]
    rows.sort(key=lambda r: int(r[0]))
    if len(rows) < 12:
        raise RuntimeError(f"only {len(rows)} confirmed candles for {inst} {bar}")
    return [int(r[0]) for r in rows], [float(r[4]) for r in rows]


def list_swaps():
    """Print live perpetuals. Their instId is what goes in INST_IDS."""
    data = okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
    live = [d for d in data if d.get("state") == "live"]
    by_settle = {}
    for d in live:
        by_settle.setdefault(d.get("settleCcy", "?"), []).append(d["instId"])
    print(f"{len(live)} live perpetuals\n")
    for ccy in sorted(by_settle, key=lambda c: -len(by_settle[c])):
        ids = sorted(by_settle[ccy])
        print(f"--- settled in {ccy} ({len(ids)}) ---")
        for i in range(0, len(ids), 4):
            print("  " + "  ".join(f"{x:<22}" for x in ids[i:i + 4]))
        print()
    print("Put a comma-separated subset in INST_IDS, e.g.")
    print("  INST_IDS='BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP'")
    print("\nEach instrument costs one API call per timeframe per run, so a long")
    print("list makes runs slow and the history file large. Pick what you watch.")


# ---------- geometry ----------

def stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def centered_ma(y, k):
    h = k // 2
    out = []
    for i in range(len(y)):
        a, b = max(0, i - h), min(len(y), i + h + 1)
        out.append(sum(y[a:b]) / (b - a))
    return out


def path_len(y):
    """Arc length with dt = 1 per bar."""
    return sum(math.hypot(1.0, y[i + 1] - y[i]) for i in range(len(y) - 1))


def turning_angles(y):
    v = [(1.0, y[i + 1] - y[i]) for i in range(len(y) - 1)]
    taus = []
    for i in range(len(v) - 1):
        ax, ay = v[i]
        bx, by = v[i + 1]
        taus.append(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
    return taus


def kappa_of(n):
    """Perimeter ratio of a regular n-gon circumscribing a circle."""
    return n * math.tan(math.pi / n) / math.pi


def n_from_kappa(kappa):
    """Invert kappa(N) numerically.

    The closed form N = pi/sqrt(3(kappa-1)) comes from tan x ~ x + x^3/3 and
    is only accurate for large N. At N=3 it understates by ~25% -- exactly the
    regime a jagged price path sits in. kappa_of is strictly decreasing on
    N > 2, so bisect instead of using the asymptotic form.
    """
    if kappa <= 1 + 1e-12:
        return float("inf")
    lo, hi = 3.0, 1e4
    if kappa >= kappa_of(lo):      # sharper than a triangle: not meaningful
        return 3.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if kappa_of(mid) > kappa:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


_CNULL = {}


def c_null(n_points, trials=400, seed=20260919):
    """E[C] for an iid-increment path of the same length.

    C is not scale-free: its value under "no directional persistence at all"
    depends on the window length, so a raw C of 0.02 means nothing until it is
    compared against this baseline. Seeded, so it does not add run-to-run noise.
    """
    if n_points in _CNULL:
        return _CNULL[n_points]
    rnd = random.Random(seed)
    tot, k = 0.0, 0
    for _ in range(trials):
        y = [0.0]
        for _ in range(n_points - 1):
            y.append(y[-1] + rnd.gauss(0, 1))
        taus = turning_angles(y)
        s = sum(abs(t) for t in taus)
        if s > 0:
            tot += abs(sum(taus)) / s
            k += 1
    _CNULL[n_points] = tot / k if k else 0.0
    return _CNULL[n_points]


def label(n_kappa, c):
    if n_kappa < 5:
        return "sharp"        # 三角形区: 尖锐折点, 趋势在硬转
    if n_kappa < 12:
        return "normal"       # 常规波动
    return "circular" if c > 0.35 else "grinding"  # 高边数: 真圆弧 vs 小幅锯齿


def analyze(closes):
    w = closes[-WINDOW:]
    d = [w[i + 1] - w[i] for i in range(len(w) - 1)]
    sd = stdev(d)
    if sd <= 0:
        raise ValueError("flat window, zero volatility")

    # normalise: 1 bar horizontally, 1 sigma of bar-to-bar move vertically
    y = [0.0]
    for x in d:
        y.append(y[-1] + x / sd)
    s = centered_ma(y, SMOOTH)

    kappa = path_len(y) / path_len(s)
    n_kappa = n_from_kappa(kappa)
    taus = turning_angles(y)
    sum_abs = sum(abs(t) for t in taus)
    n_turn = 2 * math.pi / (sum_abs / len(taus)) if sum_abs > 0 else float("inf")
    c = abs(sum(taus)) / sum_abs if sum_abs > 0 else 0.0
    er = abs(w[-1] - w[0]) / sum(abs(x) for x in d)

    def cap(x):
        return round(min(x, 999.0), 2) if math.isfinite(x) else 999.0

    cn = c_null(len(w))
    return {
        "kappa": round(kappa, 6),
        "n_eff": cap(n_kappa),
        "c_null": round(cn, 5),
        "c_ratio": round(c / cn, 3) if cn > 0 else 0.0,
        "n_turn": cap(n_turn),
        "consistency": round(c, 4),
        "efficiency_ratio": round(er, 4),
        "sigma_per_bar": round(sd, 4),
        "last": w[-1],
        "state": label(n_kappa, c),
    }


# ---------- output ----------

COLS = ["generated_at", "instrument", "bar", "candle_ts", "last",
        "kappa", "n_eff", "n_turn", "consistency", "c_null", "c_ratio",
        "efficiency_ratio", "state"]


def selftest():
    random.seed(7)
    smooth = [100 + 20 * math.sin(i / 40) for i in range(200)]
    noisy = [p + random.gauss(0, 1.5) for p in smooth]
    print("smooth series:", analyze(smooth))
    print("noisy  series:", analyze(noisy))
    print("\ninversion regression (should return N exactly):")
    for N in (3, 5, 12, 50):
        print(f"  N={N:>3} -> {n_from_kappa(kappa_of(N)):.4f}")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--list-swaps" in sys.argv:
        return list_swaps()

    now = datetime.now(timezone.utc)
    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "instruments": {},
        "window_bars": WINDOW,
        "smooth_width": SMOOTH,
    }

    first = True
    for inst in INSTS:
        entry = {"timeframes": {}, "errors": {}}
        for bar in BARS:
            if not first:
                time.sleep(REQ_DELAY)   # stay well inside the public rate limit
            first = False
            try:
                ts, closes = fetch_closes(inst, bar, max(WINDOW + 20, 100))
                row = analyze(closes)
                row["candle_ts"] = ts[-1]
                entry["timeframes"][bar] = row
                print(f"{inst} {bar}: N_eff={row['n_eff']} "
                      f"C/null={row['c_ratio']} {row['state']}")
            except Exception as e:
                entry["errors"][bar] = str(e)
                print(f"{inst} {bar}: FAILED {e}", file=sys.stderr)
        result["instruments"][inst] = entry

    if not any(v["timeframes"] for v in result["instruments"].values()):
        raise SystemExit("every instrument failed; refusing to write empty output")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "neff_latest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    hist = OUT / "neff_history.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=COLS)
        if new:
            wr.writeheader()
        for inst, entry in result["instruments"].items():
            for bar, row in entry["timeframes"].items():
                wr.writerow({
                    "generated_at": result["generated_at"],
                    "instrument": inst, "bar": bar,
                    **{k: row[k] for k in COLS[3:]},
                })


if __name__ == "__main__":
    main()
