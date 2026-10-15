#!/usr/bin/env python3
"""State transitions -> signed forward path, out of sample, against baselines.

Does not fetch. Reads the rolling panel from data/backtest/ and the OHLC
from data/ohlc/ (see BACKTEST.md for how to make both).

    python experiment_transitions.py
    python experiment_transitions.py --bars 5m,1H,4H --horizons 1,3,12,48

The question is narrower than "does N_eff predict": does a change of state
(normal -> smooth, smooth -> circular, ...) say anything about where price
goes next that the window's own past move and volatility do not already say?

Event: a panel row whose state differs from the previous row (contiguous
bars only). The rules are the live label() cuts; nothing here is tuned.

Direction: N_eff has none, so the only directional reading is
continuation. d = sign(close_t - close_{t-WINDOW+1}), the window's net move.
For each horizon H, all in units of sigma_per_bar * sqrt(H):

    cont   d * (close_{t+H} - close_t)          signed continuation
    hit    cont > 0
    mae    worst move against d over bars t+1..t+H (from high/low)
    touch  which of close_t +/- k*sigma*sqrt(H) is hit first within H,
           "with" d or "against" it; a bar that spans both is ambiguous

Baselines, both fitted on the in-sample part only:

    absret  the mean cont of all bars in the same quintile of
            |window net move| / (sigma * sqrt(WINDOW-1))  -- "only |return|"
    vol     the mean cont of all bars in the same quintile of
            sigma_per_bar / close                       -- "only volatility"

edge = mean over events of (cont - matched baseline). A state transition
is worth more only if edge is clearly non-zero, in-sample AND out of sample.

Split: by time. The first --split (0.7) of each bar's panel is in-sample,
the rest out of sample. Nothing is chosen on either part: the transition
types are fixed, the quintile cuts come from in-sample, and both parts are
printed side by side.

Overlap: forward windows of nearby events overlap and inflate t. Within
each event type an event is kept only if it is at least H bars after the
last kept one. t is mean(edge) / (sd / sqrt(n)) on those thinned events.

Costs: mean cont is also shown in bps of price, next to --cost-bps (a
round trip). An edge smaller than that is not a trade.
"""

import argparse
import math
import sys
from pathlib import Path

import neff
import neff_offline as off

ROOT = Path(__file__).resolve().parent
RANK = {"sharp": 0, "normal": 1, "smooth": 2, "circular": 3}
MIN_N = 30


def quintile_cuts(values):
    s = sorted(values)
    if not s:
        return []
    return [s[min(len(s) - 1, int(q * len(s)))] for q in (0.2, 0.4, 0.6, 0.8)]


