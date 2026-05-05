import csv
import json

from pathlib import Path

from lxml import etree

NS = {"tei": "http://www.tei-c.org/ns/1.0"}

GREEK_LIT = (
    Path(__file__).resolve().parent.parent.parent
    / "PerseusDL"
    / "canonical-greekLit"
    / "data"
)

LATIN_LIT = (
    Path(__file__).resolve().parent.parent.parent
    / "PerseusDL"
    / "canonical-latinLit"
    / "data"
)


def find_xml_files(dir: Path):
    return [f for f in dir.glob("./**/*.xml") if f.name != "__cts__.xml"]


def parse_file(f: Path):
    return etree.parse(f)


def find_citations(corpus, prefix):
    files = find_xml_files(corpus)

    citation_counts = []
    citation_map = {}

    for f in files:
        tree = parse_file(f)

        citation_map[f.name] = {}

        bibls = [
            (
                el.get("n"),
                " ".join(
                    str(etree.tostring(el, method="xml", encoding="unicode")).split()
                ),
            )
            for el in tree.findall(".//tei:bibl", namespaces=NS)
        ]
        cits = [
            " ".join(str(etree.tostring(el, method="xml", encoding="unicode")).split())
            for el in tree.findall(".//tei:cit", namespaces=NS)
        ]
        citation_map[f.name]["bibls"] = bibls
        citation_map[f.name]["cits"] = cits
        citation_counts.append(
            {"filename": f.name, "n_bibls": len(bibls), "n_cits": len(cits)}
        )

    with open(f"{prefix}-citation_map.json", "w") as f:
        json.dump(citation_map, f)

    with open(f"{prefix}-citation_counts.csv", "w") as f:
        fieldnames = ["filename", "n_bibls", "n_cits"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in citation_counts:
            writer.writerow(row)


def main():
    find_citations(GREEK_LIT, "greek")
    find_citations(LATIN_LIT, "latin")


if __name__ == "__main__":
    main()
