#!/usr/bin/env python3
"""Do passive limit orders fare better in rough states? A K-line first pass.

Does not fetch. Reads panels from data/backtest/ and OHLC from data/ohlc/.

    python experiment_execution.py
    python experiment_execution.py --bars 5m,1H --ks 0.5,1 --horizons 1,3,12

POSTMORTEM.md claims the one use of N_eff that needs no forecast: when N_eff
is low the path reverses often, so market orders keep paying the spread and
slippage, while resting orders get filled by those reversals. This tests
that claim, and only that claim, with the data we have.

At every panel bar t (close c0, sigma = sigma_per_bar) a buy limit rests at
c0 - k*sigma and a sell limit at c0 + k*sigma for --valid bars. A bar whose
low (high) reaches the price fills the buy (sell); if it opened through the
price, the fill is at the open. --through demands the price trade past the
limit by that many sigma, a crude stand-in for queue position. For each
fill and horizon H the markout is the move from the fill price to the close
H bars after the fill bar, in bps, positive when it favours the order.

The claim holds only if, after taking out what volatility and the window's
|return| already explain, rough windows (sharp + normal) get better markout
than smooth ones (smooth + circular), on both sides of a 70/30 time split.

    residual  markout minus the in-sample mean markout of all fills in the
              same quintile of sigma/price ("vol") or of |window return|
              ("|r|"); quintile cuts and means are in-sample only
    diff      rough minus smooth, mean residual; Welch t on fills thinned
              to H bars apart per side

Verdict per bar, k and H, fixed before looking: PASS only if the vol-
residual diff has the same sign in and out of sample, |t| >= 2 in both,
and the out-of-sample diff is at least --fee-gap bps (taker minus maker,
one side). Anything else is FAIL. Fill rates are reported alongside.

Limits: K-line touch fills are optimistic (no queue, no partial fills, no
spread), so a FAIL here is informative and a PASS still needs trade or
order-book data before anyone relies on it.
"""

import argparse
import bisect
import math
import sys
from pathlib import Path

import neff
import neff_offline as off

ROUGH = {"sharp", "normal"}
SMOOTH = {"smooth", "circular"}
STATES = ["sharp", "normal", "smooth", "circular"]


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def var(xs):
    if len(xs) < 2:
        return float("nan")
    m = mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    d = mean(a) - mean(b)
    se = math.sqrt(var(a) / len(a) + var(b) / len(b))
    return d, (d / se if se > 0 else float("nan"))


def quintile_cuts(values):
    s = sorted(values)
    return [s[min(len(s) - 1, int(q * len(s)))] for q in (0.2, 0.4, 0.6, 0.8)]


def orders(panel, candles, bar, ks, valid, through, horizons):
    """One record per resting order: features, fill and per-H markout."""
    step = off.bar_ms(bar)
    idx = {int(c["ts"]): i for i, c in enumerate(candles)}
    w = neff.WINDOW
    out = []
    for n, r in enumerate(panel):
        i = idx.get(int(r["candle_ts"]))
        sigma = r["sigma_per_bar"]
        if i is None or i < w - 1 or sigma <= 0:
            continue
        c0 = candles[i]["close"]
        net = c0 - candles[i - (w - 1)]["close"]
        feats = {
            "n": n,
            "state": r["state"],
            "absret": abs(net) / (sigma * math.sqrt(w - 1)),
            "vol": sigma / c0,
        }
        for k in ks:
            for side in (1, -1):  # 1 = buy below, -1 = sell above
                limit = c0 - side * k * sigma
                need = limit - side * through * sigma
                fill_j, price = None, None
                for j in range(i + 1, min(i + valid, len(candles) - 1) + 1):
                    if int(candles[j]["ts"]) - int(candles[j - 1]["ts"]) != step:
                        break
                    c = candles[j]
                    hit = c["low"] <= need if side > 0 else c["high"] >= need
                    if hit:
                        gapped = c["open"] <= limit if side > 0 else c["open"] >= limit
                        fill_j, price = j, (c["open"] if gapped else limit)
                        break
                mark = {}
                if fill_j is not None:
                    for h in horizons:
                        e = fill_j + h
                        if e < len(candles) and int(candles[e]["ts"]) - int(candles[fill_j]["ts"]) == h * step:
                            mark[h] = 1e4 * side * math.log(candles[e]["close"] / price)
                out.append({**feats, "k": k, "side": side,
                            "filled": fill_j is not None, "mark": mark})
    return out


def thin(fills, h):
    """Per side, keep a fill only if it is >= h panel rows after the last kept."""
    kept = []
    for side in (1, -1):
        last = None
        for f in sorted((f for f in fills if f["side"] == side), key=lambda f: f["n"]):
            if last is None or f["n"] - last >= h:
                kept.append(f)
                last = f["n"]
    return kept


