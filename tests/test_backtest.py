#!/usr/bin/env python3
"""Stdlib checks for the offline replay. No network.

    python -m unittest tests.test_backtest
"""

import io
import json
import math
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neff  # noqa: E402
import neff_offline as off  # noqa: E402
import backfill_ohlc  # noqa: E402
import backtest_neff  # noqa: E402
import export_backtest_web as web  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures"
HIST_FIELDS = [
    "generated_at", "instrument", "bar", "candle_ts", "last",
    "kappa", "n_eff", "n_turn", "consistency", "c_null", "c_ratio",
    "efficiency_ratio", "state",
]


def build_tiny_candles():
    """90 contiguous 5m bars. Deterministic; the committed fixture matches this."""
    rnd = random.Random(20260919)
    step = off.BAR_MS["5m"]
    ts0 = 1_789_000_000_000
    ts0 = ts0 - (ts0 % step)
    price = 80000.0
    candles = []
    for i in range(90):
        close = price + 30.0 * math.sin(i / 8.0) + rnd.gauss(0, 25)
        open_ = price
        wick = abs(rnd.gauss(0, 12))
        candles.append({
            "ts": ts0 + i * step,
            "open": open_,
            "high": max(open_, close) + wick,
            "low": min(open_, close) - wick,
            "close": close,
            "vol": 1.0,
        })
        price = close
    return candles


def _hist_row(panel_row, generated_at):
    row = {"generated_at": generated_at}
    for key in HIST_FIELDS[1:]:
        row[key] = panel_row[key]
    return row


