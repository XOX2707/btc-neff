#!/usr/bin/env python3
"""Multi-timeframe agreement and geometry-as-features, out of sample.

Does not fetch. Reads panels from data/backtest/ and OHLC from data/ohlc/.

    python experiment_features.py                       # base 5m, others 1H,4H
    python experiment_features.py --base 1H --others 4H

Two questions, both against baselines that use only the past move and the
past volatility, with a 70/30 time split and nothing chosen out of sample.

Part A -- several timeframes at once. At each base bar, take the latest
*closed* row of every other timeframe (a 1H row counts only once its hour
has ended), so nothing looks ahead. Groups:

    agree           every timeframe's window moved the same way
    agree+smooth    ... and every timeframe is smooth or circular
    agree+2smooth   ... and at least two are
    agree+rough     ... and every timeframe is sharp or normal
    all smooth      every timeframe smooth or circular, any direction

The outcome is continuation along the base window's move (as in
experiment_transitions.py), in sigma*sqrt(H). "edge|r|" is against the
in-sample |return|-quintile mean; "vsAgree" is against the in-sample mean
of plain `agree`, i.e. what momentum agreement alone gives. Geometry is
worth something here only if agree+smooth beats agree.

Part B -- continuous features in a model. Two nested models per horizon:

    M0  past signed return z, |z| and sigma/price, for every timeframe
    M1  M0 + n_eff, c_ratio, ER, n_turn for every timeframe, each also
        multiplied by that timeframe's move sign, so geometry can modulate
        continuation as well as shift it

Target 1 is the sign of the forward return (logistic regression, IRLS with
a small ridge). Target 2 is |forward move| / (sigma*sqrt(H)) (least
squares). Features are standardised on in-sample; training uses every
--stride-th in-sample row (neighbouring windows share 59 of 60 bars, so
little is lost). Out of sample: log loss, accuracy and AUC for target 1,
R^2 for target 2, and the per-row M1 - M0 loss difference with a t-value
on rows thinned to H apart. A trade proxy takes sign(p - 0.5) only when
|p - 0.5| is above its in-sample 90th percentile, at most once per H bars
so trades do not overlap, and reports mean bps per trade (with t) and
"net", the same minus --cost-bps per round trip.

Standard library only, like the rest of the repo.
"""

import argparse
import bisect
import math
import sys
from pathlib import Path

import neff
import neff_offline as off

RANK = {"sharp": 0, "normal": 1, "smooth": 2, "circular": 3}
GEOM = ["n_eff", "c_ratio", "efficiency_ratio", "n_turn"]


# ---------- small linear algebra ----------

