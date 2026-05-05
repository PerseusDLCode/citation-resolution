import csv
import json

from pathlib import Path

from lxml import etree

NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def find_xml_files(dir: Path):
    return [f for f in dir.glob("./**/*.xml") if f.name != "__cts__.xml"]


def parse_file(f: Path):
    return etree.parse(f)


def main():
    GREEK_LIT = (
        Path(__file__).resolve().parent.parent.parent
        / "PerseusDL"
        / "canonical-greekLit"
        / "data"
    )

    files = find_xml_files(GREEK_LIT)

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

    with open("citation_map.json", "w") as f:
        json.dump(citation_map, f)

    with open("citation_counts.csv", "w") as f:
        fieldnames = ["filename", "n_bibls", "n_cits"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in citation_counts:
            writer.writerow(row)


if __name__ == "__main__":
    main()
