#!/usr/bin/env python3
"""
prepare_restricted_seeds.py
============================

Convert proprietary abbreviation sources into the seed format expected by
enrich_gazetteer.py and write them to kb/seeds/restricted/ (gitignored).

LICENSING GUARDRAIL
-------------------
The output files go in kb/seeds/restricted/, which is gitignored.
NEVER commit or redistribute these files.

Seed file format
----------------
Each output JSON file must follow the schema consumed by enrich_gazetteer.py:

  {
    "source":  "lsj" | "old" | ...,
    "license": "RESTRICTED: <source name> — DO NOT REDISTRIBUTE",
    "note":    "...",
    "entries": [
      {
        "tg_urn":      "urn:cts:greekLit:tlg0012",   // preferred: direct binding
        "author_abbrev": "Hom.",
        "author_name":   "Homer",

        // optional work binding:
        "work_urn":    "urn:cts:greekLit:tlg0012.tlg001",
        "work_abbrev": "Il.",
        "work_name":   "Iliad"
      }
    ]
  }

To add a new restricted source
--------------------------------
1. Obtain the abbreviation data (e.g. from LSJ, OLD, TLL) in any format.
2. Write a converter function below (following the pattern of convert_tabular).
3. Add a --<source> argument and call it in main().
4. Run this script; the output goes to kb/seeds/restricted/<source>.json.
5. Then run: python kb/enrich_gazetteer.py --restricted

Usage
-----
    python kb/prepare_restricted_seeds.py --tabular PATH --source lsj
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import _build_name_index, _norm  # noqa: E402

RESTRICTED_DIR = Path(__file__).parent / "seeds" / "restricted"


def convert_tabular(path: str, source: str, gazetteer: dict) -> dict:
    """
    Generic converter for a CSV/TSV file with columns:
      author_abbrev, author_name[, work_abbrev, work_name[, tg_urn[, work_urn]]]

    Direct URN columns (tg_urn, work_urn) bypass name reconciliation when present.
    """
    name_idx = _build_name_index(gazetteer)

    with open(path, encoding="utf-8", newline="") as fh:
        # Sniff delimiter
        sample = fh.read(4096)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|")
        reader = csv.DictReader(fh, dialect=dialect)
        rows = list(reader)

    entries = []
    skipped = 0
    for row in rows:
        abbrev = (row.get("author_abbrev") or "").strip()
        name = (row.get("author_name") or "").strip()
        tg_urn = (row.get("tg_urn") or "").strip() or None

        if not abbrev:
            skipped += 1
            continue

        if not tg_urn and name:
            candidates = name_idx.get(_norm(name), [])
            if len(candidates) == 1:
                tg_urn = candidates[0]
            else:
                skipped += 1
                continue

        if not tg_urn:
            skipped += 1
            continue

        entry: dict = {"tg_urn": tg_urn, "author_abbrev": abbrev}
        if name:
            entry["author_name"] = name

        work_abbrev = (row.get("work_abbrev") or "").strip()
        work_name = (row.get("work_name") or "").strip()
        work_urn = (row.get("work_urn") or "").strip() or None
        if work_abbrev:
            entry["work_abbrev"] = work_abbrev
            if work_name:
                entry["work_name"] = work_name
            if work_urn:
                entry["work_urn"] = work_urn

        entries.append(entry)

    print(
        f"{source}: {len(rows)} rows, {len(entries)} converted, {skipped} skipped",
        file=sys.stderr,
    )
    return {
        "source": source,
        "license": f"RESTRICTED: {source.upper()} — DO NOT REDISTRIBUTE",
        "note": f"Converted from {Path(path).name}. Keep this file private.",
        "entries": entries,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert proprietary abbreviation data to restricted seed files."
    )
    p.add_argument(
        "--tabular",
        metavar="PATH",
        help="CSV/TSV with columns: author_abbrev, author_name[, work_abbrev, work_name[, tg_urn[, work_urn]]]",
    )
    p.add_argument(
        "--source",
        metavar="NAME",
        default="restricted",
        help='source label used in the output filename and seed metadata (e.g. "lsj", "old")',
    )
    p.add_argument(
        "--gazetteer",
        default="kb/data/gazetteer.json",
        metavar="PATH",
        help="merged gazetteer for name reconciliation (default: kb/data/gazetteer.json)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.tabular:
        print("Nothing to do — pass --tabular PATH to convert a source.", file=sys.stderr)
        print(__doc__)
        return 0

    print(f"Loading gazetteer from {args.gazetteer} …", file=sys.stderr)
    with open(args.gazetteer, encoding="utf-8") as fh:
        gazetteer = json.load(fh)

    RESTRICTED_DIR.mkdir(parents=True, exist_ok=True)

    seed = convert_tabular(args.tabular, args.source, gazetteer)
    out = RESTRICTED_DIR / f"{args.source}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(seed, fh, ensure_ascii=False, indent=2)
    print(f"Wrote {out} ({len(seed['entries'])} entries) — KEEP PRIVATE", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
