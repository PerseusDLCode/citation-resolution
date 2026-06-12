#!/usr/bin/env python3
"""
enrich_gazetteer.py
===================

Layer additional abbreviations onto the merged gazetteer produced by
merge_gazetteer.py, drawing from two clearly-separated seed stores:

  kb/seeds/open/        CC-licensed seeds, bundled in the repo
  kb/seeds/restricted/  Proprietary seeds (LSJ/OLD/TLL/etc.) — gitignored,
                        MUST NOT be committed or redistributed

Each seed file is a JSON object:
  {
    "source":  "hand_curated" | "lsj" | "old" | ...,
    "license": "CC0" | "RESTRICTED: LSJ" | ...,
    "note":    "...",
    "entries": [
      {
        // Direct-binding form (preferred — no reconciliation needed):
        "tg_urn":      "urn:cts:greekLit:tlg0012",
        "author_abbrev": "Hom.",
        "author_name":   "Homer",           // informational only

        // Optional work binding:
        "work_urn":    "urn:cts:greekLit:tlg0012.tlg001",
        "work_abbrev": "Il.",
        "work_name":   "Iliad"
      },
      // Name-reconciliation form (tg_urn absent):
      {
        "author_name":   "Thucydides",
        "author_abbrev": "Th."
      }
    ]
  }

LICENSING GUARDRAIL
-------------------
Seeds in kb/seeds/restricted/ are loaded ONLY when --restricted is given
and MUST NOT be written into any publicly-distributable file.
The default output (--output) is open-only.
The restricted overlay (--restricted-output) is a separate file that the
caller must keep private.

Usage
-----
    # Open-only (redistributable):
    python kb/enrich_gazetteer.py

    # With restricted overlay (private build only):
    python kb/enrich_gazetteer.py --restricted

    # Coverage report only, no files written:
    python kb/enrich_gazetteer.py --report-only
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import _build_name_index, _norm  # noqa: E402

OPEN_SEEDS_DIR = Path(__file__).parent / "seeds" / "open"
RESTRICTED_SEEDS_DIR = Path(__file__).parent / "seeds" / "restricted"


def _build_work_name_index(gazetteer: dict) -> dict[str, dict[str, list[str]]]:
    """{tg_urn: {normalised_title: [work_urn, ...]}}"""
    idx: dict[str, dict[str, list[str]]] = {}
    for tg_urn, rec in gazetteer.items():
        widx: dict[str, list[str]] = {}
        for w_urn, w_rec in rec.get("works", {}).items():
            title = w_rec.get("canonical_title", "")
            if title:
                widx.setdefault(_norm(title), []).append(w_urn)
        if widx:
            idx[tg_urn] = widx
    return idx


# --------------------------------------------------------------------------- #
# Seed loading
# --------------------------------------------------------------------------- #
def _load_seeds(seed_dir: Path, label: str) -> list[dict]:
    seeds: list[dict] = []
    if not seed_dir.exists():
        return seeds
    for path in sorted(seed_dir.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            seeds.append(data)
            n = len(data.get("entries", []))
            print(f"  [{label}] {path.name}: {n} entries", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  warning: could not load {path}: {exc}", file=sys.stderr)
    return seeds


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def _apply_seeds(
    gazetteer: dict,
    seeds: list[dict],
    name_idx: dict[str, list[str]],
    work_name_idx: dict[str, dict[str, list[str]]],
    source_tag: str,
) -> dict:
    """Apply seed entries to gazetteer in place; return stats dict."""
    stats: dict = {
        "source": source_tag,
        "author_abbrevs_added": 0,
        "work_abbrevs_added": 0,
        "reconcile_ok": 0,
        "reconcile_failed": [],
    }

    for seed in seeds:
        for entry in seed.get("entries", []):
            tg_urn = entry.get("tg_urn")

            # Reconcile by name when tg_urn not directly given
            if not tg_urn:
                author_name = entry.get("author_name", "")
                candidates = name_idx.get(_norm(author_name), [])
                if len(candidates) == 1:
                    tg_urn = candidates[0]
                    stats["reconcile_ok"] += 1
                elif len(candidates) > 1:
                    stats["reconcile_failed"].append(
                        {"author_name": author_name, "reason": "ambiguous", "candidates": candidates}
                    )
                    continue
                else:
                    stats["reconcile_failed"].append(
                        {"author_name": author_name, "reason": "not_found"}
                    )
                    continue

            if tg_urn not in gazetteer:
                gazetteer[tg_urn] = {
                    "canonical_name": entry.get("author_name", ""),
                    "name_abbrevs": [],
                    "default_work": entry.get("default_work"),
                    "works": {},
                }
            elif entry.get("default_work") and not gazetteer[tg_urn].get("default_work"):
                gazetteer[tg_urn]["default_work"] = entry["default_work"]

            rec = gazetteer[tg_urn]

            author_abbrev = entry.get("author_abbrev")
            if author_abbrev and author_abbrev not in rec["name_abbrevs"]:
                rec["name_abbrevs"].append(author_abbrev)
                stats["author_abbrevs_added"] += 1

            work_abbrev = entry.get("work_abbrev")
            if work_abbrev:
                work_urn = entry.get("work_urn")
                if not work_urn:
                    work_name = entry.get("work_name", "")
                    w_cands = work_name_idx.get(tg_urn, {}).get(_norm(work_name), [])
                    if len(w_cands) == 1:
                        work_urn = w_cands[0]
                    else:
                        stats["reconcile_failed"].append(
                            {"work_name": work_name, "tg_urn": tg_urn, "reason": "work_not_found"}
                        )
                        continue

                if work_urn not in rec["works"]:
                    rec["works"][work_urn] = {
                        "canonical_title": entry.get("work_name", ""),
                        "title_abbrevs": [],
                        "scheme": entry.get("scheme", "flat"),
                    }
                w_rec = rec["works"][work_urn]
                if work_abbrev not in w_rec["title_abbrevs"]:
                    w_rec["title_abbrevs"].append(work_abbrev)
                    stats["work_abbrevs_added"] += 1

    return stats


# --------------------------------------------------------------------------- #
# Coverage report
# --------------------------------------------------------------------------- #
def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n // d if d else 0}%)"


def _coverage_report(gazetteer: dict, all_stats: list[dict]) -> None:
    def _counts(ns: str) -> tuple[int, int, int, int]:
        tg_recs = [r for tg, r in gazetteer.items() if ns in tg]
        with_name = sum(1 for r in tg_recs if r.get("name_abbrevs"))
        works = [w for r in tg_recs for w in r.get("works", {}).values()]
        with_title = sum(1 for w in works if w.get("title_abbrevs"))
        return len(tg_recs), with_name, len(works), with_title

    g_tg, g_na, g_w, g_wa = _counts("greekLit")
    l_tg, l_na, l_w, l_wa = _counts("latinLit")

    print("\n=== Coverage Report ===", file=sys.stderr)
    print(f"  Greek textgroups  with name_abbrevs : {_pct(g_na, g_tg)}", file=sys.stderr)
    print(f"  Greek works       with title_abbrevs: {_pct(g_wa, g_w)}", file=sys.stderr)
    print(f"  Latin textgroups  with name_abbrevs : {_pct(l_na, l_tg)}", file=sys.stderr)
    print(f"  Latin works       with title_abbrevs: {_pct(l_wa, l_w)}", file=sys.stderr)

    if all_stats:
        print("\nSeed results:", file=sys.stderr)
        for s in all_stats:
            failed = len(s["reconcile_failed"])
            print(
                f"  [{s['source']}] "
                f"+{s['author_abbrevs_added']} author abbrevs, "
                f"+{s['work_abbrevs_added']} work abbrevs, "
                f"{s['reconcile_ok']} name-reconciled, "
                f"{failed} unreconciled",
                file=sys.stderr,
            )
            for f in s["reconcile_failed"][:5]:
                print(f"    ! {f}", file=sys.stderr)
            if failed > 5:
                print(f"    … and {failed - 5} more", file=sys.stderr)

    missing_tg = sorted(
        [
            (tg, r.get("canonical_name", ""), len(r.get("works", {})))
            for tg, r in gazetteer.items()
            if ("greekLit" in tg or "latinLit" in tg) and not r.get("name_abbrevs")
        ],
        key=lambda x: -x[2],
    )
    if missing_tg:
        print(
            f"\nTop {min(10, len(missing_tg))} greekLit/latinLit textgroups still lacking name_abbrevs:",
            file=sys.stderr,
        )
        for tg, name, n_works in missing_tg[:10]:
            print(f"  {tg}  {name!r}  ({n_works} works)", file=sys.stderr)
    else:
        print("\nAll greekLit/latinLit textgroups have name_abbrevs.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Enrich merged gazetteer with open and (optionally) restricted abbreviation seeds."
    )
    p.add_argument(
        "--input",
        default="kb/data/gazetteer.json",
        metavar="PATH",
        help="merged gazetteer to enrich (default: kb/data/gazetteer.json)",
    )
    p.add_argument(
        "--output",
        default="kb/data/gazetteer.json",
        metavar="PATH",
        help="open-only output path (default: overwrites input)",
    )
    p.add_argument(
        "--restricted",
        action="store_true",
        help="also load kb/seeds/restricted/ (output MUST NOT be redistributed)",
    )
    p.add_argument(
        "--restricted-output",
        default="kb/data/gazetteer.restricted.json",
        metavar="PATH",
        help="restricted overlay output path (default: kb/data/gazetteer.restricted.json)",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="print coverage report without writing any files",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    print(f"Loading {args.input} …", file=sys.stderr)
    with open(args.input, encoding="utf-8") as fh:
        gazetteer = json.load(fh)

    name_idx = _build_name_index(gazetteer)
    work_name_idx = _build_work_name_index(gazetteer)
    all_stats: list[dict] = []

    # Open seeds ------------------------------------------------------------ #
    print("Loading open seeds …", file=sys.stderr)
    open_seeds = _load_seeds(OPEN_SEEDS_DIR, "open")
    if open_seeds:
        stats = _apply_seeds(gazetteer, open_seeds, name_idx, work_name_idx, "open")
        all_stats.append(stats)

    _coverage_report(gazetteer, all_stats)

    if not args.report_only:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(gazetteer, fh, ensure_ascii=False, indent=2)
        print(f"\nWrote open-only gazetteer: {out}", file=sys.stderr)

    # Restricted seeds ------------------------------------------------------ #
    if args.restricted:
        print(
            "\n*** RESTRICTED seeds — output MUST NOT be publicly distributed ***",
            file=sys.stderr,
        )
        r_gaz = copy.deepcopy(gazetteer)
        r_name_idx = _build_name_index(r_gaz)
        r_work_idx = _build_work_name_index(r_gaz)
        r_seeds = _load_seeds(RESTRICTED_SEEDS_DIR, "RESTRICTED")

        if r_seeds:
            r_stats = _apply_seeds(r_gaz, r_seeds, r_name_idx, r_work_idx, "restricted")
            _coverage_report(r_gaz, [r_stats])

            if not args.report_only:
                rout = Path(args.restricted_output)
                rout.parent.mkdir(parents=True, exist_ok=True)
                with open(rout, "w", encoding="utf-8") as fh:
                    json.dump(r_gaz, fh, ensure_ascii=False, indent=2)
                print(f"\nWrote restricted gazetteer: {rout}", file=sys.stderr)
                print("  *** Keep this file private — contains proprietary abbreviations ***", file=sys.stderr)
        else:
            print("  (no seed files found in kb/seeds/restricted/)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
