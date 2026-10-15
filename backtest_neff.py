#!/usr/bin/env python3
"""Rolling replay, history parity, and next-bar hypothesis tables.

Does not fetch. OHLC is read from data/ohlc/ (see backfill_ohlc.py).
The smoke command uses tests/fixtures and does not touch the network.

    python backtest_neff.py smoke
    python backtest_neff.py replay --bars 5m,1H,4H
    python backtest_neff.py parity --bars 5m,1H,4H
    python backtest_neff.py hypothesis --bars 5m,1H,4H
    python backtest_neff.py all --bars 5m,1H,4H

Replay calls neff.analyze on each contiguous WINDOW of closes and stamps
the row with the last bar of that window. WINDOW and SMOOTH are the same
env vars as the live script (default 60 and 5). Parity against
data/neff_history.csv needs step 1 and a download that starts at least
WINDOW bars before the oldest snapshot.
"""

import sys
from pathlib import Path

import neff
import neff_offline as off

ROOT = Path(__file__).resolve().parent
FIXTURE_OHLC = ROOT / "tests" / "fixtures" / "ohlc_tiny.csv"
FIXTURE_HISTORY = ROOT / "tests" / "fixtures" / "history_tiny.csv"
DEFAULT_BARS = "5m,1H,4H"


def _add_common(sp):
    sp.add_argument("--inst", default="BTC-USDT")
    sp.add_argument("--bars", default=DEFAULT_BARS)
    sp.add_argument("--ohlc-dir", default="data/ohlc")
    sp.add_argument("--out-dir", default="data/backtest")


def build_parser():
    import argparse

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("replay", help="write a panel CSV per bar")
    _add_common(sp)
    sp.add_argument("--step", type=int, default=1, help="keep every Nth eligible bar")

    sp = sub.add_parser("parity", help="compare a panel to neff_history.csv")
    _add_common(sp)
    sp.add_argument("--history", default="data/neff_history.csv")
    sp.add_argument(
        "--allow-missing",
        action="store_true",
        help="pass when every overlapping candle matches, even if some history timestamps are absent",
    )

    sp = sub.add_parser("hypothesis", help="next-bar range proxy and stop stub")
    _add_common(sp)

    sp = sub.add_parser("all", help="replay, then hypothesis, then parity")
    _add_common(sp)
    sp.add_argument("--step", type=int, default=1)
    sp.add_argument("--history", default="data/neff_history.csv")
    sp.add_argument("--allow-missing", action="store_true")

    sub.add_parser("smoke", help="replay the committed fixture; no network")
    return p


def _bars(args):
    return off.parse_bars(args.bars)


def _ohlc_path(args, bar):
    off.check_token(args.inst, "instrument")
    return Path(args.ohlc_dir) / f"{args.inst}_{bar}.csv"


def _panel_path(args, bar):
    off.check_token(args.inst, "instrument")
    return Path(args.out_dir) / f"{args.inst}_{bar}_panel.csv"


def cmd_replay(args):
    bars = _bars(args)
    print(f"window={neff.WINDOW} smooth={neff.SMOOTH} step={args.step}")
    if args.step != 1:
        print(
            "warning: step>1 will not land on every history candle_ts; "
            "parity wants step 1",
            file=sys.stderr,
        )
    for bar in bars:
        src = _ohlc_path(args, bar)
        candles = off.read_ohlc(src)
        if len(candles) < neff.WINDOW:
            print(
                f"error: {src} has {len(candles)} candles, need {neff.WINDOW}",
                file=sys.stderr,
            )
            return 1
        panel, info = off.rolling_panel(candles, args.inst, bar, step=args.step)
        dest = _panel_path(args, bar)
        if not panel:
            print(f"error: {bar}: no signal rows (gaps or flat windows?)", file=sys.stderr)
            return 1
        off.write_panel(dest, panel)
        states = {}
        for row in panel:
            states[row["state"]] = states.get(row["state"], 0) + 1
        print(
            f"{bar}: candles={len(candles)} signals={len(panel)} "
            f"skipped_gap={info['skipped_gap']} skipped_flat={info['skipped_flat']} "
            f"states={states} "
            f"{off.iso(panel[0]['candle_ts'])} .. {off.iso(panel[-1]['candle_ts'])} "
            f"-> {dest}"
        )
    return 0


