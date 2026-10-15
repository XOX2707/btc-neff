#!/usr/bin/env python3
"""Backfill confirmed OHLCV from OKX into data/ohlc/ (gitignored).

Uses /api/v5/market/history-candles, paged backward with `after`, then
overlays the recent tail from /api/v5/market/candles — the same endpoint
neff.py uses for the live reading. Hosts come from OKX_BASE, in order.

    python backfill_ohlc.py --dry-run
    python backfill_ohlc.py --inst BTC-USDT --bars 5m,1H,4H

With no --days and no --since, bars finer than 1H cover 365 days and 1H
and slower cover 730. 5m at 365 days is about 350 pages (a couple of
minutes at the default delay). OKX still serves several years of
BTC-USDT history, so that default is a time choice, not an API cap.
Pass --days to force one length on every bar.

--incremental keeps an existing CSV and only re-downloads its tail (the
last WINDOW bars, to pick up any late revision) plus everything newer.
Rows older than the requested start are dropped, so the span keeps
rolling forward. A missing or shorter file falls back to a full download.
The scheduled backtest workflow uses this with a cached data/ohlc/.

Network only happens without --dry-run. A 403 (common from US IPs) is
reported with the hosts that were tried; an empty file is not written.

The requested window is padded by WINDOW-1 bars so the first requested
day can produce a signal. Only confirm == "1" candles are stored.
"""

import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import neff
import neff_offline as off

HISTORY_PATH = "/api/v5/market/history-candles"
CANDLES_PATH = "/api/v5/market/candles"
# Calendar days of signals when neither --days nor --since is set.
# Finer than 1H: one year. 5m is ~350 pages at limit 300, a couple of
# minutes with REQ_DELAY=0.15. 1H and slower: two years, which is only
# a few dozen pages. OKX history goes back further; raise --days to pull it.
DEFAULT_DAYS = 365
DEFAULT_DAYS_SLOW = 730
DEFAULT_LIMIT = 300
RECENT_CAP = 1440  # /market/candles keeps about this many bars


def parse_ms(text):
    """Epoch seconds, epoch milliseconds, YYYY-MM-DD, or ISO-8601."""
    s = text.strip()
    if s.isdigit():
        n = int(s)
        return n if n >= 10**12 else n * 1000
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if len(s) == 10:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_page(path, params):
    try:
        return neff.okx_get(path, params)
    except RuntimeError as e:
        raise RuntimeError(
            "OKX request failed. Public market data often returns 403 from "
            f"US IPs. Hosts in order: {', '.join(neff.BASES)}. {e}"
        ) from e


def pace():
    if neff.REQ_DELAY > 0:
        time.sleep(neff.REQ_DELAY)


def default_days(bar):
    """Per-bar default used when the caller does not pass --days or --since."""
    if off.bar_ms(bar) >= off.BAR_MS["1H"]:
        return DEFAULT_DAYS_SLOW
    return DEFAULT_DAYS


def build_plan(inst, bars, days, since, until, limit, out_dir, now_ms, window):
    if since is not None and days is not None:
        raise ValueError("pass only one of --since and --days")
    if days is not None and days < 1:
        raise ValueError("--days must be >= 1")
    if limit < 1 or limit > 300:
        raise ValueError("--limit must be between 1 and 300")
    off.check_token(inst, "instrument")
    until_ms = parse_ms(until) if until else now_ms
    if until_ms > now_ms:
        until_ms = now_ms
    items = []
    for bar in bars:
        step = off.bar_ms(bar)
        bar_days = days if days is not None else (None if since is not None else default_days(bar))
        if since is not None:
            signal_start = parse_ms(since)
        else:
            signal_start = until_ms - int(bar_days) * 86_400_000
        fetch_start = signal_start - (window - 1) * step
        if fetch_start >= until_ms:
            raise ValueError(f"{bar}: empty range")
        est_bars = max(1, math.ceil((until_ms - fetch_start) / step))
        items.append({
            "inst": inst,
            "bar": bar,
            "fetch_start": fetch_start,
            "signal_start": signal_start,
            "until": until_ms,
            "est_bars": est_bars,
            "est_pages": max(1, math.ceil(est_bars / limit)),
            "path": str(Path(out_dir) / f"{inst}_{bar}.csv"),
            "limit": limit,
            "window": window,
            "days": bar_days,
        })
    return items


def format_plan(items):
    lines = [
        f"hosts: {', '.join(neff.BASES)}",
        f"window={items[0]['window']} smooth={neff.SMOOTH} limit={items[0]['limit']}",
    ]
    for item in items:
        span = f"{item['days']} days" if item["days"] else "--since"
        lines.append(
            f"  {item['inst']} {item['bar']}: fetch {off.iso(item['fetch_start'])}"
            f" .. {off.iso(item['until'])}"
            f" (signals from {off.iso(item['signal_start'])},"
            f" {span}, warmup {item['window'] - 1} bars)"
            f" est_bars={item['est_bars']} est_pages={item['est_pages']}"
            f" -> {item['path']}"
        )
    return "\n".join(lines)


def _on_page(inst, bar):
    def inner(n, page):
        if not page:
            print(f"  {inst} {bar} page {n}: empty", flush=True)
            return
        if n == 1 or n % 5 == 0:
            oldest = min(int(r[0]) for r in page)
            print(
                f"  {inst} {bar} page {n} oldest {off.iso(oldest)}",
                flush=True,
            )
    return inner


