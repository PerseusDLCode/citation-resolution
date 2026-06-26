#!/usr/bin/env python3
"""
merge_gazetteer.py
==================

Merge the HuCit abbreviation gazetteer with the CTS skeleton to produce the
production knowledge base consumed by Gazetteer.from_dict() in tei_cts_linker.py.

Merge rules (by URN key):
  - CTS skeleton is authoritative for identity and citation scheme.
  - HuCit is authoritative for name_abbrevs and title_abbrevs.
  - For textgroups in both: overlay HuCit abbrevs onto the CTS record.
  - For textgroups in HuCit only: include them (scheme stays "flat").
  - For textgroups in CTS only: include them (abbrevs stay empty).
  - Works follow the same pattern within each textgroup.
  - default_work is taken from HuCit when set (CTS skeleton never sets it).

Usage:
    python kb/merge_gazetteer.py
    python kb/merge_gazetteer.py \\
        --hucit  kb/data/hucit.gazetteer.json \\
        --cts    kb/data/cts.skeleton.json \\
        --output src/citation_resolution/data/gazetteer.json \\
        --verify
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
def merge(hucit: dict, cts: dict) -> tuple[dict, dict]:
    """
    Return (merged_gazetteer, log) where log records URNs that appear in only
    one source and works whose scheme was promoted from 'flat'.
    """
    log: dict = {
        "tg_hucit_only": [],
        "tg_cts_only": [],
        "work_hucit_only": [],
        "work_cts_only": [],
        "schemes_promoted": 0,
        "schemes_still_flat": 0,
    }

    hucit_tg = set(hucit)
    cts_tg = set(cts)

    result: dict = {}

    # ------------------------------------------------------------------ #
    # 1. Shared textgroups: CTS base + HuCit abbrevs overlaid
    # ------------------------------------------------------------------ #
    for tg_urn in hucit_tg & cts_tg:
        h_rec = hucit[tg_urn]
        c_rec = cts[tg_urn]

        merged_rec: dict = {
            "name_abbrevs": list(h_rec.get("name_abbrevs") or []),
            "default_work": h_rec.get("default_work"),
            "works": {},
        }

        h_works = h_rec.get("works", {})
        c_works = c_rec.get("works", {})

        # Works in both: CTS scheme, HuCit title_abbrevs
        for w_urn in set(h_works) & set(c_works):
            h_w = h_works[w_urn]
            c_w = c_works[w_urn]
            scheme = c_w["scheme"]  # CTS wins
            if scheme != "flat" and h_w.get("scheme") == "flat":
                log["schemes_promoted"] += 1
            merged_rec["works"][w_urn] = {
                "title_abbrevs": list(h_w.get("title_abbrevs") or []),
                "scheme": scheme,
            }

        # Works in HuCit only: add as-is (scheme stays flat)
        for w_urn in set(h_works) - set(c_works):
            merged_rec["works"][w_urn] = copy.deepcopy(h_works[w_urn])
            log["work_hucit_only"].append(w_urn)

        # Works in CTS only: keep with empty abbrevs
        for w_urn in set(c_works) - set(h_works):
            merged_rec["works"][w_urn] = copy.deepcopy(c_works[w_urn])
            log["work_cts_only"].append(w_urn)

        result[tg_urn] = merged_rec

    # ------------------------------------------------------------------ #
    # 2. HuCit-only textgroups: include verbatim
    # ------------------------------------------------------------------ #
    for tg_urn in hucit_tg - cts_tg:
        result[tg_urn] = copy.deepcopy(hucit[tg_urn])
        log["tg_hucit_only"].append(tg_urn)

    # ------------------------------------------------------------------ #
    # 3. CTS-only textgroups: include with empty abbrevs
    # ------------------------------------------------------------------ #
    for tg_urn in cts_tg - hucit_tg:
        result[tg_urn] = copy.deepcopy(cts[tg_urn])
        log["tg_cts_only"].append(tg_urn)

    # Count remaining flat schemes
    log["schemes_still_flat"] = sum(
        1
        for rec in result.values()
        for w in rec["works"].values()
        if w["scheme"] == "flat"
    )

    return result, log


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def verify(gazetteer: dict) -> bool:
    ok = True

    checks = [
        (
            "urn:cts:greekLit:tlg0012",
            "urn:cts:greekLit:tlg0012.tlg001",
            ["Hom."],
            ["Il."],
            "book.line",
        ),
        (
            "urn:cts:greekLit:tlg0003",
            "urn:cts:greekLit:tlg0003.tlg001",
            ["Th."],
            [],
            "book.chapter.section",
        ),
        (
            "urn:cts:greekLit:tlg0059",
            "urn:cts:greekLit:tlg0059.tlg030",
            ["Pl."],
            [],
            "stephanus",
        ),
    ]

    for tg_urn, w_urn, name_abbrevs, title_abbrevs, scheme in checks:
        tg = gazetteer.get(tg_urn)
        if tg is None:
            print(f"FAIL: textgroup {tg_urn} not found", file=sys.stderr)
            ok = False
            continue
        for abbr in name_abbrevs:
            if abbr not in tg["name_abbrevs"]:
                print(
                    f"FAIL: {abbr!r} not in name_abbrevs for {tg_urn}", file=sys.stderr
                )
                ok = False
        w = tg["works"].get(w_urn)
        if w is None:
            print(f"FAIL: work {w_urn} not found", file=sys.stderr)
            ok = False
            continue
        for abbr in title_abbrevs:
            if abbr not in w["title_abbrevs"]:
                print(
                    f"FAIL: {abbr!r} not in title_abbrevs for {w_urn}", file=sys.stderr
                )
                ok = False
        if w["scheme"] != scheme:
            print(
                f"FAIL: {w_urn} scheme={w['scheme']!r}, expected {scheme!r}",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"OK  : {w_urn} scheme={scheme!r} abbrevs OK", file=sys.stderr)

    # No shared work should retain scheme="flat" if CTS had a real scheme
    flat_proper = [
        (tg, w_urn)
        for tg, rec in gazetteer.items()
        for w_urn, w in rec["works"].items()
        if w["scheme"] == "flat" and ("greekLit" in w_urn or "latinLit" in w_urn)
    ]
    if flat_proper:
        print(
            f"INFO: {len(flat_proper)} greekLit/latinLit works still have scheme='flat' "
            "(no CTS refsDecl in either source — expected for partial coverage)",
            file=sys.stderr,
        )
    else:
        print("OK  : no greekLit/latinLit works retain scheme='flat'", file=sys.stderr)

    # Smoke-test Gazetteer round-trip
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from citation_resolution.tei_cts_linker import Gazetteer

        gaz = Gazetteer.from_dict(gazetteer)
        authors = gaz.author_for("Hom.")
        assert authors, "Hom. not found"
        work = gaz.work_for(authors[0], "Il.")
        assert work is not None, "Il. not found under Homer"
        assert work.scheme == "book.line", f"Iliad scheme={work.scheme!r}"
        print(
            f"OK  : Gazetteer round-trip: Hom. Il. -> {work.work_urn} [{work.scheme}]",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"FAIL: Gazetteer round-trip: {exc}", file=sys.stderr)
        ok = False

    if ok:
        print("verify: all checks passed", file=sys.stderr)
    return ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Merge HuCit abbreviations with CTS skeleton into production gazetteer."
    )
    p.add_argument(
        "--hucit",
        default="kb/data/hucit.gazetteer.json",
        metavar="PATH",
        help="HuCit gazetteer JSON (default: kb/data/hucit.gazetteer.json)",
    )
    p.add_argument(
        "--cts",
        default="kb/data/cts.skeleton.json",
        metavar="PATH",
        help="CTS skeleton JSON (default: kb/data/cts.skeleton.json)",
    )
    p.add_argument(
        "--output",
        default="src/citation_resolution/data/gazetteer.json",
        metavar="PATH",
        help="output merged gazetteer (default: kb/data/gazetteer.json)",
    )
    p.add_argument(
        "--verify", action="store_true", help="run spot-checks after writing"
    )
    p.add_argument(
        "--log",
        metavar="PATH",
        help="write merge log JSON to this path",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print(f"Loading HuCit gazetteer from {args.hucit} …", file=sys.stderr)
    with open(args.hucit, encoding="utf-8") as fh:
        hucit = json.load(fh)

    print(f"Loading CTS skeleton from {args.cts} …", file=sys.stderr)
    with open(args.cts, encoding="utf-8") as fh:
        cts = json.load(fh)

    print("Merging …", file=sys.stderr)
    gazetteer, log = merge(hucit, cts)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(gazetteer, fh, ensure_ascii=False, indent=2)

    tg_count = len(gazetteer)
    work_count = sum(len(r["works"]) for r in gazetteer.values())
    print(
        f"\nWrote {out_path}: {tg_count:,} textgroups, {work_count:,} works",
        file=sys.stderr,
    )
    print(f"  textgroups HuCit-only: {len(log['tg_hucit_only']):,}", file=sys.stderr)
    print(f"  textgroups CTS-only:   {len(log['tg_cts_only']):,}", file=sys.stderr)
    print(
        f"  works HuCit-only (shared tg): {len(log['work_hucit_only']):,}",
        file=sys.stderr,
    )
    print(
        f"  works CTS-only (shared tg):   {len(log['work_cts_only']):,}",
        file=sys.stderr,
    )
    print(
        f"  schemes promoted flat→real:   {log['schemes_promoted']:,}", file=sys.stderr
    )
    print(
        f"  schemes still flat:           {log['schemes_still_flat']:,}",
        file=sys.stderr,
    )

    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(log, fh, ensure_ascii=False, indent=2)
        print(f"  log written to {log_path}", file=sys.stderr)

    if args.verify:
        ok = verify(gazetteer)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