def regen_fixtures(dest=None):
    dest = Path(dest or FIXTURE_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    candles = build_tiny_candles()
    off.write_ohlc(dest / "ohlc_tiny.csv", candles)
    panel, _ = off.rolling_panel(candles, "BTC-USDT", "5m")
    picks = [panel[0], panel[len(panel) // 2], panel[-1]]
    rows = [_hist_row(p, "2026-01-01T00:00:00+00:00") for p in picks]
    rows.append(_hist_row(picks[1], "2020-01-01T00:00:00+00:00"))
    off.write_csv(dest / "history_tiny.csv", HIST_FIELDS, rows)


def make_candles(closes, bar="5m", ts0=1_700_000_000_000):
    step = off.BAR_MS[bar]
    out = []
    for i, close in enumerate(closes):
        close = float(close)
        out.append({
            "ts": ts0 + i * step,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "vol": 1.0,
        })
    return out


def random_closes(n, seed=1, scale=1.0):
    rnd = random.Random(seed)
    price = 100.0
    out = []
    for _ in range(n):
        price += rnd.gauss(0, scale)
        out.append(price)
    return out


class ReplayTests(unittest.TestCase):
    def test_signal_is_the_window_end_and_matches_analyze(self):
        closes = random_closes(neff.WINDOW + 15, seed=3)
        candles = make_candles(closes)
        panel, info = off.rolling_panel(candles, "BTC-USDT", "5m")
        self.assertEqual(info["skipped_gap"], 0)
        self.assertEqual(info["skipped_flat"], 0)
        self.assertEqual(len(panel), len(candles) - neff.WINDOW + 1)
        self.assertEqual(panel[0]["candle_ts"], candles[neff.WINDOW - 1]["ts"])
        first = neff.analyze(closes[:neff.WINDOW])
        last = neff.analyze(closes)
        self.assertEqual(panel[0]["n_eff"], first["n_eff"])
        self.assertEqual(panel[0]["state"], first["state"])
        self.assertEqual(panel[0]["kappa"], first["kappa"])
        self.assertEqual(panel[-1]["n_eff"], last["n_eff"])
        self.assertEqual(panel[-1]["last"], candles[-1]["close"])
        self.assertNotEqual(panel[0]["candle_ts"], candles[-1]["ts"])

    def test_future_bar_does_not_change_earlier_rows(self):
        candles = make_candles(random_closes(neff.WINDOW + 12, seed=4))
        panel, _ = off.rolling_panel(candles, "BTC-USDT", "5m")
        later = [dict(c) for c in candles]
        bumped = dict(later[-1])
        bumped["close"] = bumped["close"] + 10000
        bumped["high"] = bumped["close"] + 1
        later[-1] = bumped
        panel2, _ = off.rolling_panel(later, "BTC-USDT", "5m")
        self.assertEqual(panel[:-1], panel2[:-1])
        self.assertNotEqual(panel[-1]["last"], panel2[-1]["last"])

    def test_gap_drops_windows_that_cross_it(self):
        window = neff.WINDOW
        left = make_candles(random_closes(window + 5, seed=5), ts0=0)
        jump = left[-1]["ts"] + off.BAR_MS["5m"] * 5
        right = make_candles(random_closes(window + 5, seed=6), ts0=jump)
        panel, info = off.rolling_panel(left + right, "BTC-USDT", "5m")
        self.assertGreater(info["skipped_gap"], 0)
        emitted = {r["candle_ts"] for r in panel}
        self.assertIn(left[-1]["ts"], emitted)
        self.assertNotIn(right[0]["ts"], emitted)
        self.assertNotIn(right[window - 2]["ts"], emitted)
        self.assertIn(right[window - 1]["ts"], emitted)

    def test_flat_window_is_skipped(self):
        closes = [50.0] * neff.WINDOW + [50.0 + i * 0.4 for i in range(neff.WINDOW)]
        panel, info = off.rolling_panel(make_candles(closes), "BTC-USDT", "5m")
        self.assertGreater(info["skipped_flat"], 0)
        self.assertGreater(len(panel), 0)

    def test_step_keeps_every_nth_index(self):
        candles = make_candles(random_closes(neff.WINDOW + 20, seed=8))
        full, _ = off.rolling_panel(candles, "BTC-USDT", "5m", step=1)
        thin, _ = off.rolling_panel(candles, "BTC-USDT", "5m", step=5)
        self.assertEqual([r["candle_ts"] for r in thin], [r["candle_ts"] for r in full][::5])
        self.assertLess(len(thin), len(full))


class PaginationTests(unittest.TestCase):
    def _row(self, ts, confirm="1", close="1"):
        return [str(ts), "1", "2", "0", close, "9", "", "", confirm]

    def test_pages_backward_drops_unconfirmed_and_stops_at_start(self):
        limit = 2
        calls = []
        sleeps = {"n": 0}

        def fetch(path, params):
            calls.append(params.get("after"))
            cursor = int(params["after"]) if params.get("after") else 10_000
            return [self._row(cursor - (i + 1) * 1000) for i in range(limit)]

        def sleep():
            sleeps["n"] += 1

        rows, info = off.collect_candles(
            fetch, "BTC-USDT", "5m", start_ms=5000, end_ms=10**15,
            limit=limit, sleep=sleep, max_pages=10,
        )
        # pages of (9000,8000), (7000,6000), (5000,4000); 4000 is before start
        self.assertEqual(calls, [None, "8000", "6000"])
        self.assertEqual(sleeps["n"], 2)
        self.assertTrue(info["reached_start"])
        self.assertEqual(set(rows), {9000, 8000, 7000, 6000, 5000})

    def test_unconfirmed_newest_is_dropped(self):
        def fetch(path, params):
            return [
                self._row(3000, confirm="0", close="9"),
                self._row(2000, close="2"),
                self._row(1000, close="1"),
            ]

        rows, info = off.collect_candles(
            fetch, "BTC-USDT", "5m", start_ms=0, end_ms=10**15, limit=100,
        )
        self.assertEqual(set(rows), {2000, 1000})
        self.assertEqual(rows[2000]["close"], "2")
        self.assertFalse(info["reached_start"])
        self.assertTrue(info["exhausted"])

    def test_stalled_cursor_raises(self):
        def fetch(path, params):
            return [self._row(2000), self._row(1000)]

        with self.assertRaises(RuntimeError):
            off.collect_candles(
                fetch, "BTC-USDT", "5m", start_ms=0, end_ms=10**15, limit=2,
            )

    def test_overlay_keeps_candles_endpoint_on_conflict(self):
        base = {1: {"ts": 1, "close": "10"}}
        overlay = {1: {"ts": 1, "close": "11"}, 2: {"ts": 2, "close": "12"}}
        n = off.merge_candles(base, overlay)
        self.assertEqual(n, 1)
        self.assertEqual(base[1]["close"], "11")
        self.assertEqual(base[2]["close"], "12")


class ParityTests(unittest.TestCase):
    def _panel(self):
        candles = make_candles(random_closes(neff.WINDOW + 2, seed=9))
        panel, _ = off.rolling_panel(candles, "BTC-USDT", "5m")
        return panel

    def test_tolerances_reject_one_stored_step(self):
        self.assertTrue(off.values_close("n_eff", 4.70, "4.70"))
        self.assertFalse(off.values_close("n_eff", 4.70, "4.71"))
        self.assertTrue(off.values_close("state", "sharp", " sharp "))
        self.assertFalse(off.values_close("state", "sharp", "normal"))

    def test_match_mismatch_missing_and_conflict(self):
        panel = self._panel()
        hist = [_hist_row(panel[0], "2026-01-02T00:00:00+00:00")]
        good = off.check_parity(panel, hist, "BTC-USDT", "5m")
        self.assertEqual(good["matched"], 1)
        self.assertEqual(good["mismatched"], [])
        self.assertTrue(off.parity_ok([good]))

        wrong = [_hist_row(panel[0], "2026-01-02T00:00:00+00:00")]
        wrong[0]["n_eff"] = "9.99"
        bad = off.check_parity(panel, wrong, "BTC-USDT", "5m")
        self.assertEqual(bad["matched"], 0)
        self.assertEqual(len(bad["mismatched"]), 1)
        self.assertFalse(off.parity_ok([bad]))

        missing = [
            _hist_row(panel[0], "2026-01-02T00:00:00+00:00"),
            _hist_row(panel[0], "2026-01-02T00:00:00+00:00"),
        ]
        missing[1]["candle_ts"] = 123
        missing[1]["generated_at"] = "2026-01-03T00:00:00+00:00"
        got = off.check_parity(panel, missing, "BTC-USDT", "5m")
        self.assertEqual(got["matched"], 1)
        self.assertEqual(got["missing"], [123])
        self.assertFalse(off.parity_ok([got]))
        self.assertTrue(off.parity_ok([got], allow_missing=True))

        older = _hist_row(panel[0], "2020-01-01T00:00:00+00:00")
        older["state"] = "circular" if panel[0]["state"] != "circular" else "sharp"
        latest = _hist_row(panel[0], "2026-01-02T00:00:00+00:00")
        conflicted = off.check_parity(panel, [older, latest], "BTC-USDT", "5m")
        self.assertEqual(conflicted["conflicts"], [panel[0]["candle_ts"]])
        self.assertEqual(conflicted["matched"], 1)

    def test_migrated_c_ratio_is_accepted_when_geometry_matches(self):
        panel = [dict(self._panel()[0])]
        hist = _hist_row(panel[0], "2026-01-02T00:00:00+00:00")
        legacy = off.legacy_c_ratio(hist["consistency"], hist["c_null"])
        hist["c_ratio"] = legacy
        panel[0]["c_ratio"] = round(legacy + 0.002, 3)
        got = off.check_parity(panel, [hist], "BTC-USDT", "5m")
        self.assertEqual(got["mismatched"], [])
        self.assertEqual(got["matched"], 1)
        self.assertEqual(got["c_ratio_legacy"], 1)

        hist["c_ratio"] = "9.99"
        bad = off.check_parity(panel, [hist], "BTC-USDT", "5m")
        self.assertEqual(len(bad["mismatched"]), 1)
        self.assertEqual(bad["c_ratio_legacy"], 0)


class HypothesisTests(unittest.TestCase):
    def test_known_next_bar_geometry(self):
        ts = 1_000_000_000_000
        step = off.BAR_MS["5m"]
        candles = [
            {"ts": ts, "open": 100, "high": 101, "low": 99, "close": 100, "vol": 1},
            {"ts": ts + step, "open": 100, "high": 111, "low": 99, "close": 110, "vol": 1},
        ]
        panel = [{
            "candle_ts": ts,
            "instrument": "BTC-USDT",
            "bar": "5m",
            "last": 100.0,
            "kappa": 1.2,
            "n_eff": 4.5,
            "n_turn": 8.0,
            "consistency": 0.01,
            "c_null": 0.02,
            "c_ratio": 0.5,
            "efficiency_ratio": 0.1,
            "sigma_per_bar": 10.0,
            "state": "sharp",
        }]
        hyp = off.build_hypothesis(panel, candles, "5m")
        self.assertEqual(hyp["n"], 1)
        sharp = next(r for r in hyp["range_rows"] if r["group_kind"] == "state")
        self.assertEqual(sharp["group"], "sharp")
        self.assertAlmostEqual(sharp["median_abs_over_sigma"], 1.0)
        self.assertAlmostEqual(sharp["median_range_over_sigma"], 1.2)
        stops = {(r["group"], r["k"]): r for r in hyp["stop_rows"] if r["group_kind"] == "state"}
        self.assertEqual(stops[("sharp", 0.5)]["touch_rate"], 1)
        self.assertEqual(stops[("sharp", 0.5)]["wick_rate"], 0)
        self.assertEqual(stops[("sharp", 0.5)]["close_through_rate"], 1)
        self.assertEqual(stops[("sharp", 1.0)]["wick_rate"], 1)
        self.assertEqual(stops[("sharp", 1.0)]["close_through_rate"], 0)
        self.assertEqual(stops[("sharp", 1.5)]["touch_rate"], 0)
        text = off.format_hypothesis([hyp])
        self.assertIn("Descriptive only", text)
        self.assertIn("not a directional-alpha", text)

    def test_does_not_pair_across_a_gap_or_a_close_mismatch(self):
        ts = 1_000_000_000_000
        step = off.BAR_MS["5m"]
        candles = [
            {"ts": ts, "open": 1, "high": 2, "low": 0, "close": 1, "vol": 1},
            {"ts": ts + 2 * step, "open": 1, "high": 2, "low": 0, "close": 1, "vol": 1},
        ]
        panel = [{
            "candle_ts": ts, "instrument": "BTC-USDT", "bar": "5m", "last": 1.0,
            "kappa": 1, "n_eff": 4, "n_turn": 8, "consistency": 0, "c_null": 0.01,
            "c_ratio": 0, "efficiency_ratio": 0, "sigma_per_bar": 1, "state": "sharp",
        }]
        hyp = off.build_hypothesis(panel, candles, "5m")
        self.assertEqual(hyp["n"], 0)
        self.assertEqual(hyp["skipped_no_next"], 1)

        candles[1]["ts"] = ts + step
        panel[0]["last"] = 50.0
        hyp = off.build_hypothesis(panel, candles, "5m")
        self.assertEqual(hyp["skipped_mismatch"], 1)
        self.assertEqual(hyp["n"], 0)


class PlanTests(unittest.TestCase):
    def test_days_pad_warmup_and_since_conflicts(self):
        now = 1_792_047_600_000
        items = backfill_ohlc.build_plan(
            "BTC-USDT", ["4H"], 10, None, None, 300, Path("data/ohlc"), now, 60,
        )
        self.assertEqual(items[0]["until"], now)
        self.assertEqual(items[0]["signal_start"], now - 10 * 86_400_000)
        self.assertEqual(
            items[0]["fetch_start"],
            items[0]["signal_start"] - 59 * off.BAR_MS["4H"],
        )

    def test_omitted_days_is_one_year_below_1h_and_two_years_slower(self):
        now = 1_792_047_600_000
        items = backfill_ohlc.build_plan(
            "BTC-USDT", ["5m", "1H", "4H"], None, None, None, 300,
            Path("data/ohlc"), now, 60,
        )
        by = {item["bar"]: item for item in items}
        self.assertEqual(by["5m"]["days"], 365)
        self.assertEqual(by["1H"]["days"], 730)
        self.assertEqual(by["4H"]["days"], 730)
        self.assertEqual(by["5m"]["signal_start"], now - 365 * 86_400_000)
        self.assertEqual(by["4H"]["signal_start"], now - 730 * 86_400_000)
        with self.assertRaises(ValueError):
            backfill_ohlc.build_plan(
                "BTC-USDT", ["5m"], 10, "2026-01-01", None, 300, Path("data/ohlc"), now, 60,
            )
        with self.assertRaises(ValueError):
            off.parse_bars("2m")

    def test_incremental_resume_point(self):
        step = off.BAR_MS["5m"]
        t0 = 1_792_000_000_000
        rows = [
            {"ts": t0 + i * step, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 0.0}
            for i in range(200)
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "BTC-USDT_5m.csv"
            until = t0 + 300 * step
            # No file: full download.
            self.assertEqual(backfill_ohlc.resume_point(path, t0, until, step, 60), ({}, t0))
            off.write_ohlc(path, rows)
            # Span rolled forward by 10 bars: older rows drop, tail is refetched.
            kept, start = backfill_ohlc.resume_point(path, t0 + 10 * step, until, step, 60)
            self.assertEqual(min(kept), t0 + 10 * step)
            self.assertEqual(len(kept), 190)
            self.assertEqual(start, t0 + 199 * step - 60 * step)
            # File starts after the requested start: not enough history, full download.
            self.assertEqual(
                backfill_ohlc.resume_point(path, t0 - 5 * step, until, step, 60),
                ({}, t0 - 5 * step),
            )

    def test_dry_run_does_not_fetch(self):
        calls = []
        orig = backfill_ohlc.fetch_page
        backfill_ohlc.fetch_page = lambda *a, **k: calls.append(1)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = backfill_ohlc.main(["--dry-run", "--days", "3", "--bars", "5m,4H"])
        finally:
            backfill_ohlc.fetch_page = orig
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertIn("dry-run: no request sent", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = backfill_ohlc.main(["--dry-run", "--days", "2", "--since", "2026-01-01"])
        self.assertEqual(rc, 2)
        self.assertIn("only one of", err.getvalue())


class FixtureTests(unittest.TestCase):
    def setUp(self):
        if neff.WINDOW != 60 or neff.SMOOTH != 5:
            self.skipTest("committed fixture assumes WINDOW=60 SMOOTH=5")

    def test_fixture_matches_generator_and_history(self):
        built = build_tiny_candles()
        loaded = off.read_ohlc(FIXTURE_DIR / "ohlc_tiny.csv")
        self.assertEqual(len(built), len(loaded))
        for a, b in zip(built, loaded):
            self.assertEqual(a["ts"], b["ts"])
            self.assertEqual(float(str(a["close"])), b["close"])
            self.assertEqual(float(str(a["high"])), b["high"])
            self.assertEqual(float(str(a["low"])), b["low"])
        panel, info = off.rolling_panel(loaded, "BTC-USDT", "5m")
        self.assertEqual(info["skipped_gap"], 0)
        self.assertEqual(len(panel), len(loaded) - neff.WINDOW + 1)
        metrics = neff.analyze([c["close"] for c in loaded])
        self.assertEqual(panel[-1]["n_eff"], metrics["n_eff"])
        self.assertEqual(panel[-1]["state"], metrics["state"])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel.csv"
            off.write_panel(path, panel)
            roundtrip = off.read_panel(path)
        result = off.check_parity(
            roundtrip, off.read_history(FIXTURE_DIR / "history_tiny.csv"), "BTC-USDT", "5m",
        )
        self.assertEqual(result["mismatched"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["matched"], 3)
        self.assertGreater(result["history_rows"], result["unique"])

        hyp = off.build_hypothesis(panel, loaded, "5m")
        self.assertEqual(hyp["n"], len(panel) - 1)
        self.assertGreater(hyp["n"], 0)

    def test_cli_smoke_and_fixture_commands(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backtest_neff.main(["smoke"]), 0)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "backfill_ohlc.py"),
             "--dry-run", "--days", "2", "--bars", "5m,1H"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry-run: no request sent", proc.stdout)
        self.assertIn("www.okx.com", proc.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ohlc = tmp / "ohlc"
            out = tmp / "out"
            ohlc.mkdir()
            shutil.copy(FIXTURE_DIR / "ohlc_tiny.csv", ohlc / "BTC-USDT_5m.csv")
            base = ["--inst", "BTC-USDT", "--bars", "5m",
                    "--ohlc-dir", str(ohlc), "--out-dir", str(out)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(backtest_neff.main(["replay", *base]), 0)
                self.assertEqual(backtest_neff.main(["hypothesis", *base]), 0)
            text = (out / "hypothesis.txt").read_text(encoding="utf-8")
            self.assertIn("Descriptive only", text)
            self.assertTrue((out / "hypothesis_range.csv").exists())
            self.assertTrue((out / "hypothesis_stop.csv").exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(backtest_neff.main([
                    "parity", *base, "--history", str(FIXTURE_DIR / "history_tiny.csv"),
                ]), 0)
            self.assertIn("PASS", (out / "parity.txt").read_text(encoding="utf-8"))


class WebExportTests(unittest.TestCase):
    def setUp(self):
        if neff.WINDOW != 60 or neff.SMOOTH != 5:
            self.skipTest("committed fixture assumes WINDOW=60 SMOOTH=5")

    def _layout(self, tmp):
        tmp = Path(tmp)
        ohlc = tmp / "ohlc"
        panel = tmp / "backtest"
        out = tmp / "web"
        ohlc.mkdir()
        shutil.copy(FIXTURE_DIR / "ohlc_tiny.csv", ohlc / "BTC-USDT_5m.csv")
        base = ["--inst", "BTC-USDT", "--bars", "5m",
                "--ohlc-dir", str(ohlc), "--out-dir", str(panel)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(backtest_neff.main(["all", *base,
                "--history", str(FIXTURE_DIR / "history_tiny.csv")]), 0)
        return ohlc, panel, out

    def test_export_fixture_summary_is_small_and_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ohlc, panel, out = self._layout(tmp)
            with redirect_stdout(io.StringIO()):
                rc = web.main([
                    "--inst", "BTC-USDT", "--bars", "5m",
                    "--ohlc-dir", str(ohlc), "--backtest-dir", str(panel),
                    "--history", str(FIXTURE_DIR / "history_tiny.csv"),
                    "--out-dir", str(out),
                ])
            self.assertEqual(rc, 0)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["placeholder"])
            self.assertIn("不回答该不该买", summary["disclaimer"])
            self.assertEqual(summary["disclaimer_en"], off.DISCLAIMER)
            self.assertEqual(summary["instrument"], "BTC-USDT")
            self.assertEqual(summary["window_bars"], 60)
            self.assertEqual(summary["parity"]["verdict"], "PASS")
            self.assertEqual(summary["parity"]["bars"][0]["matched"], 3)
            meta = summary["bars"][0]
            self.assertEqual(meta["bar"], "5m")
            self.assertEqual(meta["candles"], 90)
            self.assertEqual(meta["gaps"], 0)
            self.assertEqual(
                sum(meta["states"][name] for name in off.STATE_ORDER),
                meta["signals"],
            )
            self.assertGreater(meta["signals"], 0)
            hyp = summary["hypothesis"]["bars"][0]
            self.assertEqual(hyp["source"], "ohlc")
            self.assertEqual(hyp["n"], meta["signals"] - 1)
            self.assertTrue(hyp["range_rows"])
            self.assertTrue(hyp["stop_rows"])
            self.assertTrue((out / "hypothesis_range.csv").exists())
            self.assertTrue((out / "hypothesis_stop.csv").exists())
            self.assertLess((out / "summary.json").stat().st_size, 200_000)
            names = sorted(p.name for p in out.iterdir())
            self.assertEqual(names, [
                "hypothesis_range.csv", "hypothesis_stop.csv", "summary.json",
            ])

    def test_csv_fallback_when_ohlc_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            _ohlc, panel, out = self._layout(tmp)
            empty = Path(tmp) / "no-ohlc"
            empty.mkdir()
            with redirect_stdout(io.StringIO()):
                rc = web.main([
                    "--inst", "BTC-USDT", "--bars", "5m",
                    "--ohlc-dir", str(empty), "--backtest-dir", str(panel),
                    "--history", str(FIXTURE_DIR / "history_tiny.csv"),
                    "--out-dir", str(out),
                ])
            self.assertEqual(rc, 0)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            meta = summary["bars"][0]
            self.assertIsNone(meta["candles"])
            self.assertFalse(meta["ohlc_present"])
            hyp = summary["hypothesis"]["bars"][0]
            self.assertEqual(hyp["source"], "csv")
            self.assertGreater(hyp["n"], 0)
            sharp = next(c for c in hyp["contrasts"] if c["label"] == "sharp vs normal")
            self.assertIn("ok", sharp)
            self.assertTrue((out / "hypothesis_range.csv").exists())

    def test_placeholder_writes_no_ratios_and_clears_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "web"
            out.mkdir()
            (out / "hypothesis_range.csv").write_text("stale\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(web.main(["--placeholder", "--out-dir", str(out)]), 0)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["placeholder"])
            self.assertEqual(summary["bars"], [])
            self.assertIsNone(summary["hypothesis"])
            self.assertIsNone(summary["parity"])
            self.assertIn("不会补造数字", summary["placeholder_note"])
            self.assertFalse((out / "hypothesis_range.csv").exists())
            self.assertFalse((out / "hypothesis_stop.csv").exists())

    def test_missing_panels_do_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "web"
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = web.main([
                    "--backtest-dir", str(Path(tmp) / "missing"),
                    "--out-dir", str(out),
                ])
            self.assertEqual(rc, 1)
            self.assertIn("Refusing to invent", err.getvalue())
            self.assertFalse(out.exists())

    def test_pages_link_each_other_and_gitignore_keeps_the_summary(self):
        page = (ROOT / "backtest.html").read_text(encoding="utf-8")
        live = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("data/backtest_web/summary.json", page)
        self.assertIn("不回答该不该买", page)
        self.assertIn('href="index.html"', page)
        self.assertIn('href="backtest.html"', live)
        self.assertIn("data/backtest_web/", (ROOT / "BACKTEST.md").read_text(encoding="utf-8"))
        for path in ("data/ohlc/BTC-USDT_5m.csv", "data/backtest/BTC-USDT_5m_panel.csv"):
            proc = subprocess.run(
                ["git", "check-ignore", "-q", "--", path],
                cwd=ROOT, check=False,
            )
            self.assertEqual(proc.returncode, 0, path)
        kept = subprocess.run(
            ["git", "check-ignore", "-q", "--", "data/backtest_web/summary.json"],
            cwd=ROOT, check=False,
        )
        self.assertEqual(kept.returncode, 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--regen-fixtures":
        regen_fixtures()
        print(f"wrote {FIXTURE_DIR}")
    else:
        unittest.main()
