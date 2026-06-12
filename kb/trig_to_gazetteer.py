#!/usr/bin/env python3
"""
trig_to_gazetteer.py
====================

Convert the exported HuCit RDF dataset (TriG format) to the flat JSON
gazetteer consumed by Gazetteer.from_dict() in tei_cts_linker.py.

Graph model (CIDOC-CRM + FRBRoo / hucitlib conventions):

  F10_Person  --P1_is_identified_by--> E42_Identifier (type CTS_URN) [rdfs:label = textgroup URN]
              --P1_is_identified_by--> F12_Name
                                         --P139_has_alternative_form--> E41_Appellation [rdfs:label = "Hom."]
              --P14i_performed-->      F27_Work_Conception
                                         --R16_initiated-->             F1_Work

  F1_Work     --P1_is_identified_by--> E42_Identifier (type CTS_URN) [rdfs:label = work URN]
              --P102_has_title-->       E35_Title
                                         --P139_has_alternative_form--> E41_Appellation [rdfs:label = "Il."]

Output shape (matches Gazetteer.from_dict contract):

  {
    "urn:cts:greekLit:tlg0012": {
      "name_abbrevs": ["Hom.", "Hom"],
      "default_work": null,
      "works": {
        "urn:cts:greekLit:tlg0012.tlg001": {
          "title_abbrevs": ["Il."],
          "scheme": "flat"          # placeholder; filled in by merge_gazetteer.py
        }
      }
    }
  }

Usage:
    python kb/trig_to_gazetteer.py hucit.trig kb/data/hucit.gazetteer.json
    python kb/trig_to_gazetteer.py --verify  hucit.trig kb/data/hucit.gazetteer.json
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

# Suppress rdflib ConjunctiveGraph deprecation warning — we use Dataset below
# but the TriG parser still triggers it internally.
warnings.filterwarnings("ignore", category=DeprecationWarning, module="rdflib")

try:
    from rdflib import Dataset, Graph, Literal, Node, URIRef
    from rdflib.namespace import RDF, RDFS
except ImportError:
    sys.exit("rdflib not found; run: pip install rdflib")


# --------------------------------------------------------------------------- #
# Namespace constants (discovered from profiling; do not hardcode string URIs
# outside this block)
# --------------------------------------------------------------------------- #
ECRM = "http://erlangen-crm.org/current/"
EFRBROO = "http://erlangen-crm.org/efrbroo/"
HUCIT = "http://purl.org/net/hucit#"

P1_IS_IDENTIFIED_BY = URIRef(ECRM + "P1_is_identified_by")
P2_HAS_TYPE = URIRef(ECRM + "P2_has_type")
P139_HAS_ALT_FORM = URIRef(ECRM + "P139_has_alternative_form")
P14I_PERFORMED = URIRef(EFRBROO + "P14i_performed")
P102_HAS_TITLE = URIRef(EFRBROO + "P102_has_title")
R16_INITIATED = URIRef(EFRBROO + "R16_initiated")

F10_PERSON = URIRef(EFRBROO + "F10_Person")
F12_NAME = URIRef(EFRBROO + "F12_Name")
F1_WORK = URIRef(EFRBROO + "F1_Work")
F27_WORK_CONCEPTION = URIRef(EFRBROO + "F27_Work_Conception")
E35_TITLE = URIRef(EFRBROO + "E35_Title")
E41_APPELLATION = URIRef(ECRM + "E41_Appellation")
E42_IDENTIFIER = URIRef(ECRM + "E42_Identifier")

CTS_URN_TYPE = URIRef("http://purl.org/hucit/kb/types/CTS_URN")

CTS_PREFIX = "urn:cts:"


# --------------------------------------------------------------------------- #
# Graph loading
# --------------------------------------------------------------------------- #
def load_graph(trig_path: str) -> Graph:
    """Parse the TriG file and merge all named graphs into a single Graph."""
    print(f"Loading {trig_path} …", file=sys.stderr)
    ds = Dataset()
    ds.parse(trig_path, format="trig")
    # Dataset.subjects() only queries the default graph, so we merge all named
    # graphs into a plain Graph for straightforward s/p/o iteration.
    merged = Graph()
    quad_count = 0
    for s, p, o, _ in ds.quads():
        merged.add((s, p, o))
        quad_count += 1
    print(f"  {quad_count:,} quads merged into single graph", file=sys.stderr)
    return merged


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #
def _labels(g: Graph, node: URIRef | Node) -> list[str]:
    """All rdfs:label string values for a node (any language, plain or typed)."""
    return [str(o) for o in g.objects(node, RDFS.label) if isinstance(o, Literal)]


def _cts_urn_for(g: Graph, entity: Node) -> str | None:
    """Return the CTS URN string for an author or work node, or None."""
    for id_node in g.objects(entity, P1_IS_IDENTIFIED_BY):
        if (id_node, P2_HAS_TYPE, CTS_URN_TYPE) in g:
            labels = _labels(g, id_node)
            for lbl in labels:
                if lbl.startswith(CTS_PREFIX):
                    return lbl
    return None


def _name_abbrevs_for(g: Graph, author: Node) -> list[str]:
    """Abbreviations for an author: F12_Name → P139_has_alternative_form → E41_Appellation → label."""
    abbrevs: list[str] = []
    for id_node in g.objects(author, P1_IS_IDENTIFIED_BY):
        if (id_node, RDF.type, F12_NAME) in g:
            for abbr_node in g.objects(id_node, P139_HAS_ALT_FORM):
                if (abbr_node, RDF.type, E41_APPELLATION) in g:
                    abbrevs.extend(_labels(g, abbr_node))
    return abbrevs


def _title_abbrevs_for(g: Graph, work: URIRef) -> list[str]:
    """Abbreviations for a work: P102_has_title → E35_Title → P139_has_alternative_form → E41_Appellation → label."""
    abbrevs: list[str] = []
    for title_node in g.objects(work, P102_HAS_TITLE):
        if (title_node, RDF.type, E35_TITLE) in g:
            for abbr_node in g.objects(title_node, P139_HAS_ALT_FORM):
                if (abbr_node, RDF.type, E41_APPELLATION) in g:
                    abbrevs.extend(_labels(g, abbr_node))
    return abbrevs


def _works_for_author(g: Graph, author: Node) -> list[URIRef]:
    """All F1_Work nodes attributed to this author via F27_Work_Conception."""
    works: list[URIRef] = []
    for event in g.objects(author, P14I_PERFORMED):
        if (event, RDF.type, F27_WORK_CONCEPTION) in g:
            for work in g.objects(event, R16_INITIATED):
                if isinstance(work, URIRef) and (work, RDF.type, F1_WORK) in g:
                    works.append(work)
    return works


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #
def build_gazetteer(g: Graph) -> dict:
    """Walk all F10_Person nodes and build the gazetteer dict."""
    gazetteer: dict = {}
    authors_seen = 0
    authors_no_urn = 0
    works_seen = 0
    works_no_urn = 0

    all_authors = list(g.subjects(RDF.type, F10_PERSON))
    print(f"  {len(all_authors):,} F10_Person nodes found", file=sys.stderr)

    for author in all_authors:
        tg_urn = _cts_urn_for(g, author)
        if tg_urn is None:
            authors_no_urn += 1
            continue

        # Deduplicate: if two named graphs describe the same author, the URN is
        # shared and we'll encounter the same tg_urn twice. Merge abbreviations.
        if tg_urn not in gazetteer:
            gazetteer[tg_urn] = {
                "name_abbrevs": [],
                "default_work": None,
                "works": {},
            }

        rec = gazetteer[tg_urn]
        for abbr in _name_abbrevs_for(g, author):
            if abbr not in rec["name_abbrevs"]:
                rec["name_abbrevs"].append(abbr)

        for work in _works_for_author(g, author):
            works_seen += 1
            w_urn = _cts_urn_for(g, work)
            if w_urn is None:
                works_no_urn += 1
                continue

            if w_urn not in rec["works"]:
                rec["works"][w_urn] = {
                    "title_abbrevs": [],
                    "scheme": "flat",  # placeholder; overridden by merge_gazetteer.py
                }

            w_rec = rec["works"][w_urn]
            for abbr in _title_abbrevs_for(g, work):
                if abbr not in w_rec["title_abbrevs"]:
                    w_rec["title_abbrevs"].append(abbr)

        authors_seen += 1

    print(
        f"  authors: {authors_seen:,} with URN, {authors_no_urn} without",
        file=sys.stderr,
    )
    print(
        f"  works:   {works_seen:,} seen, {works_no_urn} without URN",
        file=sys.stderr,
    )
    return gazetteer


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def verify(gazetteer: dict) -> bool:
    """Spot-check: Homer resolves with Hom. and has Il. / Od."""
    ok = True
    hom = gazetteer.get("urn:cts:greekLit:tlg0012")
    if hom is None:
        print("FAIL: Homer textgroup not found", file=sys.stderr)
        return False

    if "Hom." not in hom["name_abbrevs"]:
        print(
            f"FAIL: 'Hom.' not in name_abbrevs: {hom['name_abbrevs']}", file=sys.stderr
        )
        ok = False

    iliad_urn = "urn:cts:greekLit:tlg0012.tlg001"
    if iliad_urn not in hom["works"]:
        print(f"FAIL: Iliad URN {iliad_urn} not found under Homer", file=sys.stderr)
        ok = False
    elif "Il." not in hom["works"][iliad_urn]["title_abbrevs"]:
        print(
            f"FAIL: 'Il.' not in Iliad title_abbrevs: {hom['works'][iliad_urn]['title_abbrevs']}",
            file=sys.stderr,
        )
        ok = False

    od_urn = "urn:cts:greekLit:tlg0012.tlg002"
    if od_urn not in hom["works"]:
        print(f"FAIL: Odyssey URN {od_urn} not found under Homer", file=sys.stderr)
        ok = False
    elif "Od." not in hom["works"][od_urn]["title_abbrevs"]:
        print(
            f"FAIL: 'Od.' not in Odyssey title_abbrevs: {hom['works'][od_urn]['title_abbrevs']}",
            file=sys.stderr,
        )
        ok = False

    # Also smoke-test Gazetteer.from_dict round-trip if tei_cts_linker is importable.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from citation_resolution.tei_cts_linker import Gazetteer

        gaz = Gazetteer.from_dict(gazetteer)
        authors = gaz.author_for("Hom.")
        if not authors:
            print(
                "FAIL: Gazetteer.author_for('Hom.') returned nothing", file=sys.stderr
            )
            ok = False
        else:
            work = gaz.work_for(authors[0], "Il.")
            if work is None:
                print(
                    "FAIL: Gazetteer.work_for(Homer, 'Il.') returned None",
                    file=sys.stderr,
                )
                ok = False
            else:
                print(f"OK  : Hom. Il. -> {work.work_urn}", file=sys.stderr)
    except ImportError:
        print(
            "  (skipping Gazetteer round-trip: tei_cts_linker not importable)",
            file=sys.stderr,
        )

    if ok:
        print("verify: all checks passed", file=sys.stderr)
    return ok


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Convert HuCit TriG export to gazetteer JSON for tei_cts_linker."
    )
    p.add_argument("trig", help="path to hucit.trig (or equivalent TriG export)")
    p.add_argument(
        "output", help="output JSON path (e.g. kb/data/hucit.gazetteer.json)"
    )
    p.add_argument(
        "--verify", action="store_true", help="run spot-checks after writing"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    g = load_graph(args.trig)
    gazetteer = build_gazetteer(g)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(gazetteer, fh, ensure_ascii=False, indent=2)

    author_count = len(gazetteer)
    work_count = sum(len(rec["works"]) for rec in gazetteer.values())
    print(
        f"\nWrote {out_path}: {author_count:,} authors, {work_count:,} works",
        file=sys.stderr,
    )

    if args.verify:
        ok = verify(gazetteer)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
