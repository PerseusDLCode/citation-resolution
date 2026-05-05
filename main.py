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

URN_PATH = (
    Path(__file__).resolve().parent
    / "src"
    / "citation_resolution"
    / "static"
    / "json"
    / "urn.json"
)
INVERTED_URN_PATH = (
    Path(__file__).resolve().parent
    / "src"
    / "citation_resolution"
    / "static"
    / "json"
    / "inverted_urn.json"
)


def find_xml_files(dir: Path):
    return [f for f in dir.glob("./**/*.xml") if f.name != "__cts__.xml"]


def find_citations(corpus, prefix):
    files = find_xml_files(corpus)

    citation_counts = []
    citation_map = {}

    for f in files:
        tree = etree.parse(f)

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


def invert_urns():
    with open(URN_PATH) as f:
        urns_to_textgroup_works = json.load(f)

    inverted_urns = {}

    for k, v in urns_to_textgroup_works.items():
        author = v["author"].lower()
        title = v["title"].lower()

        textgroup = inverted_urns.get(author)

        if textgroup is None:
            inverted_urns[author] = {}
            textgroup = inverted_urns[author]

        work = textgroup.get(title)

        if work is None:
            textgroup[title] = ".".join(k.split(".")[0:-1])

    with open(INVERTED_URN_PATH, "w") as f:
        json.dump(inverted_urns, f)


def main():
    print(resolve("Dem. 19.326"))


if __name__ == "__main__":
    main()
