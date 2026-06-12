# Citation Resolution

This repository tracks `<bibl>` and `<cit>` tags in (currently) the
[canonical-greekLit](https://github.com/perseusdl/canonical-greeklit)
and [canonical-latinLit](https://github.com/perseusdl/canonical-latinlit)
repositories of the Perseus Digital Library.

The goal is to resolve these citations as hyperlinks between works.

## `tei_cts_linker` — resolve `<bibl>` / `<cit>` to CTS URNs

Walks a TEI document, matches `<bibl>` and `<cit>` text against the
knowledge base, and writes CTS URNs back into the tree (`@ref` on `<bibl>`,
`@source` on `<cit>`). References that can't be resolved are flagged with a
`nel:status="review"` attribute rather than silently dropped.

### CLI

```sh
# Resolve a file using the default gazetteer (kb/data/gazetteer.json)
uv run python -m citation_resolution.tei_cts_linker input.xml -o output.xml

# Write a review report alongside the output
uv run python -m citation_resolution.tei_cts_linker input.xml \
    -o output.xml \
    --report report.json

# Use a custom gazetteer
uv run python -m citation_resolution.tei_cts_linker input.xml \
    --gazetteer kb/data/hucit.gazetteer.json \
    -o output.xml

# Split flat <bibl> interiors into <author>/<title>/<citedRange>
uv run python -m citation_resolution.tei_cts_linker input.xml \
    --decompose -o output.xml

# Treat commas as citation-level separators (continental style: "11,4,11")
uv run python -m citation_resolution.tei_cts_linker input.xml \
    --comma level -o output.xml
```

### Library

```python
from lxml import etree
from citation_resolution.tei_cts_linker import Gazetteer, TEILinker, ScopeParser

gaz = Gazetteer.from_json("kb/data/gazetteer.json")
linker = TEILinker(kb=gaz, scope_parser=ScopeParser(comma="list"))

tree = etree.parse("input.xml")
stats = linker.run(tree)

print(f"linked={stats.linked} review={stats.review} skipped={stats.skipped}")
for item in stats.review_items:
    print(item["raw"], "->", item["note"])

tree.write("output.xml", xml_declaration=True, encoding="UTF-8")
```

To plug in a custom text backend for quote verification, implement `fetch(urn: str) -> str | None` and pass it as `resolver=`:

```python
class MyResolver:
    def fetch(self, urn: str) -> str | None:
        ...  # call Perseus CTS API, Scaife, or a local corpus checkout

linker = TEILinker(kb=gaz, resolver=MyResolver())
```

### Gazetteer format

The gazetteer JSON maps textgroup URNs to author records:

```json
{
  "urn:cts:greekLit:tlg0012": {
    "name_abbrevs": ["Hom.", "Hom"],
    "default_work": null,
    "works": {
      "urn:cts:greekLit:tlg0012.tlg001": {
        "title_abbrevs": ["Il."],
        "scheme": "book.line"
      }
    }
  }
}
```

`kb/data/gazetteer.json` is the merged production gazetteer. The build
pipeline has four stages; run them in order.

### Prerequisites

Clone the Perseus canonical text repos alongside this one. (Note that the locations
in `~/code/PerseusDL` are just for demonstration purposes. Clone as you usually would
and update the examples below accordingly.)

```sh
git clone https://github.com/PerseusDL/canonical-greekLit ~/code/PerseusDL/canonical-greekLit
git clone https://github.com/PerseusDL/canonical-latinLit ~/code/PerseusDL/canonical-latinLit
```

You also need the HuCit RDF export (`hucit.trig`) in the project root.
It is not checked in; obtain it from the project maintainers.

### Step 1 — Convert HuCit TriG → abbreviation gazetteer

Parses the HuCit RDF named graphs and extracts author/work abbreviations
keyed by CTS URN. Citation schemes are left as `"flat"` placeholders; they
are filled in step 3.

```sh
uv run python kb/trig_to_gazetteer.py hucit.trig kb/data/hucit.gazetteer.json

# Add --verify to spot-check Homer (Hom. / Il. / Od.) after writing:
uv run python kb/trig_to_gazetteer.py hucit.trig kb/data/hucit.gazetteer.json --verify
```

### Step 2 — Harvest CTS skeleton from canonical-\*Lit repos

Walks `__cts__.xml` files for URNs and canonical titles, then derives the
citation scheme for each work from its `refsDecl/cRefPattern` nodes (e.g.
`book.line`, `book.chapter.section`, `stephanus`).

```sh
uv run python kb/cts_harvester.py \
    --greek ~/code/PerseusDL/canonical-greekLit \
    --latin ~/code/PerseusDL/canonical-latinLit \
    --output kb/data/cts.skeleton.json

# Add --verify to check Iliad (book.line), Thucydides (book.chapter.section),
# and Plato Republic (stephanus):
uv run python kb/cts_harvester.py \
    --greek ~/code/PerseusDL/canonical-greekLit \
    --latin ~/code/PerseusDL/canonical-latinLit \
    --output kb/data/cts.skeleton.json --verify
```

### Step 3 — Merge into the production gazetteer

Combines the two outputs: CTS is authoritative for identity and citation
scheme; HuCit is authoritative for abbreviations.

```sh
uv run python kb/merge_gazetteer.py

# Non-default input paths and merge log:
uv run python kb/merge_gazetteer.py \
    --hucit kb/data/hucit.gazetteer.json \
    --cts   kb/data/cts.skeleton.json \
    --output kb/data/gazetteer.json \
    --log kb/data/merge.log.json \
    --verify
```

### Step 4 — Enrich with abbreviation seeds (optional)

Layers additional abbreviations from seed files onto the merged gazetteer.
Open (CC-licensed) seeds live in `kb/seeds/open/`; proprietary seeds
(LSJ, OLD, TLL) go in `kb/seeds/restricted/` (gitignored — **do not commit
or redistribute**).

```sh
# Open seeds only (redistributable build):
uv run python kb/enrich_gazetteer.py

# Include restricted seeds (private build only):
uv run python kb/enrich_gazetteer.py --restricted

# Print coverage report without writing any files:
uv run python kb/enrich_gazetteer.py --report-only
```

Each seed file in `kb/seeds/open/` is a JSON object:

```json
{
  "source": "hand_curated",
  "license": "CC0",
  "entries": [
    {
      "tg_urn": "urn:cts:greekLit:tlg0012",
      "author_abbrev": "Hom.",
      "work_urn": "urn:cts:greekLit:tlg0012.tlg001",
      "work_abbrev": "Il."
    }
  ]
}
```

Entries without a `tg_urn` are reconciled to the gazetteer by
`author_name`; entries without a `work_urn` are reconciled by `work_name`.
Ambiguous or unresolved entries are reported but never silently applied.

### TL;DR (Full rebuild with all steps)

```sh
uv run python kb/trig_to_gazetteer.py hucit.trig kb/data/hucit.gazetteer.json
uv run python kb/cts_harvester.py \
    --greek ~/code/PerseusDL/canonical-greekLit \
    --latin ~/code/PerseusDL/canonical-latinLit \
    --output kb/data/cts.skeleton.json
uv run python kb/merge_gazetteer.py --verify
uv run python kb/enrich_gazetteer.py
```


## Copyright notices

- HuCit data used for the gazetteer is copyright Matteo Romanello and licensed under GNU GPL v3.

## LICENSE

MIT License

Copyright (c) 2026 The Perseus Digital Library

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
