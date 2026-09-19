#!/usr/bin/env python3
"""
OKX -> N_eff: polygon-equivalent smoothness of the BTC price curve.

Zero dependencies (stdlib only). Fetches closed candles from OKX public
market-data API, normalises the path, and derives:

  kappa   = raw path length / smoothed path length   (circumscribed-polygon ratio)
  N_kappa = pi / sqrt(3 * (kappa - 1))               (equivalent number of sides)
  N_turn  = 2*pi / mean(|turning angle|)             (cross-check)
  C       = |sum(tau)| / sum(|tau|)                  (turn-direction consistency)
  ER      = |net move| / sum(|move|)                 (Kaufman efficiency ratio)

Env vars:
  OKX_BASE   comma-separated API hosts to try in order
  INST_ID    instrument, default BTC-USDT
  BARS       comma-separated timeframes, default 5m,15m,1H,4H,1D
  WINDOW     bars per analysis window, default 60
  SMOOTH     centred moving-average width used as the smooth baseline, default 5
  OUT_DIR    output directory, default data
"""

import csv
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASES = [b.strip() for b in os.getenv(
    "OKX_BASE",
    "https://www.okx.com,https://aws.okx.com,https://openapi.okx.com",
).split(",") if b.strip()]
INST = os.getenv("INST_ID", "BTC-USDT")
BARS = [b.strip() for b in os.getenv("BARS", "5m,15m,1H,4H,1D").split(",") if b.strip()]
WINDOW = int(os.getenv("WINDOW", "60"))
SMOOTH = int(os.getenv("SMOOTH", "5"))
OUT = Path(os.getenv("OUT_DIR", "data"))


# ---------- fetch ----------

def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "neff-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_closes(bar, limit):
    """Return (timestamps_ms, closes) ascending, confirmed candles only."""
    err = "no host tried"
    for base in BASES:
        url = f"{base}/api/v5/market/candles?instId={INST}&bar={bar}&limit={limit}"
        try:
            j = http_json(url)
        except Exception as e:
            err = f"{base} -> {e!r}"
            continue
        if j.get("code") != "0":
            err = f"{base} -> code={j.get('code')} msg={j.get('msg')}"
            continue
        rows = [r for r in j.get("data", []) if len(r) < 9 or r[8] == "1"]
        rows.sort(key=lambda r: int(r[0]))
        if len(rows) < 12:
            err = f"{base} -> only {len(rows)} confirmed candles"
            continue
        return [int(r[0]) for r in rows], [float(r[4]) for r in rows]
    raise RuntimeError(f"OKX fetch failed for bar={bar}: {err}")


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


def n_from_kappa(kappa):
    """Invert kappa(N) = (N/pi)tan(pi/N) ~= 1 + pi^2/(3N^2)."""
    if kappa <= 1 + 1e-12:
        return float("inf")
    return math.pi / math.sqrt(3.0 * (kappa - 1.0))


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

    return {
        "kappa": round(kappa, 6),
        "n_eff": cap(n_kappa),
        "n_turn": cap(n_turn),
        "consistency": round(c, 4),
        "efficiency_ratio": round(er, 4),
        "sigma_per_bar": round(sd, 4),
        "last": w[-1],
        "state": label(n_kappa, c),
    }


# ---------- output ----------

def main():
    if "--selftest" in sys.argv:
        import random
        random.seed(7)
        smooth = [100 + 20 * math.sin(i / 40) for i in range(200)]
        noisy = [p + random.gauss(0, 1.5) for p in smooth]
        print("smooth series:", analyze(smooth))
        print("noisy  series:", analyze(noisy))
        return

    now = datetime.now(timezone.utc)
    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "instrument": INST,
        "window_bars": WINDOW,
        "smooth_width": SMOOTH,
        "timeframes": {},
        "errors": {},
    }

    for bar in BARS:
        try:
            ts, closes = fetch_closes(bar, max(WINDOW + 20, 100))
            row = analyze(closes)
            row["candle_ts"] = ts[-1]
            result["timeframes"][bar] = row
            print(f"{bar}: N_eff={row['n_eff']} kappa={row['kappa']} "
                  f"C={row['consistency']} state={row['state']}")
        except Exception as e:
            result["errors"][bar] = str(e)
            print(f"{bar}: FAILED {e}", file=sys.stderr)

    if not result["timeframes"]:
        raise SystemExit("all timeframes failed; refusing to write empty output")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "neff_latest.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    hist = OUT / "neff_history.csv"
    new = not hist.exists()
    cols = ["generated_at", "instrument", "bar", "candle_ts", "last",
            "kappa", "n_eff", "n_turn", "consistency", "efficiency_ratio", "state"]
    with hist.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        if new:
            wr.writeheader()
        for bar, row in result["timeframes"].items():
            wr.writerow({
                "generated_at": result["generated_at"],
                "instrument": INST, "bar": bar,
                **{k: row[k] for k in cols[3:]},
            })


if __name__ == "__main__":
    main()