def solve(a, b):
    """Gaussian elimination with partial pivoting; a is n x n, b length n."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise ValueError("singular system")
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def _normal(xs, ys, ws, ridge):
    k = len(xs[0])
    a = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for x, y, w in zip(xs, ys, ws):
        for i in range(k):
            wi = w * x[i]
            b[i] += wi * y
            row = a[i]
            for j in range(i, k):
                row[j] += wi * x[j]
    for i in range(k):
        for j in range(i):
            a[i][j] = a[j][i]
        if i:  # no ridge on the intercept
            a[i][i] += ridge
    return a, b


def fit_ols(xs, ys, ridge=1.0):
    a, b = _normal(xs, ys, [1.0] * len(ys), ridge)
    return solve(a, b)


def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit_logit(xs, ys, ridge=1.0, iters=25):
    """IRLS. Each step solves (X'WX + R) beta = X'W z with z the working response."""
    k = len(xs[0])
    beta = [0.0] * k
    for _ in range(iters):
        ws, zs = [], []
        for x, y in zip(xs, ys):
            eta = sum(bi * xi for bi, xi in zip(beta, x))
            p = sigmoid(eta)
            w = max(p * (1 - p), 1e-6)
            ws.append(w)
            zs.append(eta + (y - p) / w)
        a, b = _normal(xs, zs, ws, ridge)
        new = solve(a, b)
        step = max(abs(n - o) for n, o in zip(new, beta))
        beta = new
        if step < 1e-6:
            break
    return beta


def dot(b, x):
    return sum(bi * xi for bi, xi in zip(b, x))


# ---------- data ----------

def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def tstat(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    m = mean(xs)
    v = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m / math.sqrt(v / n) if v > 0 else float("nan")


def auc(scores, labels):
    pairs = sorted(zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        r = (i + j + 1) / 2.0  # average 1-based rank of the tie block
        rank_sum += r * sum(lab for _, lab in pairs[i:j])
        i = j
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def timeframe_rows(inst, bar, panel_dir, ohlc_dir):
    """Panel rows with the window's signed move attached, sorted by close time."""
    panel = off.read_panel(Path(panel_dir) / f"{inst}_{bar}_panel.csv")
    candles = off.read_ohlc(Path(ohlc_dir) / f"{inst}_{bar}.csv")
    idx = {int(c["ts"]): i for i, c in enumerate(candles)}
    step = off.bar_ms(bar)
    w = neff.WINDOW
    out = []
    for r in panel:
        i = idx.get(int(r["candle_ts"]))
        if i is None or i < w - 1 or r["sigma_per_bar"] <= 0:
            continue
        c0 = candles[i]["close"]
        net = c0 - candles[i - (w - 1)]["close"]
        z = net / (r["sigma_per_bar"] * math.sqrt(w - 1))
        out.append({
            "close_ts": int(r["candle_ts"]) + step,  # when this row is known
            "i": i,
            "state": r["state"],
            "z": z,
            "d": 1.0 if net > 0 else (-1.0 if net < 0 else 0.0),
            "vol": r["sigma_per_bar"] / c0,
            "sigma": r["sigma_per_bar"],
            **{g: r[g] for g in GEOM},
        })
    return out, candles


def build(inst, base, others, horizons, panel_dir, ohlc_dir):
    base_rows, candles = timeframe_rows(inst, base, panel_dir, ohlc_dir)
    step = off.bar_ms(base)
    other_rows = {}
    for o in others:
        rows, _ = timeframe_rows(inst, o, panel_dir, ohlc_dir)
        other_rows[o] = (rows, [r["close_ts"] for r in rows])
    out = []
    for r in base_rows:
        if r["d"] == 0:
            continue
        tfs = {base: r}
        ok = True
        for o, (rows, keys) in other_rows.items():
            j = bisect.bisect_right(keys, r["close_ts"]) - 1
            # Latest closed row, and not stale (at most one bar of that tf old).
            if j < 0 or r["close_ts"] - rows[j]["close_ts"] > off.bar_ms(o):
                ok = False
                break
            tfs[o] = rows[j]
        if not ok:
            continue
        i = r["i"]
        c0 = candles[i]["close"]
        fwd = {}
        for h in horizons:
            j = i + h
            if j >= len(candles) or int(candles[j]["ts"]) - int(candles[i]["ts"]) != h * step:
                continue
            move = candles[j]["close"] - c0
            scale = r["sigma"] * math.sqrt(h)
            fwd[h] = {
                "up": 1 if move > 0 else 0,
                "cont": r["d"] * move / scale,
                "mag": abs(move) / scale,
                "bps": 1e4 * math.log(candles[j]["close"] / c0),
            }
        out.append({"ts": r["close_ts"], "tfs": tfs, "fwd": fwd})
    return out


# ---------- part A ----------

def part_a(rows, ins, oos, tf_names, horizons, cost_bps):
    def agree(r):
        ds = {r["tfs"][t]["d"] for t in tf_names}
        return len(ds) == 1 and 0.0 not in ds

    def n_smooth(r):
        return sum(RANK[r["tfs"][t]["state"]] >= 2 for t in tf_names)

    k = len(tf_names)
    groups = [
        ("all bars", lambda r: True),
        ("agree", agree),
        ("agree+smooth", lambda r: agree(r) and n_smooth(r) == k),
        ("agree+rough", lambda r: agree(r) and n_smooth(r) == 0),
        ("all smooth", lambda r: n_smooth(r) == k),
    ]
    if k >= 3:
        groups.insert(3, ("agree+2smooth", lambda r: agree(r) and n_smooth(r) >= 2))
    base = tf_names[0]
    lines = ["", f"### Part A: {'/'.join(tf_names)} at once (continuation along {base})"]
    for h in horizons:
        # |return| quintile baseline and the plain-agree mean, in-sample only.
        absz = sorted(abs(r["tfs"][base]["z"]) for r in ins)
        cuts = [absz[int(q * len(absz))] for q in (0.2, 0.4, 0.6, 0.8)]

        def q(r):
            return bisect.bisect_right(cuts, abs(r["tfs"][base]["z"]))
        bq = {}
        for b in range(5):
            xs = [r["fwd"][h]["cont"] for r in ins if h in r["fwd"] and q(r) == b]
            bq[b] = mean(xs) if xs else 0.0
        agree_is = mean([r["fwd"][h]["cont"] for r in ins if h in r["fwd"] and agree(r)])
        lines.append(f"\nH={h}  in-sample plain-agree mean cont {agree_is:+.3f}")
        lines.append(f"{'group':<16}{'part':>5}{'n':>7}{'share':>7}{'cont':>8}{'hit':>7}"
                     f"{'edge|r|':>9}{'t':>6}{'vsAgree':>9}{'t':>6}{'bps':>8}{'net':>8}")
        for name, pred in groups:
            for part, data in (("in", ins), ("out", oos)):
                ev = [r for r in data if h in r["fwd"] and pred(r)]
                share = len(ev) / len(data)
                ev = ev[::h]  # thin to H apart (rows are one base bar apart)
                if len(ev) < 2:
                    continue
                cont = [r["fwd"][h]["cont"] for r in ev]
                e_abs = [r["fwd"][h]["cont"] - bq[q(r)] for r in ev]
                e_agr = [c - agree_is for c in cont]
                bps = [r["tfs"][base]["d"] * r["fwd"][h]["bps"] for r in ev]
                lines.append(
                    f"{name:<16}{part:>5}{len(ev):>7}{share:>7.3f}{mean(cont):>+8.3f}"
                    f"{mean([1.0 if c > 0 else 0.0 for c in cont]):>7.3f}"
                    f"{mean(e_abs):>+9.3f}{tstat(e_abs):>6.1f}{mean(e_agr):>+9.3f}{tstat(e_agr):>6.1f}"
                    f"{mean(bps):>+8.1f}{mean(bps) - cost_bps:>+8.1f}" + (" *" if len(ev) < 30 else "")
                )
    lines.append(f"\nnet = bps - {cost_bps:.1f} bps round trip")
    return lines


# ---------- part B ----------

def features(r, tf_names, with_geom):
    x = [1.0]
    for t in tf_names:
        f = r["tfs"][t]
        x += [f["z"], abs(f["z"]), f["vol"]]
        if with_geom:
            for g in GEOM:
                x += [f[g], f["d"] * f[g]]
    return x


def standardise(xs_in, *others):
    k = len(xs_in[0])
    mu = [mean([x[j] for x in xs_in]) for j in range(k)]
    sd = []
    for j in range(k):
        v = mean([(x[j] - mu[j]) ** 2 for x in xs_in])
        sd.append(math.sqrt(v) if v > 0 else 1.0)
    mu[0], sd[0] = 0.0, 1.0  # keep the intercept column at 1

    def f(xs):
        return [[(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    return [f(xs_in)] + [f(o) for o in others]


def logloss(p, y):
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def part_b(ins, oos, tf_names, horizons, stride, cost_bps, ridge):
    lines = ["", f"### Part B: M0 (past return, |return|, vol on {'/'.join(tf_names)}) "
             f"vs M1 (M0 + n_eff, c_ratio, ER, n_turn and their x move-sign)"]
    lines.append(f"{'H':>3}{'target':>8}{'model':>6}{'n_oos':>7}{'logloss':>9}{'acc':>7}"
                 f"{'auc':>7}{'R2':>8}{'trades':>8}{'bps/tr':>8}{'t':>6}{'net':>8}")
    for h in horizons:
        tr = [r for r in ins if h in r["fwd"]][::stride]
        te = [r for r in oos if h in r["fwd"]]
        if len(tr) < 100 or len(te) < 100:
            lines.append(f"{h:>3}  not enough rows")
            continue
        res = {}
        for name, geom in (("M0", False), ("M1", True)):
            x_tr = [features(r, tf_names, geom) for r in tr]
            x_te = [features(r, tf_names, geom) for r in te]
            x_tr, x_te = standardise(x_tr, x_te)
            # Target 1: direction.
            y_tr = [r["fwd"][h]["up"] for r in tr]
            y_te = [r["fwd"][h]["up"] for r in te]
            beta = fit_logit(x_tr, y_tr, ridge)
            p_tr = [sigmoid(dot(beta, x)) for x in x_tr]
            p_te = [sigmoid(dot(beta, x)) for x in x_te]
            conf = sorted(abs(p - 0.5) for p in p_tr)
            thr = conf[int(0.9 * len(conf))]
            # Trades are taken at most once per H bars so their outcomes
            # do not overlap; t is then an honest count.
            trades, last = [], None
            for n, (p, r) in enumerate(zip(p_te, te)):
                if abs(p - 0.5) >= thr and (last is None or n - last >= h):
                    trades.append((1 if p > 0.5 else -1) * r["fwd"][h]["bps"])
                    last = n
            ll = [logloss(p, y) for p, y in zip(p_te, y_te)]
            # Target 2: magnitude.
            m_tr = [r["fwd"][h]["mag"] for r in tr]
            m_te = [r["fwd"][h]["mag"] for r in te]
            g = fit_ols(x_tr, m_tr, ridge)
            pred = [dot(g, x) for x in x_te]
            mu = mean(m_tr)
            sse = [(m - q) ** 2 for m, q in zip(m_te, pred)]
            sst = sum((m - mu) ** 2 for m in m_te)
            r2 = 1 - sum(sse) / sst if sst > 0 else float("nan")
            res[name] = {"ll": ll, "sse": sse}
            lines.append(
                f"{h:>3}{'dir+mag':>8}{name:>6}{len(te):>7}{mean(ll):>9.4f}"
                f"{mean([1.0 if (p > 0.5) == (y == 1) else 0.0 for p, y in zip(p_te, y_te)]):>7.3f}"
                f"{auc(p_te, y_te):>7.3f}{r2:>8.4f}{len(trades):>8}{mean(trades):>+8.2f}{tstat(trades):>6.1f}"
                f"{mean(trades) - cost_bps:>+8.2f}"
            )
        d_ll = [a - b for a, b in zip(res["M1"]["ll"], res["M0"]["ll"])][::h]
        d_se = [a - b for a, b in zip(res["M1"]["sse"], res["M0"]["sse"])][::h]
        lines.append(
            f"{h:>3}  M1-M0 out of sample: logloss {mean(d_ll):+.5f} (t {tstat(d_ll):.1f}), "
            f"squared error {mean(d_se):+.5f} (t {tstat(d_se):.1f}); negative = geometry helps"
        )
    lines.append(f"\nbps/tr is per trade before cost; net = bps/tr - {cost_bps:.1f} bps round trip")
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--base", default="5m")
    p.add_argument("--others", default="1H,4H")
    p.add_argument("--horizons", default="1,3,12,48")
    p.add_argument("--split", type=float, default=0.7)
    p.add_argument("--stride", type=int, default=4, help="train on every Nth in-sample row")
    p.add_argument("--ridge", type=float, default=1.0)
    p.add_argument("--cost-bps", type=float, default=10.0)
    p.add_argument("--panel-dir", default="data/backtest")
    p.add_argument("--ohlc-dir", default="data/ohlc")
    p.add_argument("--out-dir", default="data/backtest")
    args = p.parse_args(argv)
    try:
        base = off.parse_bars(args.base)
        if len(base) != 1:
            raise ValueError("--base takes one bar")
        base = base[0]
        others = off.parse_bars(args.others) if args.others else []
        horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
        if not horizons or min(horizons) < 1:
            raise ValueError("--horizons must be positive integers")
        if not 0.1 <= args.split <= 0.9 or args.stride < 1:
            raise ValueError("--split must be in [0.1, 0.9] and --stride >= 1")
        rows = build(args.inst, base, others, horizons, args.panel_dir, args.ohlc_dir)
        cut = int(len(rows) * args.split)
        ins, oos = rows[:cut], rows[cut:]
        if len(ins) < 100 or len(oos) < 50:
            raise ValueError(f"only {len(rows)} aligned rows")
        tf_names = [base] + others
        lines = [
            f"{args.inst} base={base} others={','.join(others) or '-'} window={neff.WINDOW} "
            f"smooth={neff.SMOOTH} cuts={neff.state_cuts()} split={args.split} stride={args.stride}",
            f"rows={len(rows)} in-sample {off.iso(ins[0]['ts'])} .. {off.iso(ins[-1]['ts'])} "
            f"out-of-sample {off.iso(oos[0]['ts'])} .. {off.iso(oos[-1]['ts'])}",
            "cont in sigma*sqrt(H); t on rows thinned to H apart; * n < 30",
        ]
        lines += part_a(rows, ins, oos, tf_names, horizons, args.cost_bps)
        lines += part_b(ins, oos, tf_names, horizons, args.stride, args.cost_bps, args.ridge)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = "\n".join(lines) + "\n"
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = f"features_{base}.txt"
    (out / name).write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