def run_bar(inst, bar, ks, valid, through, horizons, split, fee_gap, panel_dir, ohlc_dir):
    panel = off.read_panel(Path(panel_dir) / f"{inst}_{bar}_panel.csv")
    candles = off.read_ohlc(Path(ohlc_dir) / f"{inst}_{bar}.csv")
    recs = orders(panel, candles, bar, ks, valid, through, horizons)
    if not recs:
        raise ValueError(f"{bar}: no orders")
    n_cut = int(len(panel) * split)
    ins = [r for r in recs if r["n"] < n_cut]
    oos = [r for r in recs if r["n"] >= n_cut]
    if not ins or not oos:
        raise ValueError(f"{bar}: not enough rows to split")
    cuts_v = quintile_cuts([r["vol"] for r in ins])
    cuts_a = quintile_cuts([r["absret"] for r in ins])
    for r in recs:
        r["qv"] = bisect.bisect_right(cuts_v, r["vol"])
        r["qa"] = bisect.bisect_right(cuts_a, r["absret"])

    lines = [f"## {inst} {bar}  panel rows={len(panel)}  split at {off.iso(panel[n_cut]['candle_ts'])}"]
    verdicts = []
    for k in ks:
        ins_k = [r for r in ins if r["k"] == k]
        oos_k = [r for r in oos if r["k"] == k]
        lines.append(f"\n### k={k}  (limit at close -/+ {k} sigma, valid {valid} bar(s), through {through} sigma)")
        lines.append(f"{'group':<10}{'part':>5}{'orders':>8}{'fill':>7}")
        for name, pred in [("all", lambda r: True)] + \
                [(s, (lambda s: lambda r: r["state"] == s)(s)) for s in STATES] + \
                [("rough", lambda r: r["state"] in ROUGH), ("smooth+", lambda r: r["state"] in SMOOTH)]:
            for part, data in (("in", ins_k), ("out", oos_k)):
                g = [r for r in data if pred(r)]
                if g:
                    lines.append(f"{name:<10}{part:>5}{len(g):>8}"
                                 f"{mean([1.0 if r['filled'] else 0.0 for r in g]):>7.3f}")
        for h in horizons:
            fills_in = [r for r in ins_k if h in r["mark"]]
            base_v = {q: mean([r["mark"][h] for r in fills_in if r["qv"] == q]) for q in range(5)}
            base_a = {q: mean([r["mark"][h] for r in fills_in if r["qa"] == q]) for q in range(5)}
            lines.append(f"\nk={k} H={h}  markout bps of fills (positive favours the resting order)")
            lines.append(f"{'group':<10}{'part':>5}{'fills':>7}{'mark':>8}{'res_vol':>9}{'res|r|':>9}")
            res = {}
            for part, data in (("in", ins_k), ("out", oos_k)):
                fills = thin([r for r in data if h in r["mark"]], h)
                for r in fills:
                    r["_rv"] = r["mark"][h] - base_v[r["qv"]]
                    r["_ra"] = r["mark"][h] - base_a[r["qa"]]
                for name, sel in (("all", lambda r: True),
                                  ("rough", lambda r: r["state"] in ROUGH),
                                  ("smooth+", lambda r: r["state"] in SMOOTH)):
                    g = [r for r in fills if sel(r)]
                    if g:
                        lines.append(f"{name:<10}{part:>5}{len(g):>7}{mean([r['mark'][h] for r in g]):>+8.2f}"
                                     f"{mean([r['_rv'] for r in g]):>+9.2f}{mean([r['_ra'] for r in g]):>+9.2f}")
                rough = [r for r in fills if r["state"] in ROUGH]
                smooth = [r for r in fills if r["state"] in SMOOTH]
                dv, tv = welch([r["_rv"] for r in rough], [r["_rv"] for r in smooth])
                da, ta = welch([r["_ra"] for r in rough], [r["_ra"] for r in smooth])
                res[part] = (dv, tv, da, ta, len(rough), len(smooth))
            for part in ("in", "out"):
                dv, tv, da, ta, nr, ns = res[part]
                lines.append(f"rough - smooth+ {part:>3}: vol-resid {dv:+.2f} bps (t {tv:.1f}), "
                             f"|r|-resid {da:+.2f} bps (t {ta:.1f}), n {nr} vs {ns}")
            (di, ti, *_), (do, to, *_) = res["in"], res["out"]
            ok = (di * do > 0 and abs(ti) >= 2 and abs(to) >= 2 and abs(do) >= fee_gap)
            verdict = "PASS" if ok else "FAIL"
            verdicts.append((bar, k, h, verdict))
            lines.append(f"verdict k={k} H={h}: {verdict}")
    return lines, verdicts


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--bars", default="5m,1H,4H")
    p.add_argument("--ks", default="0.5,1", help="limit offsets in sigma")
    p.add_argument("--valid", type=int, default=1, help="bars an order rests")
    p.add_argument("--through", type=float, default=0.0, help="sigma the price must trade past the limit")
    p.add_argument("--horizons", default="1,3,12")
    p.add_argument("--split", type=float, default=0.7)
    p.add_argument("--fee-gap", type=float, default=3.0, help="taker minus maker fee, bps per side")
    p.add_argument("--panel-dir", default="data/backtest")
    p.add_argument("--ohlc-dir", default="data/ohlc")
    p.add_argument("--out-dir", default="data/backtest")
    args = p.parse_args(argv)
    try:
        bars = off.parse_bars(args.bars)
        ks = [float(x) for x in args.ks.split(",") if x.strip()]
        horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
        if not ks or min(ks) < 0 or not horizons or min(horizons) < 1 or args.valid < 1:
            raise ValueError("--ks >= 0, --horizons >= 1 and --valid >= 1")
        if not 0.1 <= args.split <= 0.9:
            raise ValueError("--split must be between 0.1 and 0.9")
        lines = [
            f"window={neff.WINDOW} smooth={neff.SMOOTH} cuts={neff.state_cuts()} split={args.split} "
            f"fee_gap={args.fee_gap} bps",
            "rough = sharp + normal, smooth+ = smooth + circular; t on fills thinned to H apart per side",
        ]
        verdicts = []
        for bar in bars:
            text, v = run_bar(args.inst, bar, ks, args.valid, args.through, horizons,
                              args.split, args.fee_gap, args.panel_dir, args.ohlc_dir)
            lines += ["", *text]
            verdicts += v
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    passed = [v for v in verdicts if v[3] == "PASS"]
    lines += ["", f"verdicts: {len(passed)} PASS of {len(verdicts)}"]
    lines += [f"  PASS {b} k={k} H={h}" for b, k, h, _ in passed]
    text = "\n".join(lines) + "\n"
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "execution.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {out / 'execution.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
