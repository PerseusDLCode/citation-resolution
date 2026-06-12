#!/usr/bin/env python3
"""
cts_harvester.py
================

Walk local clones of PerseusDL/canonical-greekLit and canonical-latinLit,
extract textgroup/work URNs and canonical titles from __cts__.xml files, and
derive citation schemes from CTS-compliant editions' refsDecl/cRefPattern.

Emits kb/data/cts.skeleton.json in the Gazetteer.from_dict() shape:

  {
    "urn:cts:greekLit:tlg0012": {
      "name_abbrevs": [],          # empty — abbreviations come from HuCit
      "default_work": null,
      "works": {
        "urn:cts:greekLit:tlg0012.tlg001": {
          "title_abbrevs": [],     # empty — abbreviations come from HuCit
          "scheme": "book.line"
        }
      }
    }
  }

Scheme derivation
-----------------
The CTS refsDecl carries one <cRefPattern n="LEVEL_NAME"> per citation level,
from most-specific to least-specific.  Sorting by ascending group count in
matchPattern recovers [coarsest, ..., finest], which we join with "." to get
the scheme string.

Stephanus detection
-------------------
For Plato and other Stephanus-paginated works the deepest-level @n values in
the actual XML look like "327a", "328b", etc. (digits + trailing letter a-e).
We scan the first 30 innermost <div @n> values; if the majority match that
pattern we label the scheme "stephanus" instead of the raw level names.

CTS compliance filtering
------------------------
We consult the repo's *.tracking.json first.  If that file is absent or
unparseable (the Latin tracking.json has a known JSON error), we fall back to
checking whether the edition file itself contains <refsDecl n="CTS">.

Usage
-----
    python kb/cts_harvester.py \
        --greek  ~/code/PerseusDL/canonical-greekLit \
        --latin  ~/code/PerseusDL/canonical-latinLit \
        --output kb/data/cts.skeleton.json
    python kb/cts_harvester.py ...  --verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _utils import _has_cts_refsDecl_file, _load_tracking  # noqa: E402

# --------------------------------------------------------------------------- #
# XML namespace constants
# --------------------------------------------------------------------------- #
CTS_NS = "http://chs.harvard.edu/xmlns/cts"
TEI_NS = "http://www.tei-c.org/ns/1.0"
CTS_TG = f"{{{CTS_NS}}}textgroup"
CTS_WORK = f"{{{CTS_NS}}}work"
CTS_GROUPNAME = f"{{{CTS_NS}}}groupname"
CTS_TITLE = f"{{{CTS_NS}}}title"
TEI_REFS_DECL = f"{{{TEI_NS}}}refsDecl"
TEI_CREF = f"{{{TEI_NS}}}cRefPattern"
TEI_DIV = f"{{{TEI_NS}}}div"
TEI_MILESTONE = f"{{{TEI_NS}}}milestone"


def _has_cts_refsDecl_tracking(tracking: dict, version_urn: str) -> Optional[bool]:
    """Return True/False from tracking if the URN is present, else None."""
    rec = tracking.get(version_urn)
    if rec is None:
        return None
    return bool(rec.get("has_cts_refsDecl"))


# --------------------------------------------------------------------------- #
# refsDecl / scheme parsing
# --------------------------------------------------------------------------- #
def _parse_scheme(xml_path: Path) -> Optional[str]:
    """
    Parse the CTS refsDecl from an edition XML and return a scheme string.
    Returns None if no CTS refsDecl is found.
    """
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        print(f"  warning: XML parse error in {xml_path.name}: {exc}", file=sys.stderr)
        return None

    root = tree.getroot()

    # Find <refsDecl n="CTS">
    refs_decl = None
    for rd in root.iter(TEI_REFS_DECL):
        if rd.get("n") == "CTS":
            refs_decl = rd
            break
    if refs_decl is None:
        return None

    patterns = refs_decl.findall(TEI_CREF)
    if not patterns:
        return None

    # Sort from coarsest (fewest groups) to finest (most groups)
    def group_count(el: ET.Element) -> int:
        mp = el.get("matchPattern", "")
        # Count parenthesised groups: each (\w+) or similar group = 1 level
        return mp.count("(")

    sorted_pats = sorted(patterns, key=group_count)
    level_names = [el.get("n", "") for el in sorted_pats]

    if not level_names:
        return None

    scheme = ".".join(level_names)

    # Stephanus detection: sample innermost div @n values from the document
    if _is_stephanus(root):
        scheme = "stephanus"

    return scheme


def _is_stephanus(root: ET.Element) -> bool:
    """
    Return True if the document uses Stephanus pagination.

    Reliable signal: <milestone unit="section" resp="Stephanus"> elements,
    which Perseus uses to mark the a/b/c/d/e subsections within each
    Stephanus page (the page itself is a <div @n="327">).
    """
    for ms in root.iter(TEI_MILESTONE):
        if (
            ms.get("resp", "").lower() == "stephanus"
            and ms.get("unit", "") in ("section", "page")
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Core harvester
# --------------------------------------------------------------------------- #
def harvest_repo(repo: Path, tracking: dict) -> dict:
    """
    Walk one canonical-*Lit repo and return a partial gazetteer dict.
    """
    result: dict = {}
    data_dir = repo / "data"
    if not data_dir.is_dir():
        print(f"  warning: no data/ directory in {repo}", file=sys.stderr)
        return result

    tg_dirs = sorted(d for d in data_dir.iterdir() if d.is_dir())
    for tg_dir in tg_dirs:
        tg_cts = tg_dir / "__cts__.xml"
        if not tg_cts.exists():
            continue

        try:
            tg_tree = ET.parse(tg_cts)
        except ET.ParseError as exc:
            print(f"  warning: {tg_cts}: {exc}", file=sys.stderr)
            continue

        tg_root = tg_tree.getroot()
        tg_urn = tg_root.get("urn")
        if not tg_urn:
            continue

        # Collect all groupname values; prefer the English one as canonical_name
        groupnames = tg_root.findall(f"{{{CTS_NS}}}groupname")
        canonical_name = next(
            (e.text for e in groupnames if e.get("{http://www.w3.org/XML/1998/namespace}lang") == "eng"),
            next((e.text for e in groupnames if e.text), None),
        )

        result[tg_urn] = {
            "canonical_name": canonical_name or "",
            "name_abbrevs": [],
            "default_work": None,
            "works": {},
        }

        work_dirs = sorted(d for d in tg_dir.iterdir() if d.is_dir())
        for work_dir in work_dirs:
            work_cts = work_dir / "__cts__.xml"
            if not work_cts.exists():
                continue

            try:
                w_tree = ET.parse(work_cts)
            except ET.ParseError as exc:
                print(f"  warning: {work_cts}: {exc}", file=sys.stderr)
                continue

            w_root = w_tree.getroot()
            work_urn = w_root.get("urn")
            if not work_urn:
                continue

            work_titles = w_root.findall(f"{{{CTS_NS}}}title")
            canonical_title = next(
                (e.text for e in work_titles if e.get("{http://www.w3.org/XML/1998/namespace}lang") == "eng"),
                next((e.text for e in work_titles if e.text), None),
            )

            # Find the best scheme for this work by scanning edition files
            scheme = _pick_scheme(work_dir, work_urn, tracking)

            result[tg_urn]["works"][work_urn] = {
                "canonical_title": canonical_title or "",
                "title_abbrevs": [],
                "scheme": scheme or "flat",
            }

    return result


def _pick_scheme(work_dir: Path, work_urn: str, tracking: dict) -> Optional[str]:
    """
    Choose the citation scheme for a work by reading the first CTS-compliant
    edition file.  Preference order: has_cts_refsDecl from tracking > in-file
    detection.  Skip files that end in 'grc1' or 'lat1' (commonly P4-era,
    often unreliable schemes) unless they're the only option.
    """
    xml_files = sorted(
        f for f in work_dir.glob("*.xml") if f.name != "__cts__.xml"
    )
    if not xml_files:
        return None

    def score(path: Path) -> tuple[int, int]:
        # Version URN for this file = work_urn + "." + stem suffix
        stem = path.stem  # e.g. "tlg0012.tlg001.perseus-grc2"
        parts = stem.rsplit(".", 1)
        version_suffix = parts[-1] if len(parts) > 1 else ""
        version_urn = f"{work_urn}.{version_suffix}"

        tracking_result = _has_cts_refsDecl_tracking(tracking, version_urn)

        if tracking_result is True:
            cts_ok = 2  # confirmed by tracking
        elif tracking_result is False:
            cts_ok = 0  # explicitly excluded
        else:
            # Unknown to tracking: check in-file
            cts_ok = 1 if _has_cts_refsDecl_file(path) else 0

        # Penalise P4-era files (version suffix ends with "1")
        p4_penalty = 0 if not version_suffix.endswith("1") else -1

        return (cts_ok, p4_penalty)

    # Sort descending: highest score first
    ranked = sorted(xml_files, key=score, reverse=True)
    for candidate in ranked:
        s, p = score(candidate)
        if s == 0:
            break  # remaining files also have no CTS refsDecl
        scheme = _parse_scheme(candidate)
        if scheme:
            return scheme

    return None


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def verify(skeleton: dict) -> bool:
    ok = True

    checks = [
        ("urn:cts:greekLit:tlg0012", "urn:cts:greekLit:tlg0012.tlg001", "book.line", "Iliad"),
        ("urn:cts:greekLit:tlg0003", "urn:cts:greekLit:tlg0003.tlg001", "book.chapter.section", "Thucydides"),
        ("urn:cts:greekLit:tlg0059", "urn:cts:greekLit:tlg0059.tlg030", "stephanus", "Plato Republic"),
    ]

    for tg_urn, work_urn, expected_scheme, label in checks:
        tg = skeleton.get(tg_urn)
        if tg is None:
            print(f"FAIL: textgroup {tg_urn} ({label}) not found", file=sys.stderr)
            ok = False
            continue
        w = tg["works"].get(work_urn)
        if w is None:
            print(f"FAIL: work {work_urn} ({label}) not found", file=sys.stderr)
            ok = False
            continue
        if w["scheme"] != expected_scheme:
            print(
                f"FAIL: {label} scheme = {w['scheme']!r}, expected {expected_scheme!r}",
                file=sys.stderr,
            )
            ok = False
        else:
            print(f"OK  : {label} scheme = {w['scheme']!r}", file=sys.stderr)

    # Smoke-test Gazetteer round-trip
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from citation_resolution.tei_cts_linker import Gazetteer  # type: ignore

        Gazetteer.from_dict(skeleton)
        print("OK  : Gazetteer.from_dict() round-trip succeeded", file=sys.stderr)
    except Exception as exc:
        print(f"FAIL: Gazetteer.from_dict() raised {exc}", file=sys.stderr)
        ok = False

    if ok:
        print("verify: all checks passed", file=sys.stderr)
    return ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Harvest CTS URNs and citation schemes from canonical-*Lit repos."
    )
    p.add_argument(
        "--greek",
        default=str(Path.home() / "code/PerseusDL/canonical-greekLit"),
        metavar="PATH",
        help="path to canonical-greekLit clone (default: ~/code/PerseusDL/canonical-greekLit)",
    )
    p.add_argument(
        "--latin",
        default=str(Path.home() / "code/PerseusCode/canonical-latinLit"),
        metavar="PATH",
        help="path to canonical-latinLit clone (default: ~/code/PerseusDL/canonical-latinLit)",
    )
    p.add_argument(
        "--output",
        default="kb/data/cts.skeleton.json",
        metavar="PATH",
        help="output JSON path (default: kb/data/cts.skeleton.json)",
    )
    p.add_argument(
        "--verify", action="store_true", help="run spot-checks after writing"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    skeleton: dict = {}

    for label, repo_path in [("greekLit", args.greek), ("latinLit", args.latin)]:
        repo = Path(repo_path)
        if not repo.exists():
            print(f"  warning: {label} repo not found at {repo}", file=sys.stderr)
            continue
        print(f"\nHarvesting {label} from {repo} …", file=sys.stderr)
        tracking = _load_tracking(repo)
        partial = harvest_repo(repo, tracking)
        tg_count = len(partial)
        work_count = sum(len(r["works"]) for r in partial.values())
        scheme_count = sum(
            1 for r in partial.values()
            for w in r["works"].values()
            if w["scheme"] != "flat"
        )
        print(
            f"  {tg_count:,} textgroups, {work_count:,} works, "
            f"{scheme_count:,} with a real scheme",
            file=sys.stderr,
        )
        skeleton.update(partial)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(skeleton, fh, ensure_ascii=False, indent=2)

    total_works = sum(len(r["works"]) for r in skeleton.values())
    print(
        f"\nWrote {out_path}: {len(skeleton):,} textgroups, {total_works:,} works",
        file=sys.stderr,
    )

    if args.verify:
        ok = verify(skeleton)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
