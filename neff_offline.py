#!/usr/bin/env python3
"""Offline OHLC assembly, rolling N_eff replay, parity, and hypothesis tables.

No HTTP here. `backfill_ohlc.py` fetches; this module only combines pages and
runs `neff.analyze` on windows of closes.

The centered moving average inside `analyze` looks a couple of bars either
side of each point, but only inside the window it was given. A live reading
is that one window, known when the last bar of the window has closed. Replay
does the same thing: the row timestamp is the last bar, never a point in the
middle of a longer series.
"""

import csv
import math
from datetime import datetime, timezone
from pathlib import Path

import neff

# OKX bar id -> width in milliseconds. 1D/1W are fixed widths; month bars are
# not listed because their length varies.
BAR_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1H": 3_600_000,
    "2H": 2 * 3_600_000,
    "4H": 4 * 3_600_000,
    "6H": 6 * 3_600_000,
    "12H": 12 * 3_600_000,
    "1D": 86_400_000,
    "2D": 2 * 86_400_000,
    "3D": 3 * 86_400_000,
    "1W": 7 * 86_400_000,
}

OHLC_FIELDS = ["ts", "open", "high", "low", "close", "vol"]

PANEL_FIELDS = [
    "candle_ts", "candle_time", "instrument", "bar", "last",
    "kappa", "n_eff", "n_turn", "consistency", "c_null", "c_ratio",
    "efficiency_ratio", "sigma_per_bar", "state",
]

# Fields stored on both the live history CSV and the offline panel.
COMPARE_FIELDS = [
    "last", "kappa", "n_eff", "n_turn", "consistency", "c_null",
    "c_ratio", "efficiency_ratio", "state",
]

# Half a displayed step on the rounded analyze() outputs, so identical
# inputs pass and a one-tick change in the stored figure fails.
PARITY_TOL = {
    "last": 1e-4,
    "kappa": 5e-7,
    "n_eff": 5e-3,
    "n_turn": 5e-3,
    "consistency": 5e-5,
    "c_null": 5e-6,
    "c_ratio": 5e-4,
    "efficiency_ratio": 5e-5,
}

# The band splits at neff.state_cuts()["smooth_from"], the random walk's 95%
# quantile: below it a window is sharp or normal, at or above it smooth or
# circular. Like neff.label it reads the rounded n_eff column.
STOP_KS = (0.5, 1.0, 1.5)
THIN_N = 30
STATE_ORDER = ["sharp", "normal", "smooth", "circular"]

RANGE_FIELDS = [
    "bar", "group_kind", "group", "n", "thin",
    "mean_abs_dclose", "median_abs_dclose", "mean_range", "median_range",
    "mean_abs_over_sigma", "median_abs_over_sigma",
    "mean_range_over_sigma", "median_range_over_sigma",
]
STOP_FIELDS = [
    "bar", "group_kind", "group", "k", "n", "thin",
    "touch_rate", "wick_rate", "close_through_rate",
]

DISCLAIMER = (
    "Descriptive only. Each row is analyze() on the window that ends at a "
    "closed bar; the outcome is the next bar's |close change| and high-low, "
    "scaled by that window's sigma_per_bar. This is not a directional-alpha "
    "test and not a trading backtest."
)
STOP_NOTE = (
    "Stop stub, next bar only: touched when next high >= close + k*sigma or "
    "next low <= close - k*sigma. A wick is a touch whose close finishes back "
    "inside the band (inclusive). No entry, no position, no later bars."
)


def bar_ms(bar):
    try:
        return BAR_MS[bar]
    except KeyError:
        known = ", ".join(BAR_MS)
        raise ValueError(f"unknown bar {bar!r}; known: {known}") from None


def parse_bars(text):
    bars = [b.strip() for b in text.split(",") if b.strip()]
    if not bars:
        raise ValueError("no bars given")
    for b in bars:
        bar_ms(b)
    return bars


def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def check_token(value, what):
    if not value or value in (".", "..") or any(c in value for c in "/\\\t "):
        raise ValueError(f"invalid {what} {value!r}")
    return value


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


# ---------- candle pages ----------

