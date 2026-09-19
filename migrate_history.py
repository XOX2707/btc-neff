#!/usr/bin/env python3
"""One-off migration of data/neff_history.csv.

Why this exists: n_eff used to be derived from the small-angle form
N = pi/sqrt(3(kappa-1)), which understates N by ~7-13% in the range a jagged
price path actually occupies (it is exact only as N -> infinity). The fix is a
numerical inversion of kappa(N).

Because the raw kappa was stored with every row, the whole history can be
recomputed exactly rather than left on the old scale -- no discontinuity in the
series. This also backfills the two new columns (c_null, c_ratio).

Run once, from the repo root:

    python migrate_history.py

It rewrites data/neff_history.csv in place after printing what changed.
Safe to run twice: rows already carrying c_ratio are recomputed to the same
values.
"""

import csv
import sys
from pathlib import Path

import neff

CSV = Path("data/neff_history.csv")
NEW_COLS = ["generated_at", "instrument", "bar", "candle_ts", "last",
            "kappa", "n_eff", "n_turn", "consistency", "c_null", "c_ratio",
            "efficiency_ratio", "state"]


def main():
    if not CSV.exists():
        sys.exit(f"{CSV} not found -- run this from the repo root.")

    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    if not rows:
        sys.exit("history is empty, nothing to migrate")

    cn = neff.c_null(neff.WINDOW)
    print(f"window={neff.WINDOW}  c_null={cn:.5f}  rows={len(rows)}\n")
    print(f"{'bar':>5} {'kappa':>9} {'n_eff old':>10} {'n_eff new':>10} "
          f"{'shift':>7}  {'state':>9}")

    shifts, out = [], []
    for r in rows:
        k = float(r["kappa"])
        old = float(r["n_eff"])
        new = neff.n_from_kappa(k)
        c = float(r["consistency"])
        state_old = r["state"]
        state_new = neff.label(new, c)

        new_r = round(min(new, 999.0), 2)
        shifts.append((new_r - old) / old if old else 0.0)
        flag = "" if state_new == state_old else f"  {state_old} -> {state_new}"
        print(f"{r['bar']:>5} {k:>9.4f} {old:>10.2f} {new_r:>10.2f} "
              f"{(new_r-old)/old:>6.1%}  {state_new:>9}{flag}")

        r["n_eff"] = new_r
        r["state"] = state_new
        r["c_null"] = round(cn, 5)
        r["c_ratio"] = round(c / cn, 3) if cn > 0 else 0.0
        out.append({k2: r.get(k2, "") for k2 in NEW_COLS})

    with CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NEW_COLS)
        w.writeheader()
        w.writerows(out)

    avg = sum(shifts) / len(shifts)
    print(f"\nrewrote {len(out)} rows; mean n_eff shift {avg:+.1%}")


if __name__ == "__main__":
    main()
