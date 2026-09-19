#!/usr/bin/env python3
"""Print a one-line commit message summarising the latest run.

This exists as its own file for one reason: it used to be a python -c inside
the workflow that read d['timeframes'], and when the JSON grew an
`instruments` layer the one-liner raised KeyError and took five runs down with
it -- including the first scheduled ones. The data had been fetched and written
fine; only the commit message failed.

So: it understands both the old and new shapes, and it never raises. A commit
message is cosmetic and must not be able to fail a run.
"""

import json
import sys

PATH = "data/neff_latest.json"
FALLBACK = "update"


def summarise(d):
    # new shape: {"instruments": {inst: {"timeframes": {...}}}}
    # old shape: {"timeframes": {...}}
    insts = d.get("instruments")
    if not insts:
        insts = {"": {"timeframes": d.get("timeframes", {})}}
    parts = []
    for inst, entry in insts.items():
        tf = (entry or {}).get("timeframes") or {}
        if not tf:
            continue
        body = " ".join(f"{bar}={row.get('n_eff')}" for bar, row in tf.items())
        parts.append(f"{inst} {body}".strip())
    return " | ".join(parts) or FALLBACK


def main():
    try:
        with open(PATH, encoding="utf-8") as f:
            print(summarise(json.load(f)))
    except Exception as e:                      # noqa: BLE001 - never fail
        print(FALLBACK)
        print(f"commit_msg: falling back ({e!r})", file=sys.stderr)


if __name__ == "__main__":
    main()
