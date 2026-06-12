#!/usr/bin/env python3
"""
run_linker.py
=============

Run tei_cts_linker over all XML files in this directory, with the
LocalPassageResolver wired in for quote verification, and produce
REPORT.md summarising the results.

Usage
-----
    python samples/run_linker.py [--samples DIR] [--gazetteer PATH]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from lxml import etree

from citation_resolution.tei_cts_linker import (
    Gazetteer,
    LinkStats,
    ScopeParser,
    TEILinker,
)
from kb.resolvers.perseus_resolver import make_resolver


# Locate the src/ package and kb/ from this script's location
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run linker over sample TEI files and write REPORT.md."
    )
    p.add_argument(
        "--samples",
        default=str(_HERE),
        metavar="DIR",
        help="directory of TEI XML samples (default: samples/)",
    )
    p.add_argument(
        "--gazetteer",
        default=str(_ROOT / "kb" / "data" / "gazetteer.json"),
        metavar="PATH",
    )
    p.add_argument(
        "--skeleton",
        default=str(_ROOT / "kb" / "data" / "cts.skeleton.json"),
        metavar="PATH",
    )
    p.add_argument(
        "--greek",
        default=None,
        metavar="PATH",
        help="canonical-greekLit repo (default: ~/code/PerseusDL/canonical-greekLit)",
    )
    p.add_argument(
        "--latin",
        default=None,
        metavar="PATH",
        help="canonical-latinLit repo (default: ~/code/PerseusCode/canonical-latinLit)",
    )
    p.add_argument(
        "--output-dir",
        default=str(_HERE),
        metavar="DIR",
        help="where to write REPORT.md and linked XML (default: samples/)",
    )
    p.add_argument("--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    samples_dir = Path(args.samples)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(samples_dir.glob("*.xml"))
    if not xml_files:
        print(f"No XML files found in {samples_dir}", file=sys.stderr)
        return 1

    print(f"Loading gazetteer from {args.gazetteer} …", file=sys.stderr)
    gaz = Gazetteer.from_json(args.gazetteer)

    print(f"Loading resolver …", file=sys.stderr)
    resolver = make_resolver(
        skeleton_path=args.skeleton,
        greek_repo=args.greek,
        latin_repo=args.latin,
        verbose=args.verbose,
    )

    scope_parser = ScopeParser()

    # Aggregate across all files
    total = LinkStats()
    all_review_items: list[dict] = []
    file_summaries: list[dict] = []

    lxml_parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)

    for xml_path in xml_files:
        print(f"\nProcessing {xml_path.name} …", file=sys.stderr)
        tree = etree.parse(str(xml_path), lxml_parser)

        linker = TEILinker(
            kb=gaz,
            scope_parser=scope_parser,
            resolver=resolver,
            decompose=False,
        )
        stats = linker.run(tree)

        # Write enriched XML alongside the input
        out_path = output_dir / xml_path.name.replace(".xml", ".linked.xml")
        out_bytes = etree.tostring(
            tree, xml_declaration=True, encoding="UTF-8", pretty_print=True
        )
        with open(out_path, "wb") as fh:
            fh.write(out_bytes)
        print(f"  wrote {out_path.name}", file=sys.stderr)

        file_summaries.append(
            {
                "file": xml_path.name,
                "bibl_seen": stats.bibl_seen,
                "cit_seen": stats.cit_seen,
                "linked": stats.linked,
                "review": stats.review,
                "skipped": stats.skipped,
                "verified": stats.verified,
                "verify_failed": stats.verify_failed,
            }
        )
        # Accumulate totals
        total.bibl_seen += stats.bibl_seen
        total.cit_seen += stats.cit_seen
        total.linked += stats.linked
        total.review += stats.review
        total.skipped += stats.skipped
        total.verified += stats.verified
        total.verify_failed += stats.verify_failed
        all_review_items.extend(stats.review_items)

        print(
            f"  bibl={stats.bibl_seen} cit={stats.cit_seen} "
            f"linked={stats.linked} review={stats.review} "
            f"skipped={stats.skipped} verified={stats.verified} "
            f"verify_failed={stats.verify_failed}",
            file=sys.stderr,
        )

    # Write the JSON report
    report_json = output_dir / "report.json"
    with open(report_json, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "totals": {
                    "bibl_seen": total.bibl_seen,
                    "cit_seen": total.cit_seen,
                    "linked": total.linked,
                    "review": total.review,
                    "skipped": total.skipped,
                    "verified": total.verified,
                    "verify_failed": total.verify_failed,
                },
                "files": file_summaries,
                "review_items": all_review_items,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )

    _write_report(output_dir, total, file_summaries, all_review_items)
    return 0


def _write_report(
    output_dir: Path,
    total: LinkStats,
    file_summaries: list[dict],
    review_items: list[dict],
) -> None:
    """Write REPORT.md to output_dir."""

    # Triage review items by cause
    cause_counts: dict[str, int] = collections.Counter()
    by_cause: dict[str, list[dict]] = collections.defaultdict(list)
    for item in review_items:
        note = item.get("note", "")
        if "work unresolved" in note:
            cause = "author matched, work unresolved"
        elif "scope not CTS-parseable" in note:
            cause = "scope not CTS-parseable (fragment / editor scheme)"
        elif "ambiguous" in note and "MISMATCH" not in note:
            cause = "ambiguous abbreviation"
        elif "MISMATCH" in note:
            cause = "quote mismatch (verify_failed)"
        elif "ambiguous" in note and "MISMATCH" in note:
            cause = "ambiguous + quote mismatch"
        else:
            cause = note or "unresolved (other)"
        cause_counts[cause] += 1
        by_cause[cause].append(item)

    # Unresolved author tokens (from review items where the note mentions work)
    unknown_authors: dict[str, int] = collections.Counter()
    for item in review_items:
        author = item.get("author") or ""
        if author:
            unknown_authors[author] += 1

    # Compute resolve rate
    total_refs = total.linked + total.review + total.skipped
    link_pct = f"{100 * total.linked // total_refs if total_refs else 0}%"
    review_pct = f"{100 * total.review // total_refs if total_refs else 0}%"
    skip_pct = f"{100 * total.skipped // total_refs if total_refs else 0}%"

    lines: list[str] = []
    a = lines.append

    a("# Linker Run Report\n")

    a("## Overall Statistics\n")
    a(f"| Metric | Count |")
    a(f"|--------|-------|")
    a(f"| bibl elements seen | {total.bibl_seen} |")
    a(f"| cit elements seen | {total.cit_seen} |")
    a(f"| **linked** | **{total.linked}** ({link_pct}) |")
    a(f"| review | {total.review} ({review_pct}) |")
    a(f"| skipped (secondary / no KB match) | {total.skipped} ({skip_pct}) |")
    a(f"| quotes verified ✓ | {total.verified} |")
    a(f"| quote mismatches ✗ | {total.verify_failed} |")
    a("")

    if len(file_summaries) > 1:
        a("## Per-File Breakdown\n")
        a("| File | bibl | cit | linked | review | skipped | verified | failed |")
        a("|------|------|-----|--------|--------|---------|----------|--------|")
        for s in file_summaries:
            a(
                f"| {s['file']} | {s['bibl_seen']} | {s['cit_seen']} | "
                f"{s['linked']} | {s['review']} | {s['skipped']} | "
                f"{s['verified']} | {s['verify_failed']} |"
            )
        a("")

    a("## Review Queue: Items by Cause\n")
    if not cause_counts:
        a("*(no review items)*\n")
    else:
        a("| Cause | Count | Priority |")
        a("|-------|-------|----------|")
        priority_map = {
            "author matched, work unresolved": "HIGH — add default_work or work abbreviation",
            "scope not CTS-parseable (fragment / editor scheme)": "MEDIUM — fragment references; no CTS mapping exists",
            "ambiguous abbreviation": "HIGH — add disambiguation data or use quote verification",
            "quote mismatch (verify_failed)": "HIGH — wrong URN or wrong quote in source",
            "ambiguous + quote mismatch": "HIGH — ambiguous and verification failed",
        }
        for cause, count in sorted(cause_counts.items(), key=lambda x: -x[1]):
            priority = priority_map.get(cause, "LOW")
            a(f"| {cause} | {count} | {priority} |")
        a("")

    a("## Review Queue: Item Details\n")
    if not review_items:
        a("*(none)*\n")
    else:
        for cause in sorted(cause_counts.keys(), key=lambda c: -cause_counts[c]):
            items = by_cause[cause]
            a(f"### {cause}\n")
            a("| Raw citation | Author token | Work token | Scope | Partial URN |")
            a("|-------------|-------------|------------|-------|-------------|")
            for item in items:
                raw = item.get("raw", "").replace("|", "\\|")
                author = item.get("author") or "—"
                work = item.get("work") or "—"
                scope = item.get("scope") or "—"
                partial = item.get("partial_urn") or "—"
                a(f"| `{raw}` | `{author}` | `{work}` | `{scope}` | `{partial}` |")
            a("")

    a("## Curation Priorities\n")

    if cause_counts.get("author matched, work unresolved", 0) > 0:
        items = by_cause["author matched, work unresolved"]
        authors_needing_work = sorted(
            {i.get("author", "") for i in items if i.get("author")}
        )
        a("**1. Add `default_work` or explicit work abbreviations** for:")
        for auth in authors_needing_work:
            a(f"   - `{auth}` (single-work or commonly cited without title)")
        a("")

    if cause_counts.get("ambiguous abbreviation", 0) > 0:
        items = by_cause["ambiguous abbreviation"]
        a("**2. Resolve abbreviation ambiguities:**")
        a("   Enable the `LocalPassageResolver` for quote verification, which")
        a("   promotes ambiguous matches to `linked` when the quote appears in")
        a("   the fetched passage text.")
        a("")

    if cause_counts.get("quote mismatch (verify_failed)", 0) > 0:
        items = by_cause["quote mismatch (verify_failed)"]
        a("**3. Investigate quote mismatches** — the cited locus may be wrong")
        a("   in the source TEI, or the passage text in the repo may differ from")
        a("   the edition used by the commentary author:")
        for item in items:
            a(
                f"   - `{item.get('raw', '?')}` (partial URN: `{item.get('partial_urn', '?')}`)"
            )
        a("")

    a("## Skipped References (not in KB)\n")
    a("The following author tokens produced no KB match and were skipped.")
    a("If any are canonical primary-source citations, add their abbreviations")
    a("to the gazetteer.\n")
    a("*(Skipped references are not collected in the review queue — see the*")
    a("*linked XML for `nel:status` attributes or check the raw input.)*\n")

    a("## Next Steps\n")
    a("1. **Gazetteer gaps**: run `python kb/enrich_gazetteer.py --report-only`")
    a("   to see which textgroups still lack `name_abbrevs`.")
    a("2. **Work abbreviations**: textgroups with no `title_abbrevs` on any work")
    a("   will never produce a `linked` outcome — prioritize the authors that")
    a("   appear most often in the review queue above.")
    a("3. **`default_work`**: single-work authors cited without a title token")
    a("   (e.g. Herodotus, Thucydides) need `default_work` set in the gazetteer.")
    a("4. **Restricted seeds**: run with `--restricted` to apply LSJ/OLD/TLL")
    a("   abbreviations privately, then re-run this report to measure uplift.")

    report_path = output_dir / "REPORT.md"
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nWrote {report_path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
