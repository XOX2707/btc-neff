#!/usr/bin/env python3
"""Write the small Pages summary under data/backtest_web/.

Does not fetch. Does not copy OHLC or the rolling panel. Reads each
``{inst}_{bar}_panel.csv`` in ``data/backtest/``, and — when the matching
OHLC file is present — rebuilds the hypothesis tables with
``neff_offline.build_hypothesis`` (the same function that writes
``hypothesis.txt``). Parity is ``neff_offline.check_parity`` against
``data/neff_history.csv``.

    python export_backtest_web.py
    python export_backtest_web.py --placeholder

``--placeholder`` writes an explicit empty summary and removes any stale
hypothesis CSVs in the output directory. It does not invent ratios.
A normal run with no panels exits 1.

WINDOW and SMOOTH are the live env vars. Export them the same way you
ran ``backtest_neff.py``, or the recorded window will not match the panel.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import neff
import neff_offline as off

ROOT = Path(__file__).resolve().parent

DISCLAIMER_ZH = (
    "不回答该不该买。N_eff 是走完之后的几何描述：每一行都是窗口右端那根已收盘 K 线的读数，"
    "结果只看下一根的 |Δclose| 和 high−low，再除以该窗口的 σ。"
    "没有方向，没有仓位，没有成本。"
    "两组中位数之比接近 1，只说明这批样本在这个代理上没有分开，不是可以兑现的优势。"
)
STOP_NOTE_ZH = (
    "止损只是存根。下一根的最高价或最低价碰到收盘价上下 k·σ，记为触碰；"
    "碰到了但收盘回到带内，记为影线。不建仓，也不看更后面的 K 线。k 取 0.5、1、1.5。"
)
PLACEHOLDER_NOTE = (
    "本地还没有 data/backtest/ 里的面板，所以这里没有根数、对账和波动表。"
    "先回补 K 线并跑 python backtest_neff.py all，再执行 python export_backtest_web.py。"
    "这个占位文件不会补造数字。"
)

RANGE_FLOATS = [
    "mean_abs_dclose", "median_abs_dclose", "mean_range", "median_range",
    "mean_abs_over_sigma", "median_abs_over_sigma",
    "mean_range_over_sigma", "median_range_over_sigma",
]
STOP_FLOATS = ["k", "touch_rate", "wick_rate", "close_through_rate"]


def rel(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _round(value, digits=6):
    if isinstance(value, float):
        return round(value, digits)
    return value


def _ratio(a, b):
    if not b:
        return None
    return round(a / b, 6)


def notes_zh(notes):
    out = []
    for note in notes:
        m = re.search(r"n/3=([0-9.]+) and 2n/3=([0-9.]+)", note)
        if m:
            out.append(
                "三分位切在这份样本的 N_eff："
                f"n/3={m.group(1)}，2n/3={m.group(2)}（低 = 不超过下切点）。"
            )
            continue
        if "fewer than 3" in note:
            out.append("样本少于 3 根，没有做三分位。")
            continue
        if "no spread" in note:
            out.append("这份样本的 N_eff 没有拉开，没有做三分位。")
            continue
        if note == "no next-bar samples":
            out.append("没有能配上下一根的样本。")
            continue
        out.append(note)
    return out


def _state_counts(panel):
    counts = {name: 0 for name in off.STATE_ORDER}
    for row in panel:
        name = row["state"]
        counts[name] = counts.get(name, 0) + 1
    return counts


def _iso_ms(ms):
    return off.iso(ms)


def bar_meta(panel, candles, bar):
    panel = sorted(panel, key=lambda r: int(r["candle_ts"]))
    start = int(panel[0]["candle_ts"])
    end = int(panel[-1]["candle_ts"])
    meta = {
        "bar": bar,
        "ohlc_present": candles is not None,
        "candles": None if candles is None else len(candles),
        "gaps": None,
        "ohlc_start": None,
        "ohlc_end": None,
        "signals": len(panel),
        "signal_start": _iso_ms(start),
        "signal_end": _iso_ms(end),
        "span_days": round((end - start) / 86_400_000, 2),
        "states": _state_counts(panel),
    }
    if candles:
        gaps, _first = off.count_gaps(candles, bar)
        meta["gaps"] = gaps
        meta["ohlc_start"] = _iso_ms(candles[0]["ts"])
        meta["ohlc_end"] = _iso_ms(candles[-1]["ts"])
    return meta


def _num_row(row, floats):
    out = dict(row)
    for key in floats:
        if key in out and out[key] != "":
            out[key] = _round(float(out[key]))
    if "n" in out:
        out["n"] = int(out["n"])
    if "thin" in out:
        out["thin"] = bool(int(out["thin"]))
    if "k" in out:
        out["k"] = _round(float(out["k"]))
    return out


def _read_typed_csv(path, floats):
    path = Path(path)
    if not path.exists():
        return None
    import csv
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [_num_row(r, floats) for r in rows]


def contrasts_from_rows(range_rows):
    """Ratios of medians already stored on the hypothesis range table."""
    index = {(r["group_kind"], r["group"]): r for r in range_rows}

    def one(label, kind, a_name, b_name):
        a = index.get((kind, a_name))
        b = index.get((kind, b_name))
        if not a or not b or int(a["n"]) <= 0 or int(b["n"]) <= 0:
            return {
                "label": label,
                "ok": False,
                "n_a": 0 if not a else int(a["n"]),
                "n_b": 0 if not b else int(b["n"]),
            }
        aa = float(a["median_abs_over_sigma"])
        ab = float(b["median_abs_over_sigma"])
        ra = float(a["median_range_over_sigma"])
        rb = float(b["median_range_over_sigma"])
        return {
            "label": label,
            "ok": True,
            "n_a": int(a["n"]),
            "n_b": int(b["n"]),
            "median_abs_over_sigma_a": _round(aa),
            "median_abs_over_sigma_b": _round(ab),
            "ratio_abs": _ratio(aa, ab),
            "median_range_over_sigma_a": _round(ra),
            "median_range_over_sigma_b": _round(rb),
            "ratio_range": _ratio(ra, rb),
        }

    found = []
    pairs = (
        ("normal vs smooth", "state", "normal", "smooth"),
        ("n_eff<q95 vs n_eff>=q95", "n_eff_band", "lt_q95", "ge_q95"),
        ("tercile low vs high", "n_eff_tercile", "low", "high"),
    )
    for label, kind, a_name, b_name in pairs:
        item = one(label, kind, a_name, b_name)
        if item["ok"] or item["n_a"] or item["n_b"]:
            found.append(item)
    return found


def _contrast_from_hyp(c):
    if not c["ok"]:
        return {
            "label": c["label"],
            "ok": False,
            "n_a": c["n_a"],
            "n_b": c["n_b"],
        }
    return {
        "label": c["label"],
        "ok": True,
        "n_a": c["n_a"],
        "n_b": c["n_b"],
        "median_abs_over_sigma_a": _round(c["median_abs_over_sigma_a"]),
        "median_abs_over_sigma_b": _round(c["median_abs_over_sigma_b"]),
        "ratio_abs": _ratio(c["median_abs_over_sigma_a"], c["median_abs_over_sigma_b"]),
        "median_range_over_sigma_a": _round(c["median_range_over_sigma_a"]),
        "median_range_over_sigma_b": _round(c["median_range_over_sigma_b"]),
        "ratio_range": _ratio(
            c["median_range_over_sigma_a"], c["median_range_over_sigma_b"]
        ),
    }


def _range_public(row):
    out = {k: row[k] for k in off.RANGE_FIELDS}
    return _num_row(out, RANGE_FLOATS)


def _stop_public(row):
    out = {k: row[k] for k in off.STOP_FIELDS}
    return _num_row(out, STOP_FLOATS)


def hypothesis_public(hyp):
    return {
        "bar": hyp["bar"],
        "source": "ohlc",
        "n": hyp["n"],
        "skipped_no_next": hyp["skipped_no_next"],
        "skipped_close_mismatch": hyp["skipped_mismatch"],
        "skipped_bad": hyp["skipped_bad"],
        "states": {name: int(hyp["states"].get(name, 0)) for name in off.STATE_ORDER},
        "notes": list(hyp["notes"]),
        "notes_zh": notes_zh(hyp["notes"]),
        "range_rows": [_range_public(r) for r in hyp["range_rows"]],
        "stop_rows": [_stop_public(r) for r in hyp["stop_rows"]],
        "contrasts": [_contrast_from_hyp(c) for c in hyp["contrasts"]],
    }


def hypothesis_from_csv(bar, range_rows, stop_rows):
    """Fallback when OHLC was not kept next to the panel."""
    ranges = [r for r in (range_rows or []) if r.get("bar") == bar]
    stops = [r for r in (stop_rows or []) if r.get("bar") == bar]
    if not ranges and not stops:
        return {
            "bar": bar,
            "source": "missing",
            "n": 0,
            "skipped_no_next": None,
            "skipped_close_mismatch": None,
            "skipped_bad": None,
            "states": {name: 0 for name in off.STATE_ORDER},
            "notes": [],
            "notes_zh": ["没有 OHLC，也没有 hypothesis_range.csv，这张表空着。"],
            "range_rows": [],
            "stop_rows": [],
            "contrasts": [],
        }
    states = {name: 0 for name in off.STATE_ORDER}
    n = 0
    for row in ranges:
        if row["group_kind"] == "all" and row["group"] == "all":
            n = int(row["n"])
        if row["group_kind"] == "state" and row["group"] in states:
            states[row["group"]] = int(row["n"])
    return {
        "bar": bar,
        "source": "csv",
        "n": n,
        "skipped_no_next": None,
        "skipped_close_mismatch": None,
        "skipped_bad": None,
        "states": states,
        "notes": [],
        "notes_zh": ["这张表来自 data/backtest/ 里已经写好的 hypothesis CSV，没有重放 OHLC。"],
        "range_rows": ranges,
        "stop_rows": stops,
        "contrasts": contrasts_from_rows(ranges),
    }


def _parity_public(result):
    examples = []
    for item in result["mismatched"][:5]:
        bits = ", ".join(
            f"{field}: panel={got} history={hist}"
            for field, got, hist in item["diffs"]
        )
        examples.append(f"{off.iso(item['candle_ts'])} {bits}")
    return {
        "bar": result["bar"],
        "history_rows": result["history_rows"],
        "unique": result["unique"],
        "matched": result["matched"],
        "mismatched": len(result["mismatched"]),
        "missing": len(result["missing"]),
        "conflicts": len(result["conflicts"]),
        "c_ratio_legacy": result["c_ratio_legacy"],
        "examples": examples,
    }


def placeholder_summary():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema": 1,
        "generated_at": now,
        "placeholder": True,
        "placeholder_note": PLACEHOLDER_NOTE,
        "disclaimer": DISCLAIMER_ZH,
        "stop_note": STOP_NOTE_ZH,
        "disclaimer_en": off.DISCLAIMER,
        "stop_note_en": off.STOP_NOTE,
        "window_bars": neff.WINDOW,
        "smooth_width": neff.SMOOTH,
        "state_cuts": neff.state_cuts(),
        "instrument": None,
        "missing_bars": [],
        "bars": [],
        "parity": None,
        "hypothesis": None,
    }


def build_web(inst, bars, backtest_dir, ohlc_dir, history_path, allow_missing=False):
    """Return (summary, range_csv_rows, stop_csv_rows).

    ``summary`` is None when no requested panel exists.
    CSV rows are None when nothing was rebuilt or copied.
    """
    off.check_token(inst, "instrument")
    backtest_dir = Path(backtest_dir)
    ohlc_dir = Path(ohlc_dir)
    history_path = Path(history_path)

    present = []
    missing = []
    for bar in bars:
        path = backtest_dir / f"{inst}_{bar}_panel.csv"
        if path.exists():
            present.append((bar, path))
        else:
            missing.append(bar)
    if not present:
        return None, None, None

    history = None
    history_error = None
    if history_path.exists():
        history = off.read_history(history_path)
    else:
        history_error = f"missing {history_path}"

    csv_range = _read_typed_csv(backtest_dir / "hypothesis_range.csv", RANGE_FLOATS)
    csv_stop = _read_typed_csv(backtest_dir / "hypothesis_stop.csv", STOP_FLOATS)

    metas = []
    hyps_raw = []
    hyp_public = []
    parity_results = []
    sources = {"panels": [], "ohlc": [], "history": None, "hypothesis_csv": False}

    for bar, path in present:
        panel = off.read_panel(path)
        panel = [r for r in panel if r["instrument"] == inst and r["bar"] == bar]
        if not panel:
            raise ValueError(f"{path} has no rows for {inst} {bar}")
        sources["panels"].append(rel(path))
        ohlc_path = ohlc_dir / f"{inst}_{bar}.csv"
        candles = None
        if ohlc_path.exists():
            candles = off.read_ohlc(ohlc_path)
            sources["ohlc"].append(rel(ohlc_path))
        metas.append(bar_meta(panel, candles, bar))
        if candles is not None:
            hyp = off.build_hypothesis(panel, candles, bar)
            hyps_raw.append(hyp)
            hyp_public.append(hypothesis_public(hyp))
        else:
            public = hypothesis_from_csv(bar, csv_range, csv_stop)
            if public["source"] == "csv":
                sources["hypothesis_csv"] = True
            hyp_public.append(public)
        if history is not None:
            parity_results.append(off.check_parity(panel, history, inst, bar))

    range_rows = []
    stop_rows = []
    if hyps_raw:
        built_range, built_stop = off.hypothesis_csv_rows(hyps_raw)
        range_rows.extend(built_range)
        stop_rows.extend(built_stop)
    for public in hyp_public:
        if public.get("source") != "csv":
            continue
        range_rows.extend(
            {k: _csv_cell(r.get(k, "")) for k in off.RANGE_FIELDS}
            for r in public["range_rows"]
        )
        stop_rows.extend(
            {k: _csv_cell(r.get(k, "")) for k in off.STOP_FIELDS}
            for r in public["stop_rows"]
        )
    if not range_rows and not stop_rows:
        range_rows = stop_rows = None

    if history is None:
        parity = {
            "verdict": None,
            "history": rel(history_path),
            "error": history_error,
            "allow_missing": bool(allow_missing),
            "bars": [],
        }
    else:
        sources["history"] = rel(history_path)
        ok = off.parity_ok(parity_results, allow_missing=allow_missing)
        parity = {
            "verdict": "PASS" if ok else "FAIL",
            "history": rel(history_path),
            "error": None,
            "allow_missing": bool(allow_missing),
            "bars": [_parity_public(r) for r in parity_results],
        }

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = {
        "schema": 1,
        "generated_at": now,
        "placeholder": False,
        "placeholder_note": None,
        "disclaimer": DISCLAIMER_ZH,
        "stop_note": STOP_NOTE_ZH,
        "disclaimer_en": off.DISCLAIMER,
        "stop_note_en": off.STOP_NOTE,
        "window_bars": neff.WINDOW,
        "smooth_width": neff.SMOOTH,
        "state_cuts": neff.state_cuts(),
        "instrument": inst,
        "missing_bars": missing,
        "sources": sources,
        "bars": metas,
        "parity": parity,
        "hypothesis": {
            "thin_n": off.THIN_N,
            "bars": hyp_public,
        },
    }
    return summary, range_rows, stop_rows


def _csv_cell(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return f"{value:.6f}"
    return value


def write_web(summary, out_dir, range_rows, stop_rows):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "summary.json", summary)
    range_path = out / "hypothesis_range.csv"
    stop_path = out / "hypothesis_stop.csv"
    if range_rows is None or stop_rows is None:
        for path in (range_path, stop_path):
            if path.exists():
                path.unlink()
        return
    off.write_csv(range_path, off.RANGE_FIELDS, range_rows)
    off.write_csv(stop_path, off.STOP_FIELDS, stop_rows)


def _print_summary(summary, out_dir):
    if summary["placeholder"]:
        print(f"placeholder -> {out_dir / 'summary.json'}")
        return
    verdict = summary["parity"]["verdict"] if summary["parity"] else None
    print(
        f"{summary['instrument']} window={summary['window_bars']} "
        f"smooth={summary['smooth_width']} parity={verdict}"
    )
    for meta in summary["bars"]:
        print(
            f"  {meta['bar']}: signals={meta['signals']} candles={meta['candles']} "
            f"gaps={meta['gaps']} states={meta['states']} "
            f"{meta['signal_start']} .. {meta['signal_end']}"
        )
    if summary["missing_bars"]:
        print("missing panels: " + ", ".join(summary["missing_bars"]), file=sys.stderr)
    print(f"wrote {out_dir / 'summary.json'}")


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--bars", default="5m,1H,4H")
    p.add_argument("--backtest-dir", default="data/backtest")
    p.add_argument("--ohlc-dir", default="data/ohlc")
    p.add_argument("--history", default="data/neff_history.csv")
    p.add_argument("--out-dir", default="data/backtest_web")
    p.add_argument("--allow-missing", action="store_true")
    p.add_argument(
        "--placeholder",
        action="store_true",
        help="write an empty summary that says the sample is not here",
    )
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    out = Path(args.out_dir)
    try:
        if args.placeholder:
            summary = placeholder_summary()
            write_web(summary, out, None, None)
            _print_summary(summary, out)
            return 0
        bars = off.parse_bars(args.bars)
        summary, range_rows, stop_rows = build_web(
            args.inst, bars, args.backtest_dir, args.ohlc_dir,
            args.history, allow_missing=args.allow_missing,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if summary is None:
        print(
            f"error: no panels in {args.backtest_dir} for {args.inst} "
            f"({args.bars}). Refusing to invent a summary. "
            "Pass --placeholder to publish an explicit empty page.",
            file=sys.stderr,
        )
        return 1
    write_web(summary, out, range_rows, stop_rows)
    _print_summary(summary, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