def resume_point(path, fetch_start, until, step, window):
    """(rows to keep, download start) for --incremental.

    Returns ({}, fetch_start) — a full download — when the file is missing,
    unreadable, or does not reach back to fetch_start.
    """
    path = Path(path)
    if not path.exists():
        return {}, fetch_start
    try:
        rows = off.read_ohlc(path)
    except ValueError:
        return {}, fetch_start
    if rows[0]["ts"] > fetch_start + step:
        return {}, fetch_start
    keep = {r["ts"]: r for r in rows if fetch_start <= r["ts"] <= until}
    if not keep:
        return {}, fetch_start
    return keep, max(fetch_start, max(keep) - window * step)


def download_bar(inst, bar, start_ms, end_ms, limit):
    step = off.bar_ms(bar)
    span = max(step, end_ms - start_ms)
    # Slack covers the in-progress bar on page 1 and a short last page.
    max_pages = math.ceil(span / step / limit) + 15
    rows, info = off.collect_candles(
        fetch_page, inst, bar, start_ms, end_ms,
        limit=limit, sleep=pace, path=HISTORY_PATH,
        max_pages=max_pages, on_page=_on_page(inst, bar),
    )
    recent_cap = math.ceil(RECENT_CAP / limit) + 3
    try:
        recent, rinfo = off.collect_candles(
            fetch_page, inst, bar, start_ms, end_ms,
            limit=limit, sleep=pace, path=CANDLES_PATH,
            max_pages=recent_cap,
        )
    except RuntimeError as e:
        print(
            f"  warning: recent {CANDLES_PATH} overlay failed ({e}). "
            "Keeping history-candles only.",
            file=sys.stderr,
        )
        recent = {}
        rinfo = {"pages": 0}
    info["recent_pages"] = rinfo["pages"]
    info["close_conflicts"] = off.merge_candles(rows, recent)
    info["short"] = not info["reached_start"]
    return rows, info


def main(argv=None):
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--inst", default="BTC-USDT")
    p.add_argument("--bars", default="5m,1H,4H",
                   help="comma-separated OKX bar ids (default 5m,1H,4H)")
    p.add_argument("--days", type=int, default=None,
                   help=(f"calendar days of signals for every bar. Omit for the "
                         f"per-bar default: {DEFAULT_DAYS} below 1H, "
                         f"{DEFAULT_DAYS_SLOW} for 1H and slower"))
    p.add_argument("--since", default=None, help="signal start, YYYY-MM-DD or epoch ms")
    p.add_argument("--until", default=None, help="signal end, default now")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="page size, OKX accepts up to 300")
    p.add_argument("--out-dir", default="data/ohlc")
    p.add_argument("--incremental", action="store_true",
                   help="reuse an existing CSV and fetch only its tail and newer bars")
    p.add_argument("--dry-run", action="store_true",
                   help="print the request plan and do not call OKX")
    args = p.parse_args(argv)

    try:
        bars = off.parse_bars(args.bars)
        items = build_plan(
            args.inst, bars, args.days, args.since, args.until,
            args.limit, args.out_dir, int(time.time() * 1000), neff.WINDOW,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(format_plan(items))
    if args.dry_run:
        print("dry-run: no request sent")
        return 0

    failed = False
    for item in items:
        kept, start = {}, item["fetch_start"]
        if args.incremental:
            kept, start = resume_point(
                item["path"], item["fetch_start"], item["until"],
                off.bar_ms(item["bar"]), item["window"],
            )
        mode = f"incremental from {off.iso(start)}, kept {len(kept)}" if kept else "full"
        print(f"fetching {item['inst']} {item['bar']} ({mode}) ...", flush=True)
        try:
            rows, info = download_bar(
                item["inst"], item["bar"], start, item["until"], item["limit"],
            )
        except RuntimeError as e:
            print(f"error: {item['inst']} {item['bar']}: {e}", file=sys.stderr)
            failed = True
            continue
        if kept:
            info["close_conflicts"] += off.merge_candles(kept, rows)
            rows = kept
        ordered = off.ordered_candles(rows)
        if not ordered:
            print(
                f"error: {item['inst']} {item['bar']}: no confirmed candles",
                file=sys.stderr,
            )
            failed = True
            continue
        path = Path(item["path"])
        off.write_ohlc(path, ordered)
        gaps, first = off.count_gaps(ordered, item["bar"])
        print(
            f"{item['inst']} {item['bar']}: {len(ordered)} candles "
            f"{off.iso(ordered[0]['ts'])} .. {off.iso(ordered[-1]['ts'])} "
            f"gaps={gaps} history_pages={info['pages']} "
            f"recent_pages={info['recent_pages']} "
            f"close_conflicts={info['close_conflicts']} -> {path}"
        )
        if info["short"]:
            oldest = info["oldest_seen"]
            oldest_s = off.iso(oldest) if oldest is not None else "none"
            print(
                f"  SHORT: history did not reach {off.iso(item['fetch_start'])} "
                f"(oldest page ts {oldest_s}). File written, coverage incomplete.",
                file=sys.stderr,
            )
            failed = True
        if gaps and first:
            print(
                f"  warning: {gaps} gaps, first {off.iso(first[0])} -> {off.iso(first[1])} "
                f"delta={first[2]} ms, expected {off.bar_ms(item['bar'])}",
                file=sys.stderr,
            )
        if info["close_conflicts"]:
            print(
                f"  warning: {info['close_conflicts']} closes differed; "
                "kept /market/candles on those timestamps.",
                file=sys.stderr,
            )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
