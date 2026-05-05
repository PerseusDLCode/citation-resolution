import json

from pathlib import Path

ABBREVIATIONS_FILE = (
    Path(__file__).resolve().parent / "static" / "json" / "ocd_abbreviations.json"
)

with open(ABBREVIATIONS_FILE) as f:
    ABBREVIATIONS = json.load(f)


INVERTED_URNS_FILE = (
    Path(__file__).resolve().parent / "static" / "json" / "inverted_urn.json"
)

with open(INVERTED_URNS_FILE) as f:
    INVERTED_URNS = json.load(f)


def get_full(abbreviation: str):
    maybe_full = [abbr for abbr in ABBREVIATIONS if abbr["abbrev"] == abbreviation]

    if len(maybe_full) > 0:
        return maybe_full[0]["full"]

    if abbreviation.startswith("Aristoph"):
        return "Aristophanes"
    elif abbreviation.startswith("Aristot"):
        return "Aristotle"

    print(f"Full name not found for abbreviation {abbreviation}")

    return abbreviation


def resolve(citation: str):
    """
    Citations appear to follow a simple structure:
    splitting on whitespace, the 0th element is always
    the abbreviation of the author's name; the -1th
    element is always the location; and the element(s)
    in between always constitute(s) the work abbreviation.
    """
    parts = citation.split(" ")

    author_abbreviation = parts[0]
    location = parts[-1]
    work_abbreviation = " ".join(parts[1:-1])

    author_full = get_full(author_abbreviation)

    work_full = None
    if work_abbreviation:
        work_full = get_full(work_abbreviation)

    author_works_dict = INVERTED_URNS.get(author_full.lower())

    if author_works_dict is None:
        print(f"No works found for author {author_full}")

    if work_full is None:
        return f"{author_full} {location}"

    cts_urn = author_works_dict.get(work_full.lower())

    if cts_urn is None:
        cts_urn = [
            k for k, _v in author_works_dict.items() if k.startswith(work_full.lower())
        ]

        if len(cts_urn) > 0:
            cts_urn = cts_urn[0]
        else:
            cts_urn = None

    if cts_urn is None:
        print(f"No URN found for {work_full}")

        return f"{author_full} {work_full} {location}"

    return f"{cts_urn}:{location}"


def main():
    print(resolve("Plut. Cat. Mi. 66.4"))