def bucket(x, cuts):
    for i, c in enumerate(cuts):
        if x < c:
            return i
    return len(cuts)


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def tstat(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m / math.sqrt(var / n) if var > 0 else float("nan")


def build_rows(panel, candles, bar, horizons, k):
    """One dict per panel row with features and per-horizon outcomes."""
    step = off.bar_ms(bar)
    idx = {int(c["ts"]): i for i, c in enumerate(candles)}
    window = neff.WINDOW
    rows = []
    prev = None
    for r in panel:
        ts = int(r["candle_ts"])
        i = idx.get(ts)
        state = r["state"]
        contiguous = prev is not None and ts - prev["ts"] == step
        prev_state = prev["state"] if contiguous else None
        prev = {"ts": ts, "state": state}
        if i is None or i - (window - 1) < 0:
            continue
        c0 = candles[i]["close"]
        start = candles[i - (window - 1)]["close"]
        sigma = r["sigma_per_bar"]
        net = c0 - start
        if sigma <= 0 or net == 0:
            continue
        d = 1.0 if net > 0 else -1.0
        row = {
            "ts": ts,
            "state": state,
            "prev": prev_state,
            "d": d,
            "absret": abs(net) / (sigma * math.sqrt(window - 1)),
            "vol": sigma / c0,
            "out": {},
        }
        for h in horizons:
            j = i + h
            if j >= len(candles) or int(candles[j]["ts"]) - ts != h * step:
                continue
            scale = sigma * math.sqrt(h)
            path = candles[i + 1:j + 1]
            cont = d * (candles[j]["close"] - c0) / scale
            if d > 0:
                adverse = max(c0 - c["low"] for c in path)
            else:
                adverse = max(c["high"] - c0 for c in path)
            up, dn = c0 + k * scale, c0 - k * scale
            touch = "none"
            for c in path:
                hit_up, hit_dn = c["high"] >= up, c["low"] <= dn
                if hit_up and hit_dn:
                    touch = "both"
                    break
                if hit_up or hit_dn:
                    touch = "with" if (hit_up and d > 0) or (hit_dn and d < 0) else "against"
                    break
            row["out"][h] = {
                "cont": cont,
                "bps": 1e4 * d * math.log(candles[j]["close"] / c0),
                "mae": max(0.0, adverse) / scale,
                "touch": touch,
            }
        rows.append(row)
    return rows


def event_types(rows):
    """Transition label for each row, or None."""
    for r in rows:
        p, s = r["prev"], r["state"]
        if p is None or p == s:
            r["event"] = None
        else:
            r["event"] = f"{p}->{s}"
            r["dir"] = "smoother" if RANK.get(s, 0) > RANK.get(p, 0) else "rougher"


def thin(events, h):
    """Keep an event only if it is >= h bars after the last kept one."""
    kept = []
    last = None
    for e in events:
        if last is None or e["n"] - last >= h:
            kept.append(e)
            last = e["n"]
    return kept


def summarise(name, events, h, base_abs, base_vol):
    ev = [e for e in events if h in e["out"]]
    ev = thin(ev, h)
    if not ev:
        return None
    cont = [e["out"][h]["cont"] for e in ev]
    edge_abs = [e["out"][h]["cont"] - base_abs[e["qa"]] for e in ev]
    edge_vol = [e["out"][h]["cont"] - base_vol[e["qv"]] for e in ev]
    touches = [e["out"][h]["touch"] for e in ev]
    decided = [t for t in touches if t in ("with", "against")]
    return {
        "name": name,
        "n": len(ev),
        "cont": mean(cont),
        "hit": mean([1.0 if x > 0 else 0.0 for x in cont]),
        "edge_abs": mean(edge_abs),
        "t_abs": tstat(edge_abs),
        "edge_vol": mean(edge_vol),
        "t_vol": tstat(edge_vol),
        "bps": mean([e["out"][h]["bps"] for e in ev]),
        "mae": mean([e["out"][h]["mae"] for e in ev]),
        "touch_with": (sum(t == "with" for t in decided) / len(decided)) if decided else float("nan"),
    }


def run_bar(inst, bar, horizons, k, split, panel_dir, ohlc_dir, cost_bps):
    panel = off.read_panel(Path(panel_dir) / f"{inst}_{bar}_panel.csv")
    candles = off.read_ohlc(Path(ohlc_dir) / f"{inst}_{bar}.csv")
    rows = build_rows(panel, candles, bar, horizons, k)
    event_types(rows)
    for n, r in enumerate(rows):
        r["n"] = n
    cut = int(len(rows) * split)
    ins, oos = rows[:cut], rows[cut:]
    if not ins or not oos:
        raise ValueError(f"{bar}: not enough rows to split")
    qa_cuts = quintile_cuts([r["absret"] for r in ins])
    qv_cuts = quintile_cuts([r["vol"] for r in ins])
    for r in rows:
        r["qa"] = bucket(r["absret"], qa_cuts)
        r["qv"] = bucket(r["vol"], qv_cuts)

    lines = [
        f"## {inst} {bar}  rows={len(rows)}  in-sample {off.iso(ins[0]['ts'])} .. "
        f"{off.iso(ins[-1]['ts'])}  out-of-sample {off.iso(oos[0]['ts'])} .. "
        f"{off.iso(oos[-1]['ts'])}",
    ]
    types = {}
    for r in ins:
        if r["event"]:
            types[r["event"]] = types.get(r["event"], 0) + 1
    # Fixed list: every transition with enough in-sample events, plus the
    # two directions pooled. Chosen by count only, never by outcome.
    named = sorted((t for t, n in types.items() if n >= MIN_N), key=lambda t: -types[t])
    out_rows = []
    for h in horizons:
        base_abs, base_vol = {}, {}
        for q in range(5):
            ca = [r["out"][h]["cont"] for r in ins if r["qa"] == q and h in r["out"]]
            cv = [r["out"][h]["cont"] for r in ins if r["qv"] == q and h in r["out"]]
            base_abs[q] = mean(ca) if ca else 0.0
            base_vol[q] = mean(cv) if cv else 0.0
        lines.append(
            f"\n### H={h}  baseline mean cont by |ret| quintile (in-sample): "
            + " ".join(f"{base_abs[q]:+.3f}" for q in range(5))
        )
        lines.append(
            f"{'group':<22}{'part':>5}{'n':>7}{'cont':>8}{'hit':>7}"
            f"{'edge|r|':>9}{'t':>6}{'edgeVol':>9}{'t':>6}{'bps':>8}{'mae':>7}{'touchW':>8}"
        )
        groups = [("all bars", lambda r: True)]
        groups += [("any->smoother", lambda r: r["event"] and r["dir"] == "smoother"),
                   ("any->rougher", lambda r: r["event"] and r["dir"] == "rougher")]
        groups += [(t, (lambda t: lambda r: r["event"] == t)(t)) for t in named]
        for name, pred in groups:
            for part, data in (("in", ins), ("out", oos)):
                ev = [r for r in data if pred(r)]
                s = summarise(name, ev, h, base_abs, base_vol)
                if s is None:
                    continue
                s.update({"bar": bar, "h": h, "part": part})
                out_rows.append(s)
                flag = " *" if s["n"] < MIN_N else ""
                lines.append(
                    f"{name:<22}{part:>5}{s['n']:>7}{s['cont']:>+8.3f}{s['hit']:>7.3f}"
                    f"{s['edge_abs']:>+9.3f}{s['t_abs']:>6.1f}{s['edge_vol']:>+9.3f}{s['t_vol']:>6.1f}"
                    f"{s['bps']:>+8.1f}{s['mae']:>7.2f}{s['touch_with']:>8.3f}{flag}"
                )
    lines.append(f"\ncost reference: {cost_bps:.1f} bps per round trip; |bps| below that is not a trade.")
    return lines, out_rows


FIELDS = ["bar", "h", "part", "name", "n", "cont", "hit", "edge_abs", "t_abs",
          "edge_vol", "t_vol", "bps", "mae", "touch_with"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--bars", default="5m,1H,4H")
    p.add_argument("--horizons", default="1,3,12,48")
    p.add_argument("--k", type=float, default=1.0, help="touch barrier in sigma*sqrt(H)")
    p.add_argument("--split", type=float, default=0.7, help="in-sample share, by time")
    p.add_argument("--cost-bps", type=float, default=10.0)
    p.add_argument("--panel-dir", default="data/backtest")
    p.add_argument("--ohlc-dir", default="data/ohlc")
    p.add_argument("--out-dir", default="data/backtest")
    args = p.parse_args(argv)
    try:
        bars = off.parse_bars(args.bars)
        horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
        if not horizons or min(horizons) < 1:
            raise ValueError("--horizons must be positive integers")
        if not 0.1 <= args.split <= 0.9:
            raise ValueError("--split must be between 0.1 and 0.9")
        lines = [
            f"window={neff.WINDOW} smooth={neff.SMOOTH} cuts={neff.state_cuts()} "
            f"k={args.k} split={args.split}",
            "cont/edge/mae in sigma*sqrt(H); t on events thinned to >= H bars apart; * n < 30",
        ]
        rows = []
        for bar in bars:
            text, data = run_bar(args.inst, bar, horizons, args.k, args.split,
                                 args.panel_dir, args.ohlc_dir, args.cost_bps)
            lines.extend(["", *text])
            rows.extend(data)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = "\n".join(lines) + "\n"
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "transitions.txt").write_text(text, encoding="utf-8")
    off.write_csv(out / "transitions.csv", FIELDS,
                  [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k in FIELDS}
                   for r in rows])
    print(text)
    print(f"wrote {out / 'transitions.txt'}, {out / 'transitions.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
