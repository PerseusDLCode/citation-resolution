#!/usr/bin/env python3
"""
csv_to_p4_seeds.py
===================

Convert Alison Babeu's "P4 ABOs to CTS-URNs - Works" spreadsheet into the
seed formats this repo already consumes (see enrich_gazetteer.py), plus a
P4-doc-id <-> CTS-URN crosswalk that isn't part of the gazetteer schema at
all but is needed by MinimumViablePerseus to resolve old hopper `doc=`
links (Perseus/MinimumViablePerseus#169, requirement 1).

Why not group by the CSV's "ABOs" column
-----------------------------------------
The ABOs column is not a reliable join key: a handful of rows carry the
wrong ABO (e.g. an Apollonius Rhodius row tagged with the same ABO as a
Thucydides row -- see the report this script prints). Per HANDOFF.md, the
TLG/PHI/Stoa number embedded in the CTS URN *is* the textgroup/work join
key, so we group by that instead and ignore ABOs entirely for grouping
(it's still carried through informationally into the doc-id crosswalk).

Splitting "Perseus Abbreviation" into author + work
----------------------------------------------------
Each row's abbreviation is a *combined* citation form ("Soph. Aj.", "Hom.
Il.", "Thuc."), not separately-columned author/work abbrevs. We recover
the split per textgroup:

1. Collect every distinct multi-token abbreviation string for the group.
2. The most common leading token is the author abbreviation; strip it off
   each string to get that row's work abbreviation.
3. Any other leading token used >=2 times is kept as an additional author
   abbreviation alias (typos like "LIv." for "Liv." and real alternate
   forms like "Ps. Xen." both fall out of this rule harmlessly).
4. Single-token (glued, no internal space) abbreviations are handled
   separately (see `_classify_single_token`): most are just the bare
   author form used with no specific work; a few are genuinely glued
   author+work ("Cic.Catil.") and are split by prefix-matching the
   author abbrev already established for that group; anything left over
   is logged for manual review rather than guessed at.

Licensing split
----------------
"Perseus Abbreviation" is Perseus's own curated abbreviation (like
kb/seeds/open/hand_curated.json) -> kb/seeds/open/p4_abbrevs.json (CC0).
"Other Abbreviations (LSJ, OCD, etc)" is explicitly sourced from
copyrighted reference works -> kb/seeds/restricted/p4_lsj_ocd.json
(gitignored; never redistributed), per HANDOFF.md's licensing guardrail.

Usage
-----
    uv run python kb/csv_to_p4_seeds.py \\
        "kb/csv/P4 ABOs to CTS-URNs - Works.csv" \\
        --report kb/REPORT_p4_import.md
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

OPEN_SEED_PATH = Path(__file__).parent / "seeds" / "open" / "p4_abbrevs.json"
RESTRICTED_SEED_PATH = Path(__file__).parent / "seeds" / "restricted" / "p4_lsj_ocd.json"
DOC_ID_MAP_PATH = Path(__file__).parent / "data" / "p4_doc_id_map.json"

_URN_RE = re.compile(r"^urn:cts:([^:]+):([^.]+)\.([^.]+)(?:\.(.+))?$")


@dataclass
class Row:
    tg_urn: str
    work_urn: str
    version_urn: str | None
    language: str
    doc_id: str
    author_name: str
    work_name: str
    abbrev: str  # "" if none/blank
    other_abbrevs: list[str]  # [] if none/blank


def _parse_urn(urn: str) -> tuple[str, str, str | None] | None:
    """-> (tg_urn, work_urn, version_urn|None), or None if unparseable."""
    m = _URN_RE.match(urn)
    if not m:
        return None
    corpus, tg, work, version = m.groups()
    tg_urn = f"urn:cts:{corpus}:{tg}"
    work_urn = f"urn:cts:{corpus}:{tg}.{work}"
    version_urn = urn if version else None
    return tg_urn, work_urn, version_urn


def _split_other_abbrevs(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split(";")]
    return [p for p in parts if p and p.lower() != "none"]


def load_rows(csv_path: Path) -> tuple[list[Row], list[dict]]:
    """Returns (rows, skipped) where skipped logs rows with no parseable URN."""
    rows: list[Row] = []
    skipped: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            urn = raw["Original P4 CTS URN"].strip()
            if not urn:
                continue  # blank filler rows in the sheet; nothing to import
            parsed = _parse_urn(urn)
            if parsed is None:
                skipped.append({"urn": urn, "reason": "unparseable URN"})
                continue
            tg_urn, work_urn, version_urn = parsed
            abbrev = raw["Perseus Abbreviation"].strip()
            if abbrev.lower() == "none":
                abbrev = ""
            rows.append(
                Row(
                    tg_urn=tg_urn,
                    work_urn=work_urn,
                    version_urn=version_urn,
                    language=raw["Language"].strip(),
                    doc_id=raw["Perseus text ID"].strip(),
                    author_name=raw["Authors"].strip(),
                    work_name=raw["Work"].strip(),
                    abbrev=abbrev,
                    other_abbrevs=_split_other_abbrevs(
                        raw["Other Abbreviations (LSJ, OCD, etc)"]
                    ),
                )
            )
    return rows, skipped


# --------------------------------------------------------------------------- #
# Author/work abbreviation splitting
# --------------------------------------------------------------------------- #
@dataclass
class SplitResult:
    author_abbrevs: set[str] = field(default_factory=set)
    # work_urn -> set of work-only abbrevs
    work_abbrevs: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # author-only abbrevs (no work binding), e.g. bare "Liv."
    bare_author_abbrevs: set[str] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)


def _split_group(abbrevs_by_work: dict[str, set[str]]) -> SplitResult:
    """abbrevs_by_work: work_urn -> set of raw combined abbreviation strings."""
    result = SplitResult()

    all_strings: set[str] = set()
    for s in abbrevs_by_work.values():
        all_strings |= s

    if len(abbrevs_by_work) == 1:
        # Single-work author: every citation form -- multi-token or not --
        # names the author, not a (author, work) pair, since there's only
        # one work it could mean. Splitting a two-word form here would be
        # wrong (e.g. Apollonius Rhodius's "A. R" is one sigil, not
        # author "A." + work "R"). Register whole strings as bare author
        # abbrevs; the caller binds default_work.
        result.bare_author_abbrevs |= all_strings
        return result

    multi = [s for s in all_strings if len(s.split()) > 1]
    singles = [s for s in all_strings if len(s.split()) == 1]

    first_token_counts = Counter(s.split()[0] for s in multi)
    primary_author = first_token_counts.most_common(1)[0][0] if first_token_counts else None
    if primary_author:
        result.author_abbrevs.add(primary_author)
    for tok, n in first_token_counts.items():
        if n >= 2:
            result.author_abbrevs.add(tok)

    for work_urn, strings in abbrevs_by_work.items():
        for s in strings:
            tokens = s.split()
            if len(tokens) > 1 and tokens[0] in result.author_abbrevs:
                work_abbrev = " ".join(tokens[1:])
                if work_abbrev:
                    result.work_abbrevs[work_urn].add(work_abbrev)

    for s in singles:
        if s in result.author_abbrevs:
            result.bare_author_abbrevs.add(s)
            continue
        matched_prefix = next(
            (a for a in result.author_abbrevs if a != s and s.startswith(a) and len(s) > len(a)),
            None,
        )
        if matched_prefix:
            remainder = s[len(matched_prefix):]
            work_urn = next(
                (w for w, strs in abbrevs_by_work.items() if s in strs), None
            )
            if work_urn:
                result.work_abbrevs[work_urn].add(remainder)
            continue
        if len(singles) == 1:
            # Only one distinct single-token form across the whole group:
            # treat as the bare author abbreviation (e.g. "D.H", "Petr.").
            result.bare_author_abbrevs.add(s)
            continue
        result.unresolved.append(s)

    return result


# --------------------------------------------------------------------------- #
# Seed entry construction
# --------------------------------------------------------------------------- #
def _build_entries(
    rows: list[Row], *, use_other: bool
) -> tuple[list[dict], dict]:
    """Build enrich_gazetteer.py-shaped entries from either the open
    (Perseus Abbreviation) or restricted (Other Abbreviations) column.

    Returns (entries, report) where report carries per-textgroup anomalies
    for the two columns are built with the same logic, called twice.
    """
    by_tg: dict[str, dict[str, str]] = defaultdict(dict)  # tg -> author_name (first seen)
    abbrevs_by_tg_work: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    work_names: dict[str, str] = {}
    author_names_seen: dict[str, set[str]] = defaultdict(set)

    for r in rows:
        author_names_seen[r.tg_urn].add(r.author_name)
        by_tg[r.tg_urn].setdefault("name", r.author_name)
        work_names.setdefault(r.work_urn, r.work_name)
        source_abbrevs = r.other_abbrevs if use_other else ([r.abbrev] if r.abbrev else [])
        for a in source_abbrevs:
            abbrevs_by_tg_work[r.tg_urn][r.work_urn].add(a)

    entries: list[dict] = []
    report = {"mixed_author_textgroups": [], "unresolved_single_tokens": []}

    for tg_urn, names in author_names_seen.items():
        if len(names) > 1:
            report["mixed_author_textgroups"].append(
                {"tg_urn": tg_urn, "authors": sorted(names)}
            )

    for tg_urn, abbrevs_by_work in abbrevs_by_tg_work.items():
        if not any(abbrevs_by_work.values()):
            continue
        split = _split_group(abbrevs_by_work)
        author_name = by_tg[tg_urn]["name"]

        if split.unresolved:
            report["unresolved_single_tokens"].append(
                {"tg_urn": tg_urn, "author": author_name, "tokens": sorted(split.unresolved)}
            )

        single_work = len(abbrevs_by_work) == 1
        only_work_urn = next(iter(abbrevs_by_work)) if single_work else None

        emitted_author_only = False
        for author_abbrev in sorted(split.author_abbrevs | split.bare_author_abbrevs):
            entry: dict = {
                "tg_urn": tg_urn,
                "author_abbrev": author_abbrev,
                "author_name": author_name,
            }
            if single_work and only_work_urn and only_work_urn in abbrevs_by_work:
                # Single-work author: let the linker default to this work
                # when only the author is cited (e.g. bare "Thuc.").
                work_abbrevs = split.work_abbrevs.get(only_work_urn) or {author_abbrev}
                default_abbrev = sorted(work_abbrevs)[0]
                entry["work_urn"] = only_work_urn
                entry["work_abbrev"] = default_abbrev
                entry["work_name"] = work_names.get(only_work_urn, "")
                entry["default_work"] = default_abbrev
            entries.append(entry)
            emitted_author_only = True

        if not emitted_author_only:
            continue

        for work_urn, work_abbrevs in split.work_abbrevs.items():
            for work_abbrev in sorted(work_abbrevs):
                if single_work and work_urn == only_work_urn:
                    continue  # already emitted above with default_work
                entries.append(
                    {
                        "tg_urn": tg_urn,
                        "author_abbrev": sorted(split.author_abbrevs)[0]
                        if split.author_abbrevs
                        else sorted(split.bare_author_abbrevs)[0],
                        "author_name": author_name,
                        "work_urn": work_urn,
                        "work_abbrev": work_abbrev,
                        "work_name": work_names.get(work_urn, ""),
                    }
                )

    return entries, report


def build_doc_id_map(rows: list[Row]) -> dict:
    doc_map: dict[str, dict] = {}
    for r in rows:
        if not r.doc_id:
            continue
        key = r.version_urn or r.work_urn
        doc_map[r.doc_id] = {
            "urn": key,
            "tg_urn": r.tg_urn,
            "work_urn": r.work_urn,
            "language": r.language,
        }
    return doc_map


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv_path", type=Path)
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--open-output", type=Path, default=OPEN_SEED_PATH)
    p.add_argument("--restricted-output", type=Path, default=RESTRICTED_SEED_PATH)
    p.add_argument("--doc-id-output", type=Path, default=DOC_ID_MAP_PATH)
    args = p.parse_args(argv)

    rows, skipped = load_rows(args.csv_path)
    print(f"Loaded {len(rows)} rows ({len(skipped)} skipped: no parseable URN)", file=sys.stderr)

    open_entries, open_report = _build_entries(rows, use_other=False)
    restricted_entries, restricted_report = _build_entries(rows, use_other=True)
    doc_id_map = build_doc_id_map(rows)

    args.open_output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.open_output, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "source": "p4_abos",
                "license": "CC0",
                "note": (
                    "Perseus 4's own curated author/work abbreviations, from "
                    "Alison Babeu's P4-ABO-to-CTS-URN spreadsheet "
                    "(kb/csv/P4 ABOs to CTS-URNs - Works.csv). Not derived "
                    "from any copyrighted reference work."
                ),
                "entries": open_entries,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Wrote {len(open_entries)} open entries -> {args.open_output}", file=sys.stderr)

    args.restricted_output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.restricted_output, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "source": "p4_lsj_ocd",
                "license": "RESTRICTED: LSJ/OCD and other copyrighted reference works",
                "note": (
                    "'Other Abbreviations (LSJ, OCD, etc)' column of the P4 "
                    "ABO spreadsheet. MUST NOT be committed or redistributed."
                ),
                "entries": restricted_entries,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print(
        f"Wrote {len(restricted_entries)} restricted entries -> {args.restricted_output}",
        file=sys.stderr,
    )

    args.doc_id_output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.doc_id_output, "w", encoding="utf-8") as fh:
        json.dump(doc_id_map, fh, ensure_ascii=False, indent=2)
    print(f"Wrote {len(doc_id_map)} doc-id crosswalk entries -> {args.doc_id_output}", file=sys.stderr)

    if args.report:
        lines = ["# P4 ABO import report\n"]
        lines.append(f"- Rows loaded: {len(rows)}")
        lines.append(f"- Rows skipped (no parseable URN): {len(skipped)}")
        lines.append(f"- Open (Perseus Abbreviation) entries: {len(open_entries)}")
        lines.append(f"- Restricted (LSJ/OCD/etc) entries: {len(restricted_entries)}")
        lines.append(f"- Doc-id crosswalk entries: {len(doc_id_map)}\n")

        for label, rep in (("Perseus Abbreviation", open_report), ("Other Abbreviations", restricted_report)):
            lines.append(f"\n## {label} column\n")
            mixed = rep["mixed_author_textgroups"]
            lines.append(f"### Mixed-author textgroups ({len(mixed)})\n")
            lines.append(
                "Rows sharing a textgroup URN but disagreeing on Authors -- "
                "likely a data-entry error in the sheet; not used for grouping, "
                "but worth fixing at the source.\n"
            )
            for m in mixed:
                lines.append(f"- `{m['tg_urn']}`: {', '.join(m['authors'])}")

            unresolved = rep["unresolved_single_tokens"]
            lines.append(f"\n### Unresolved glued abbreviations ({len(unresolved)})\n")
            lines.append(
                "Single-token abbreviations that don't match this textgroup's "
                "established author prefix and aren't the sole abbreviation form "
                "for the group -- not auto-registered; needs a human decision.\n"
            )
            for u in unresolved:
                lines.append(f"- `{u['tg_urn']}` ({u['author']}): {', '.join(u['tokens'])}")

        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote report -> {args.report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