def parse_okx_candle(raw, start_ms, end_ms):
    """One OKX candle row, or None if unconfirmed or outside the range.

    Confirmed candles have confirm == "1" in field 8. A short row (fixtures,
    older payloads) is kept. Same rule as neff.fetch_closes.
    """
    if not raw or len(raw) < 5:
        return None
    if len(raw) >= 9 and str(raw[8]) != "1":
        return None
    ts = int(raw[0])
    if ts < start_ms or ts > end_ms:
        return None
    return {
        "ts": ts,
        "open": raw[1],
        "high": raw[2],
        "low": raw[3],
        "close": raw[4],
        "vol": raw[5] if len(raw) > 5 else "",
    }


def collect_candles(fetch_page, inst, bar, start_ms, end_ms, limit=300,
                    sleep=None, path="/api/v5/market/history-candles",
                    max_pages=10000, on_page=None):
    """Walk OKX pages backward until `start_ms`.

    OKX names the cursor `after`, but it returns records *strictly earlier*
    than that timestamp (newest first). The next cursor is the oldest ts on
    the page, including an unconfirmed bar if that happened to be oldest.

    `reached_start` is true once a page contains any ts <= start_ms, which is
    the proof the walk got back to the request. A short page before that
    means the exchange ran out of history.
    """
    if sleep is None:
        def sleep():
            return None
    if limit < 1:
        raise ValueError("limit must be >= 1")

    by_ts = {}
    after = None
    pages = 0
    reached_start = False
    exhausted = False
    oldest = None
    while True:
        params = {"instId": inst, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        page = fetch_page(path, params) or []
        pages += 1
        if on_page:
            on_page(pages, page)
        if not page:
            exhausted = True
            break
        oldest = min(int(r[0]) for r in page)
        for raw in page:
            candle = parse_okx_candle(raw, start_ms, end_ms)
            if candle is not None:
                by_ts[candle["ts"]] = candle
        if oldest <= start_ms:
            reached_start = True
            break
        if len(page) < limit:
            exhausted = True
            break
        if after is not None and oldest >= int(after):
            raise RuntimeError(
                f"{path} pagination stalled for {inst} {bar}: "
                f"oldest={oldest} after={after}"
            )
        if pages >= max_pages:
            raise RuntimeError(
                f"{path} exceeded {max_pages} pages for {inst} {bar} "
                f"at ts={oldest}, start={start_ms}"
            )
        after = str(oldest)
        sleep()
    return by_ts, {
        "pages": pages,
        "reached_start": reached_start,
        "exhausted": exhausted,
        "oldest_seen": oldest,
    }


def _price_differs(a, b):
    try:
        return abs(float(a) - float(b)) > 1e-8
    except (TypeError, ValueError):
        return str(a) != str(b)


def merge_candles(base, overlay):
    """Overlay wins. Returns how many timestamps disagreed on close."""
    conflicts = 0
    for ts, row in overlay.items():
        old = base.get(ts)
        if old is not None and _price_differs(old["close"], row["close"]):
            conflicts += 1
        base[ts] = row
    return conflicts


def ordered_candles(by_ts):
    return [by_ts[ts] for ts in sorted(by_ts)]


def count_gaps(candles, bar):
    step = bar_ms(bar)
    n = 0
    first = None
    for a, b in zip(candles, candles[1:]):
        delta = int(b["ts"]) - int(a["ts"])
        if delta != step:
            n += 1
            if first is None:
                first = (int(a["ts"]), int(b["ts"]), delta)
    return n, first


# ---------- csv ----------

def write_csv(path, fieldnames, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        wr.writeheader()
        for row in rows:
            wr.writerow({k: row.get(k, "") for k in fieldnames})
    tmp.replace(path)


def write_ohlc(path, rows):
    write_csv(path, OHLC_FIELDS, rows)


def read_ohlc(path):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"missing {path}")
    with path.open(newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        raise ValueError(f"{path} is empty")
    missing = [c for c in OHLC_FIELDS if c not in raw[0]]
    if missing:
        raise ValueError(f"{path} missing columns {missing}")
    by_ts = {}
    for r in raw:
        ts = int(r["ts"])
        vol = r.get("vol", "")
        by_ts[ts] = {
            "ts": ts,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "vol": float(vol) if vol not in ("", None) else 0.0,
        }
    return [by_ts[ts] for ts in sorted(by_ts)]


def write_panel(path, rows):
    write_csv(path, PANEL_FIELDS, rows)


def read_panel(path):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"missing {path}")
    with path.open(newline="", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    if not raw:
        raise ValueError(f"{path} is empty")
    required = [c for c in PANEL_FIELDS if c != "candle_time"]
    missing = [c for c in required if c not in raw[0]]
    if missing:
        raise ValueError(f"{path} missing columns {missing}")
    numeric = [
        "last", "kappa", "n_eff", "n_turn", "consistency", "c_null",
        "c_ratio", "efficiency_ratio", "sigma_per_bar",
    ]
    out = []
    for r in raw:
        row = {
            "candle_ts": int(r["candle_ts"]),
            "candle_time": r.get("candle_time", ""),
            "instrument": r["instrument"],
            "bar": r["bar"],
            "state": r["state"],
        }
        for k in numeric:
            row[k] = float(r[k])
        out.append(row)
    return out


def read_history(path):
    path = Path(path)
    if not path.exists():
        raise ValueError(f"missing {path}")
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    need = ["generated_at", "instrument", "bar", "candle_ts"]
    missing = [c for c in need if c not in rows[0]]
    if missing:
        raise ValueError(f"{path} missing columns {missing}")
    return rows


# ---------- replay ----------

def _run_lengths(candles, step_ms):
    """How many contiguous bars end at each index, counting that bar."""
    run = [1] * len(candles)
    for i in range(1, len(candles)):
        if int(candles[i]["ts"]) - int(candles[i - 1]["ts"]) == step_ms:
            run[i] = run[i - 1] + 1
    return run


def rolling_panel(candles, inst, bar, step=1):
    """One analyze() row per closed bar that has a full contiguous window.

    `step` keeps every Nth eligible bar (1 = all). Gap-crossing windows are
    skipped: dt inside the window is 1 bar, matching the geometry.
    """
    if step < 1:
        raise ValueError("step must be >= 1")
    window = neff.WINDOW
    step_ms = bar_ms(bar)
    run = _run_lengths(candles, step_ms)
    rows = []
    skipped_gap = 0
    skipped_flat = 0
    for i in range(window - 1, len(candles)):
        if run[i] < window:
            skipped_gap += 1
            continue
        if (i - (window - 1)) % step != 0:
            continue
        closes = [float(candles[j]["close"]) for j in range(i - window + 1, i + 1)]
        try:
            metrics = neff.analyze(closes)
        except ValueError as e:
            if "flat" not in str(e):
                raise
            skipped_flat += 1
            continue
        ts = int(candles[i]["ts"])
        rows.append({
            "candle_ts": ts,
            "candle_time": iso(ts),
            "instrument": inst,
            "bar": bar,
            "last": metrics["last"],
            "kappa": metrics["kappa"],
            "n_eff": metrics["n_eff"],
            "n_turn": metrics["n_turn"],
            "consistency": metrics["consistency"],
            "c_null": metrics["c_null"],
            "c_ratio": metrics["c_ratio"],
            "efficiency_ratio": metrics["efficiency_ratio"],
            "sigma_per_bar": metrics["sigma_per_bar"],
            "state": metrics["state"],
        })
    return rows, {
        "skipped_gap": skipped_gap,
        "skipped_flat": skipped_flat,
        "window": window,
    }


# ---------- parity ----------

def values_close(field, panel_v, hist_v):
    if field == "state":
        return str(panel_v).strip() == str(hist_v).strip()
    return abs(float(panel_v) - float(hist_v)) <= PARITY_TOL[field]


def legacy_c_ratio(consistency, c_null):
    """c_ratio as migrate_history.py wrote it.

    That script divides the already-rounded consistency by the unrounded
    c_null(WINDOW), then rounds to 3 decimals. analyze() divides the
    unrounded consistency by the same baseline. The two stored figures can
    differ by about 0.001–0.003. Returns None when `c_null` is not the
    baseline this process would write, so a different window is not excused.
    """
    try:
        c = float(consistency)
        stored = float(c_null)
    except (TypeError, ValueError):
        return None
    raw = neff.c_null(neff.WINDOW)
    if raw <= 0 or abs(stored - round(raw, 5)) > PARITY_TOL["c_null"]:
        return None
    return round(c / raw, 3)


def check_parity(panel, history_rows, inst, bar):
    """Join on instrument, bar, candle_ts.

    Repeated history snapshots of the same candle keep the latest
    generated_at. If those copies disagree, the timestamp is listed in
    `conflicts` and the latest copy is still the one compared.
    """
    groups = {}
    for r in history_rows:
        if r.get("instrument") != inst or r.get("bar") != bar:
            continue
        groups.setdefault(int(r["candle_ts"]), []).append(r)

    conflicts = []
    chosen = {}
    for ts, items in groups.items():
        items = sorted(items, key=lambda r: r.get("generated_at") or "")
        signatures = {
            tuple((r.get(f) or "") for f in COMPARE_FIELDS) for r in items
        }
        if len(signatures) > 1:
            conflicts.append(ts)
        chosen[ts] = items[-1]

    panel_by = {}
    for r in panel:
        if r.get("instrument") != inst or r.get("bar") != bar:
            continue
        panel_by[int(r["candle_ts"])] = r

    matched = 0
    mismatched = []
    missing = []
    c_ratio_legacy = 0
    for ts in sorted(chosen):
        hist = chosen[ts]
        got = panel_by.get(ts)
        if got is None:
            missing.append(ts)
            continue
        diffs = []
        legacy_ratio = False
        for field in COMPARE_FIELDS:
            hv = hist.get(field, "")
            if hv is None or hv == "":
                continue
            if field in got and values_close(field, got[field], hv):
                continue
            if field == "c_ratio" and _legacy_c_ratio_ok(got, hist, hv):
                legacy_ratio = True
                continue
            diffs.append((field, got.get(field), hv))
        if diffs:
            mismatched.append({"candle_ts": ts, "diffs": diffs})
        else:
            matched += 1
            if legacy_ratio:
                c_ratio_legacy += 1

    return {
        "instrument": inst,
        "bar": bar,
        "history_rows": sum(len(v) for v in groups.values()),
        "unique": len(chosen),
        "matched": matched,
        "mismatched": mismatched,
        "missing": missing,
        "conflicts": conflicts,
        "c_ratio_legacy": c_ratio_legacy,
    }


def _legacy_c_ratio_ok(panel_row, hist, hist_c_ratio):
    """True when history c_ratio is the migrate_history figure of the same C."""
    if not panel_row:
        return False
    if "consistency" not in panel_row or "c_null" not in panel_row:
        return False
    if not values_close("consistency", panel_row["consistency"], hist.get("consistency", "")):
        return False
    if not values_close("c_null", panel_row["c_null"], hist.get("c_null", "")):
        return False
    legacy = legacy_c_ratio(hist.get("consistency"), hist.get("c_null"))
    if legacy is None:
        return False
    return abs(legacy - float(hist_c_ratio)) <= PARITY_TOL["c_ratio"]


def parity_ok(results, allow_missing=False):
    if not results:
        return False
    for r in results:
        if r["mismatched"] or r["matched"] <= 0:
            return False
        if r["missing"] and not allow_missing:
            return False
    return True


def format_parity(results, missing_files, history_path, allow_missing=False):
    lines = [
        f"history: {history_path}",
        "tolerances: "
        + ", ".join(f"{k}={v:g}" for k, v in PARITY_TOL.items())
        + ", state exact",
        "join: instrument + bar + candle_ts; duplicate snapshots use the latest generated_at",
        "",
    ]
    headers = ["bar", "history_rows", "unique", "matched", "mismatched", "missing", "conflicts"]
    table = []
    for r in results:
        table.append([
            r["bar"], r["history_rows"], r["unique"], r["matched"],
            len(r["mismatched"]), len(r["missing"]), len(r["conflicts"]),
        ])
    if table:
        lines.append(_render_table(headers, table))
    else:
        lines.append("(no panels compared)")
    lines.append("")

    for r in results:
        if r["conflicts"]:
            shown = ", ".join(iso(ts) for ts in r["conflicts"][:8])
            lines.append(
                f"{r['bar']}: {len(r['conflicts'])} candle_ts values disagree "
                f"inside the history file (compared the latest). {shown}"
            )
        if r["mismatched"]:
            lines.append(f"{r['bar']} mismatches:")
            for item in r["mismatched"][:8]:
                bits = ", ".join(
                    f"{field}: panel={got} history={hist}"
                    for field, got, hist in item["diffs"]
                )
                lines.append(f"  {iso(item['candle_ts'])} {bits}")
            extra = len(r["mismatched"]) - 8
            if extra > 0:
                lines.append(f"  ... {extra} more")
        if r.get("c_ratio_legacy"):
            n_legacy = r["c_ratio_legacy"]
            noun = "row" if n_legacy == 1 else "rows"
            lines.append(
                f"{r['bar']}: {n_legacy} {noun} where c_ratio matches "
                "migrate_history.py (rounded consistency / unrounded c_null) "
                "rather than analyze(). kappa, n_eff, consistency, ER, and "
                "state on those rows matched."
            )
        if r["missing"]:
            lines.append(
                f"{r['bar']}: {len(r['missing'])} history candle_ts not in the panel "
                "(showing up to 8). Near the start of the file this is usually "
                "warmup: the OHLC download needs WINDOW bars before that ts."
            )
            for ts in r["missing"][:8]:
                lines.append(f"  {iso(ts)} {ts}")
    if missing_files:
        lines.append("missing panel files:")
        for path in missing_files:
            lines.append(f"  {path}")
    ok = parity_ok(results, allow_missing=allow_missing) and not missing_files
    lines.append("")
    lines.append("PASS" if ok else "FAIL")
    return "\n".join(lines)


# ---------- hypothesis ----------

def _tercile_labels(samples):
    if len(samples) < 3:
        return None, "terciles skipped: fewer than 3 samples"
    xs = sorted(s["n_eff"] for s in samples)
    n = len(xs)
    lo = xs[n // 3]
    hi = xs[(2 * n) // 3]
    if not lo < hi:
        return None, "terciles skipped: n_eff has no spread in this sample"
    labels = []
    for s in samples:
        if s["n_eff"] <= lo:
            labels.append("low")
        elif s["n_eff"] >= hi:
            labels.append("high")
        else:
            labels.append("mid")
    return labels, (
        f"tercile cuts are order statistics n/3={lo:.2f} and 2n/3={hi:.2f} "
        "of this sample's n_eff (low = at or below the lower cut)"
    )


def _contrast(samples, pred_a, pred_b, label):
    a = [s for s in samples if pred_a(s)]
    b = [s for s in samples if pred_b(s)]
    out = {"label": label, "ok": bool(a and b), "n_a": len(a), "n_b": len(b)}
    if not out["ok"]:
        return out
    for key, name in (
        ("abs_over_sigma", "median_abs_over_sigma"),
        ("range_over_sigma", "median_range_over_sigma"),
    ):
        out[name + "_a"] = median([s[key] for s in a])
        out[name + "_b"] = median([s[key] for s in b])
    return out


def _range_row(bar, kind, name, group):
    n = len(group)
    return {
        "bar": bar,
        "group_kind": kind,
        "group": name,
        "n": n,
        "thin": int(n < THIN_N),
        "mean_abs_dclose": mean([s["abs_dclose"] for s in group]),
        "median_abs_dclose": median([s["abs_dclose"] for s in group]),
        "mean_range": mean([s["range"] for s in group]),
        "median_range": median([s["range"] for s in group]),
        "mean_abs_over_sigma": mean([s["abs_over_sigma"] for s in group]),
        "median_abs_over_sigma": median([s["abs_over_sigma"] for s in group]),
        "mean_range_over_sigma": mean([s["range_over_sigma"] for s in group]),
        "median_range_over_sigma": median([s["range_over_sigma"] for s in group]),
    }


def _stop_rows(bar, kind, name, group):
    rows = []
    n = len(group)
    for k in STOP_KS:
        touched = 0
        wick = 0
        for s in group:
            st = s["stops"][k]
            touched += int(st["touched"])
            wick += int(st["wick"])
        rows.append({
            "bar": bar,
            "group_kind": kind,
            "group": name,
            "k": k,
            "n": n,
            "thin": int(n < THIN_N),
            "touch_rate": touched / n,
            "wick_rate": wick / n,
            "close_through_rate": (touched - wick) / n,
        })
    return rows


def build_hypothesis(panel, candles, bar):
    """Next-bar range proxy and a one-bar stop-touch stub.

    The signal bar's own high/low are not the outcome: they are part of the
    path that produced the reading. The next bar is used only when it sits
    exactly one `bar` step later, so a gap is not filled with a later candle.
    """
    step = bar_ms(bar)
    by_ts = {int(c["ts"]): c for c in candles}
    samples = []
    skipped_no_next = 0
    skipped_mismatch = 0
    skipped_bad = 0
    for row in panel:
        if row.get("bar") != bar:
            continue
        ts = int(row["candle_ts"])
        cur = by_ts.get(ts)
        nxt = by_ts.get(ts + step)
        if cur is None or nxt is None:
            skipped_no_next += 1
            continue
        entry = float(cur["close"])
        scale = max(1e-4, 1e-8 * abs(entry))
        if abs(entry - float(row["last"])) > scale:
            skipped_mismatch += 1
            continue
        sigma = float(row["sigma_per_bar"])
        hi = float(nxt["high"])
        lo = float(nxt["low"])
        if sigma <= 0 or hi < lo:
            skipped_bad += 1
            continue
        close = float(nxt["close"])
        stops = {}
        for k in STOP_KS:
            up = entry + k * sigma
            dn = entry - k * sigma
            touched = hi >= up or lo <= dn
            inside = dn <= close <= up
            stops[k] = {"touched": touched, "wick": touched and inside}
        samples.append({
            "state": row["state"],
            "n_eff": float(row["n_eff"]),
            "abs_dclose": abs(close - entry),
            "range": hi - lo,
            "abs_over_sigma": abs(close - entry) / sigma,
            "range_over_sigma": (hi - lo) / sigma,
            "stops": stops,
        })

    notes = []
    if not samples:
        notes.append("no next-bar samples")
        return {
            "bar": bar,
            "n": 0,
            "skipped_no_next": skipped_no_next,
            "skipped_mismatch": skipped_mismatch,
            "skipped_bad": skipped_bad,
            "states": {},
            "range_rows": [],
            "stop_rows": [],
            "contrasts": [],
            "notes": notes,
        }

    labels, tercile_note = _tercile_labels(samples)
    if tercile_note:
        notes.append(tercile_note)
    if labels:
        for s, lab in zip(samples, labels):
            s["tercile"] = lab

    buckets = [("all", "all", samples)]
    by_state = {}
    for s in samples:
        by_state.setdefault(s["state"], []).append(s)
    state_names = [name for name in STATE_ORDER if name in by_state]
    state_names += sorted(name for name in by_state if name not in STATE_ORDER)
    for name in state_names:
        buckets.append(("state", name, by_state[name]))
    cut = neff.state_cuts()["smooth_from"]
    lt = [s for s in samples if s["n_eff"] < cut]
    ge = [s for s in samples if s["n_eff"] >= cut]
    if lt:
        buckets.append(("n_eff_band", "lt_q95", lt))
    if ge:
        buckets.append(("n_eff_band", "ge_q95", ge))
    if labels:
        for name in ("low", "mid", "high"):
            chunk = [s for s in samples if s.get("tercile") == name]
            if chunk:
                buckets.append(("n_eff_tercile", name, chunk))

    range_rows = [_range_row(bar, kind, name, group) for kind, name, group in buckets]
    stop_rows = []
    for kind, name, group in buckets:
        stop_rows.extend(_stop_rows(bar, kind, name, group))

    contrasts = [
        _contrast(
            samples,
            lambda s: s["state"] == "normal",
            lambda s: s["state"] == "smooth",
            "normal vs smooth",
        ),
        _contrast(
            samples,
            lambda s: s["n_eff"] < cut,
            lambda s: s["n_eff"] >= cut,
            "n_eff<q95 vs n_eff>=q95",
        ),
    ]
    if labels:
        contrasts.append(_contrast(
            samples,
            lambda s: s.get("tercile") == "low",
            lambda s: s.get("tercile") == "high",
            "tercile low vs high",
        ))

    states = {}
    for s in samples:
        states[s["state"]] = states.get(s["state"], 0) + 1
    return {
        "bar": bar,
        "n": len(samples),
        "skipped_no_next": skipped_no_next,
        "skipped_mismatch": skipped_mismatch,
        "skipped_bad": skipped_bad,
        "states": states,
        "range_rows": range_rows,
        "stop_rows": stop_rows,
        "contrasts": contrasts,
        "notes": notes,
    }


def _fmt_num(x, digits):
    return f"{x:.{digits}f}"


def _fmt_ratio(a, b):
    if b == 0:
        return "inf" if a else "nan"
    return f"{a / b:.3f}"


def _fmt_contrast(c):
    if not c["ok"]:
        return f"  {c['label']}: not comparable (n={c['n_a']} vs n={c['n_b']})"
    return (
        f"  {c['label']} (n={c['n_a']} vs {c['n_b']}): "
        f"median |dclose|/sigma {_fmt_num(c['median_abs_over_sigma_a'], 4)} vs "
        f"{_fmt_num(c['median_abs_over_sigma_b'], 4)} "
        f"(ratio {_fmt_ratio(c['median_abs_over_sigma_a'], c['median_abs_over_sigma_b'])}); "
        f"median range/sigma {_fmt_num(c['median_range_over_sigma_a'], 4)} vs "
        f"{_fmt_num(c['median_range_over_sigma_b'], 4)} "
        f"(ratio {_fmt_ratio(c['median_range_over_sigma_a'], c['median_range_over_sigma_b'])})"
    )


def _render_table(headers, rows):
    text = [[str(c) for c in headers]]
    text += [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in text) for i in range(len(headers))]
    lines = []
    for idx, row in enumerate(text):
        lines.append("  ".join(cell.rjust(widths[i]) for i, cell in enumerate(row)))
        if idx == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def format_hypothesis(hyps):
    lines = [
        DISCLAIMER,
        "",
        STOP_NOTE,
        "",
        f"Groups with n < {THIN_N} are marked *.",
        "Ratio in a contrast is left/right. Near 1 means this sample does not separate those groups on that proxy.",
        "",
    ]
    for h in hyps:
        lines.append(
            f"## {h['bar']}  samples={h['n']}  "
            f"skipped_no_next={h['skipped_no_next']}  "
            f"skipped_close_mismatch={h['skipped_mismatch']}  "
            f"skipped_bad={h['skipped_bad']}  "
            f"states={h['states']}"
        )
        if h["range_rows"]:
            headers = [
                "group_kind", "group", "n",
                "med_|dc|/sig", "mean_|dc|/sig", "med_range/sig", "mean_range/sig",
            ]
            rows = []
            for r in h["range_rows"]:
                name = r["group"] + ("*" if r["thin"] else "")
                rows.append([
                    r["group_kind"], name, r["n"],
                    _fmt_num(r["median_abs_over_sigma"], 4),
                    _fmt_num(r["mean_abs_over_sigma"], 4),
                    _fmt_num(r["median_range_over_sigma"], 4),
                    _fmt_num(r["mean_range_over_sigma"], 4),
                ])
            lines.append(_render_table(headers, rows))
        lines.append("contrasts:")
        if h["contrasts"]:
            for c in h["contrasts"]:
                lines.append(_fmt_contrast(c))
        else:
            lines.append("  (none)")
        if h["stop_rows"]:
            lines.append("stop stub:")
            headers = ["group_kind", "group", "k", "n", "touch", "wick", "through"]
            rows = []
            for r in h["stop_rows"]:
                name = r["group"] + ("*" if r["thin"] else "")
                rows.append([
                    r["group_kind"], name, f"{r['k']:.1f}", r["n"],
                    _fmt_num(r["touch_rate"], 4),
                    _fmt_num(r["wick_rate"], 4),
                    _fmt_num(r["close_through_rate"], 4),
                ])
            lines.append(_render_table(headers, rows))
        for note in h["notes"]:
            lines.append(f"note: {note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _csv_num(value, digits=6):
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return value


def hypothesis_csv_rows(hyps):
    range_rows = []
    stop_rows = []
    for h in hyps:
        for r in h["range_rows"]:
            range_rows.append({k: _csv_num(r[k]) for k in RANGE_FIELDS})
        for r in h["stop_rows"]:
            stop_rows.append({k: _csv_num(r[k]) for k in STOP_FIELDS})
    return range_rows, stop_rows