def cmd_parity(args):
    bars = _bars(args)
    history = off.read_history(args.history)
    results = []
    missing_files = []
    for bar in bars:
        path = _panel_path(args, bar)
        if not path.exists():
            missing_files.append(str(path))
            continue
        panel = off.read_panel(path)
        results.append(off.check_parity(panel, history, args.inst, bar))
    text = off.format_parity(
        results, missing_files, args.history, allow_missing=args.allow_missing,
    )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "parity.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"wrote {out / 'parity.txt'}")
    ok = off.parity_ok(results, allow_missing=args.allow_missing) and not missing_files
    return 0 if ok else 1


def cmd_hypothesis(args):
    bars = _bars(args)
    hyps = []
    for bar in bars:
        candles = off.read_ohlc(_ohlc_path(args, bar))
        panel = off.read_panel(_panel_path(args, bar))
        hyps.append(off.build_hypothesis(panel, candles, bar))
    text = off.format_hypothesis(hyps)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "hypothesis.txt").write_text(text, encoding="utf-8")
    range_rows, stop_rows = off.hypothesis_csv_rows(hyps)
    off.write_csv(out / "hypothesis_range.csv", off.RANGE_FIELDS, range_rows)
    off.write_csv(out / "hypothesis_stop.csv", off.STOP_FIELDS, stop_rows)
    print(text)
    print(
        f"wrote {out / 'hypothesis.txt'}, "
        f"{out / 'hypothesis_range.csv'}, {out / 'hypothesis_stop.csv'}"
    )
    if not hyps or all(h["n"] == 0 for h in hyps):
        print("error: hypothesis produced no samples", file=sys.stderr)
        return 1
    return 0


def cmd_all(args):
    rc = cmd_replay(args)
    if rc != 0:
        return rc
    rc_h = cmd_hypothesis(args)
    rc_p = cmd_parity(args)
    return 0 if rc_h == 0 and rc_p == 0 else 1


def cmd_smoke():
    """Replay the committed fixture and parity-check the committed mini history."""
    if not FIXTURE_OHLC.exists() or not FIXTURE_HISTORY.exists():
        print(f"error: missing fixture under {FIXTURE_OHLC.parent}", file=sys.stderr)
        return 1
    candles = off.read_ohlc(FIXTURE_OHLC)
    panel, info = off.rolling_panel(candles, "BTC-USDT", "5m", step=1)
    print(
        f"fixture candles={len(candles)} signals={len(panel)} "
        f"skipped_gap={info['skipped_gap']} skipped_flat={info['skipped_flat']} "
        f"window={neff.WINDOW}"
    )
    if info["skipped_gap"] or info["skipped_flat"] or not panel:
        print("error: fixture should be one contiguous non-flat series", file=sys.stderr)
        return 1
    history = off.read_history(FIXTURE_HISTORY)
    result = off.check_parity(panel, history, "BTC-USDT", "5m")
    text = off.format_parity([result], [], FIXTURE_HISTORY)
    print(text)
    hyp = off.build_hypothesis(panel, candles, "5m")
    print(off.format_hypothesis([hyp]))
    if not off.parity_ok([result]) or hyp["n"] < 1:
        print("error: smoke failed", file=sys.stderr)
        return 1
    print(f"smoke ok: signals={len(panel)} hypothesis_samples={hyp['n']}")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "replay":
            return cmd_replay(args)
        if args.cmd == "parity":
            return cmd_parity(args)
        if args.cmd == "hypothesis":
            return cmd_hypothesis(args)
        if args.cmd == "all":
            return cmd_all(args)
        if args.cmd == "smoke":
            return cmd_smoke()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"error: unknown command {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
