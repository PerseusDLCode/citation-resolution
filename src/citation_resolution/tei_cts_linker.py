#!/usr/bin/env python3
"""
tei_cts_linker.py
=================

Walk a TEI document, find already-tagged <bibl> and <cit> elements, and map the
*canonical* (primary-source) references among them to CTS URNs, writing the URNs
back into the tree in place. Secondary literature is left untouched. References
the knowledge base can't resolve are flagged for human review (the INCEpTION
correction loop), never silently dropped.

This replaces the extraction stage of a from-scratch pipeline (no CRF / no span
model needed) with a tree walk, and keeps the three stages that actually matter:

    resolve-or-skip   (filter + match against the KB, in one operation)
        -> parse scope (per-work grammar -> CTS passage component)
        -> verify       (against the <quote> inside a <cit>, where present)
        -> write back   (@ref / @source / citedRange, plus a review flag for NILs)

The KB, the passage resolver, and the scope grammar are deliberately swappable.
NullResolver is bundled so the script runs without a text backend; implement
the PassageResolver protocol to plug in Perseus / canonical-greekLit text
services for quote verification.

Usage
-----
    python -m citation_resolution.tei_cts_linker input.xml -o output.xml
    python -m citation_resolution.tei_cts_linker input.xml -o output.xml --gazetteer gaz.json --report report.json

Caveats worth reading before you trust the output
--------------------------------------------------
* Scope separators are convention-dependent. Dot is treated as the citation-level
  separator and ';' as a reference-list separator. Comma is configurable because
  some continental styles use it as a *level* separator ("11,4,11" = Pliny
  nat. 11.4.11) rather than a list separator. Set ScopeParser(comma="level").
* Flat <bibl> interiors are enriched with a single (possibly multi-valued) @ref
  by default — existing markup is preserved. Pass decompose=True to also split
  the interior into <author>/<title>/<citedRange>; this rewrites mixed content,
  so review the whitespace/punctuation results.
* Work-level URNs are emitted (e.g. ...tlg001:1.1). Version selection
  (...tlg001.perseus-grc2:1.1) is a separate downstream step.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence

from lxml import etree  # ty: ignore

TEI_NS = "http://www.tei-c.org/ns/1.0"


# --------------------------------------------------------------------------- #
# Roman numeral conversion (used by ScopeParser for biblical chapter.verse)
# --------------------------------------------------------------------------- #
_ROMAN_VALID = re.compile(
    r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$",
    re.IGNORECASE,
)
_ROMAN_VALUE = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_arabic(tok: str) -> Optional[int]:
    """Convert a Roman numeral string to int, or None if tok is not a valid Roman numeral."""
    if not tok:
        return None
    t = tok.upper()
    if not _ROMAN_VALID.match(t):
        return None
    val = prev = 0
    for ch in reversed(t):
        curr = _ROMAN_VALUE[ch]
        val += curr if curr >= prev else -curr
        prev = curr
    return val if val > 0 else None
# Namespace for our own review/processing flags, kept out of the TEI namespace
# so it's trivially greppable and strippable before publication.
NEL_NS = "https://example.org/ns/nel"


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #
@dataclass
class WorkRecord:
    """A citable work in the knowledge base."""

    work_urn: str  # e.g. urn:cts:greekLit:tlg0012.tlg001
    scheme: str  # citation scheme key, e.g. "book.line", "stephanus"
    title_abbrevs: tuple[str, ...] = ()


@dataclass
class AuthorRecord:
    """An author (textgroup) in the knowledge base."""

    textgroup_urn: str  # e.g. urn:cts:greekLit:tlg0012
    name_abbrevs: tuple[str, ...] = ()
    works: dict[str, WorkRecord] = field(default_factory=dict)  # title-abbrev -> work
    default_work: Optional[str] = (
        None  # title-abbrev of the work to assume when none cited
    )


@dataclass
class Candidate:
    """A resolved (author, work) pair plus the scheme needed to parse its scope."""

    work_urn: str
    scheme: str
    author_abbrev: str
    work_abbrev: Optional[str]
    ambiguous: bool = (
        False  # True if more than one author/work matched the surface form
    )


@dataclass
class Reference:
    """One extracted reference inside a bibl/cit before resolution."""

    raw: str
    author_token: Optional[str]
    work_token: Optional[str]
    scope_surface: Optional[str]
    inherited_author: bool = False


@dataclass
class Resolved:
    """The outcome for one reference."""

    reference: Reference
    urn: Optional[str] = None  # full work-level URN incl. passage, or None
    candidate: Optional[Candidate] = None
    status: str = "unresolved"  # "linked" | "review" | "skipped"
    note: str = ""


# --------------------------------------------------------------------------- #
# Knowledge base
# --------------------------------------------------------------------------- #
class KnowledgeBase(Protocol):
    """Minimal surface the matcher needs. Implement this to plug in real HuCit."""

    def author_for(self, token: str) -> list[AuthorRecord]: ...
    def work_for(self, author: AuthorRecord, token: str) -> Optional[WorkRecord]: ...


class Gazetteer:
    """In-memory KB built from a plain dict/JSON. Good enough to run and test.

    JSON shape:
        {
          "urn:cts:greekLit:tlg0012": {
            "name_abbrevs": ["Hom.", "Hom"],
            "default_work": "Il.",            # optional
            "works": {
              "urn:cts:greekLit:tlg0012.tlg001": {
                "title_abbrevs": ["Il."], "scheme": "book.line"
              },
              ...
            }
          },
          ...
        }
    """

    def __init__(self, authors: Sequence[AuthorRecord]):
        self._authors = list(authors)
        # Build lookup indices. Abbreviations are matched case-sensitively first
        # (classics abbreviations are case-bearing: "A." vs "a."), then folded.
        self._by_name: dict[str, list[AuthorRecord]] = {}
        self._by_name_folded: dict[str, list[AuthorRecord]] = {}
        for a in self._authors:
            for abbr in a.name_abbrevs:
                self._by_name.setdefault(abbr, []).append(a)
                self._by_name_folded.setdefault(_fold(abbr), []).append(a)

    @classmethod
    def from_json(cls, path: str) -> "Gazetteer":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Gazetteer":
        authors = []
        for tg_urn, arec in data.items():
            works = {}
            for w_urn, wrec in arec.get("works", {}).items():
                abbrs = tuple(wrec.get("title_abbrevs", []))
                wr = WorkRecord(
                    work_urn=w_urn,
                    scheme=wrec.get("scheme", "flat"),
                    title_abbrevs=abbrs,
                )
                for ab in abbrs:
                    works[ab] = wr
            authors.append(
                AuthorRecord(
                    textgroup_urn=tg_urn,
                    name_abbrevs=tuple(arec.get("name_abbrevs", [])),
                    works=works,
                    default_work=arec.get("default_work"),
                )
            )
        return cls(authors)

    def author_for(self, token: str) -> list[AuthorRecord]:
        if token in self._by_name:
            return list(self._by_name[token])
        return list(self._by_name_folded.get(_fold(token), []))

    def work_for(self, author: AuthorRecord, token: str) -> Optional[WorkRecord]:
        if token in author.works:
            return author.works[token]
        folded = {_fold(k): v for k, v in author.works.items()}
        return folded.get(_fold(token))



# --------------------------------------------------------------------------- #
# Segmentation: turn a citation string into Reference objects
# --------------------------------------------------------------------------- #
# A "scope-ish" token: digits with internal . , : and a possible trailing letter
# (Stephanus 327a, Bekker 1094a) and en/em dashes for ranges.
_SCOPE_RE = re.compile(r"^[0-9][0-9.,:–—-]*[a-z]?(?:[–—-][0-9][0-9.,:]*[a-z]?)?$")
_MAX_NAME_TOKENS = 3  # allow multiword author abbrevs ("Apoll. Rhod.", "A. R.")


def _looks_like_scope(tok: str) -> bool:
    return bool(_SCOPE_RE.match(tok))


def segment(
    text: str, kb: KnowledgeBase, prev_author: Optional[str]
) -> list[Reference]:
    """Segment one bibl/cit string into references.

    Splits on ';' into chunks (a reference list), then within each chunk greedily
    matches leading tokens as author, then work, leaving the remainder as scope.
    Author elision is handled by inheriting `prev_author` across chunks.
    """
    refs: list[Reference] = []
    last_author = prev_author
    for chunk in (c.strip() for c in text.split(";")):
        if not chunk:
            continue
        tokens = chunk.split()
        i = 0
        author_tok = None
        inherited = False

        # Greedy longest-match author (up to _MAX_NAME_TOKENS leading tokens).
        for n in range(min(_MAX_NAME_TOKENS, len(tokens)), 0, -1):
            cand = " ".join(tokens[:n])
            if kb.author_for(cand):
                author_tok = cand
                i = n
                break
        if author_tok is None:
            # No author surface here: inherit the previous one (elision).
            author_tok = last_author
            inherited = author_tok is not None

        # Greedy longest-match work title from the remaining tokens, but only if
        # we have an author to attach it to.
        work_tok = None
        if author_tok is not None:
            arecs = kb.author_for(author_tok)
            for n in range(min(_MAX_NAME_TOKENS, len(tokens) - i), 0, -1):
                cand = " ".join(tokens[i : i + n])
                if any(kb.work_for(a, cand) for a in arecs):
                    work_tok = cand
                    i += n
                    break

        scope = " ".join(tokens[i:]).strip() or None
        # Filter: if nothing matched as author, this chunk is secondary lit / noise.
        if author_tok is None:
            refs.append(
                Reference(
                    raw=chunk, author_token=None, work_token=None, scope_surface=scope
                )
            )
            continue

        last_author = author_tok  # carry forward for the next chunk's elision
        refs.append(
            Reference(
                raw=chunk,
                author_token=author_tok,
                work_token=work_tok,
                scope_surface=scope,
                inherited_author=inherited,
            )
        )
    return refs


# --------------------------------------------------------------------------- #
# Matching: Reference -> Candidate(work_urn, scheme)
# --------------------------------------------------------------------------- #
def match(ref: Reference, kb: KnowledgeBase) -> Optional[Candidate]:
    if ref.author_token is None:
        return None
    authors = kb.author_for(ref.author_token)
    if not authors:
        return None

    # Resolve the work for each candidate author; collect (author, work) pairs.
    pairs: list[tuple[AuthorRecord, WorkRecord]] = []
    for a in authors:
        if ref.work_token is not None:
            w = kb.work_for(a, ref.work_token)
            if w is not None:
                pairs.append((a, w))
        elif a.default_work is not None:
            w = a.works.get(a.default_work)
            if w is not None:
                pairs.append((a, w))

    if not pairs:
        return None
    ambiguous = len(pairs) > 1
    a, w = pairs[
        0
    ]  # caller may downrank ambiguous matches; quote verification breaks ties
    return Candidate(
        work_urn=w.work_urn,
        scheme=w.scheme,
        author_abbrev=ref.author_token,
        work_abbrev=ref.work_token,
        ambiguous=ambiguous,
    )


# --------------------------------------------------------------------------- #
# Scope parsing: surface scope -> CTS passage component(s)
# --------------------------------------------------------------------------- #
@dataclass
class ParsedScope:
    passage: Optional[str]  # e.g. "1.1" or "1.1-1.7"; None if unparseable
    from_ref: Optional[str] = None
    to_ref: Optional[str] = None


_SQ_RE = re.compile(r"\s+sqq?\.\s*$", re.IGNORECASE)
# Matches a scope string that begins with a Roman numeral (biblical chapter.verse).
_ROMAN_SCOPE_RE = re.compile(r"^[IVXLCDM]+[,.\s]", re.IGNORECASE)


class ScopeParser:
    """Normalize a surface scope into a CTS passage component.

    - level separator: '.'  (configurable extra: comma as level for continental style)
    - range separators: en/em dash or hyphen
    - abbreviated range endpoints are back-filled from the start ref
      ("1.1-7" -> from 1.1, to 1.7 -> "1.1-1.7")
    - Stephanus/Bekker tokens (trailing letter) are kept atomic.
    - Roman numeral chapter/verse scopes (biblical style: "IV, 19.") are
      auto-detected and converted to Arabic dot-notation ("4.19").
    """

    _DASHES = ("–", "—", "-")
    # A valid single CTS passage ref: dot-separated numeric levels with an
    # optional single trailing letter (Stephanus 327a, Bekker 1094a). No spaces,
    # no editor sigla ("31 V."), no stray uppercase — those must go to review.
    _REF_RE = re.compile(r"^[0-9]+(\.[0-9]+)*[a-z]?$")

    def __init__(self, comma: str = "list"):
        if comma not in ("list", "level"):
            raise ValueError("comma must be 'list' or 'level'")
        self.comma = comma

    def _valid_ref(self, ref: str) -> bool:
        return bool(self._REF_RE.match(ref))

    def _preprocess_scope(self, scope: str) -> str:
        """Strip open-ended sq./sqq. suffixes before range splitting."""
        return _SQ_RE.sub("", scope).rstrip("., ")

    def _normalize_levels(self, ref: str) -> str:
        ref = ref.strip().rstrip("., ")
        # Treat comma as level separator if configured, or if the ref begins
        # with a Roman numeral (biblical chapter.verse style: "IV, 19").
        if self.comma == "level" or _ROMAN_SCOPE_RE.match(ref):
            ref = ref.replace(",", ".")
        # Collapse any whitespace around dots (e.g. "IV. 19" -> "IV.19")
        ref = re.sub(r"\s*\.\s*", ".", ref).strip(".")
        # Convert Roman numeral parts to Arabic
        parts = [p for p in ref.split(".") if p]
        result = []
        for p in parts:
            n = _roman_to_arabic(p)
            result.append(str(n) if n is not None else p)
        return ".".join(result)

    def _split_range(self, scope: str) -> tuple[str, Optional[str]]:
        for d in self._DASHES:
            if d in scope:
                left, _, right = scope.partition(d)
                return left.strip(), right.strip()
        return scope.strip(), None

    @staticmethod
    def _backfill(start: str, end: str) -> str:
        """Expand an abbreviated range endpoint using the start's leading levels.

        start="1.1", end="7"  -> "1.7"      (replace last level)
        start="1.22.1", end="2.3" -> "1.2.3" (replace trailing len(end) levels)
        start="327a", end="328b" -> "328b"  (already full)
        """
        s_parts = start.split(".")
        e_parts = end.split(".")
        if len(e_parts) >= len(s_parts):
            return end
        merged = s_parts[: len(s_parts) - len(e_parts)] + e_parts
        return ".".join(merged)

    def parse(self, scope_surface: Optional[str]) -> ParsedScope:
        if not scope_surface:
            return ParsedScope(passage=None)
        scope = self._preprocess_scope(scope_surface.strip())
        if not scope:
            return ParsedScope(passage=None)
        start, end = self._split_range(scope)
        start = self._normalize_levels(start)
        if not self._valid_ref(start):
            # Not a plain CTS scope: editor siglum, free text, or "fr. 31 V.".
            return ParsedScope(passage=None)
        if end is None:
            return ParsedScope(passage=start, from_ref=start, to_ref=None)
        end = self._normalize_levels(end)
        end_full = self._backfill(start, end)
        if not self._valid_ref(end_full):
            return ParsedScope(passage=None)
        return ParsedScope(
            passage=f"{start}-{end_full}", from_ref=start, to_ref=end_full
        )


# --------------------------------------------------------------------------- #
# Passage resolution + quote verification
# --------------------------------------------------------------------------- #
class PassageResolver(Protocol):
    """Return the text at a (work-or-version-level) CTS URN, or None if unavailable."""

    def fetch(self, urn: str) -> Optional[str]: ...


class NullResolver:
    """Default: no text backend, so quote verification is skipped (not failed)."""

    def fetch(self, urn: str) -> Optional[str]:  # noqa: D401
        return None


# To wire a real one: implement fetch() against the Perseus CTS API
# (GetPassage), a Scaife endpoint, or a local checkout of canonical-greekLit /
# canonical-latinLit, mapping the work-level URN to an available version first.


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _fold(s: str) -> str:
    """Casefold + accent-strip + NFC, for tolerant abbreviation/text comparison."""
    return unicodedata.normalize("NFC", _strip_accents(s)).casefold()


_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def quote_matches(quote: str, passage_text: str) -> bool:
    """Accent-insensitive, punctuation-insensitive containment check."""
    q = _PUNCT_RE.sub("", _fold(quote))
    t = _PUNCT_RE.sub("", _fold(passage_text))
    return bool(q) and q in t


# --------------------------------------------------------------------------- #
# The tree walk
# --------------------------------------------------------------------------- #
@dataclass
class LinkStats:
    bibl_seen: int = 0
    cit_seen: int = 0
    linked: int = 0
    review: int = 0
    skipped: int = 0
    verified: int = 0
    verify_failed: int = 0
    review_items: list[dict] = field(default_factory=list)


class TEILinker:
    def __init__(
        self,
        kb: KnowledgeBase,
        scope_parser: Optional[ScopeParser] = None,
        resolver: Optional[PassageResolver] = None,
        decompose: bool = False,
    ):
        self.kb = kb
        self.scope_parser = scope_parser or ScopeParser()
        self.resolver = resolver or NullResolver()
        self.decompose = decompose
        self.stats = LinkStats()

    # -- namespace helpers --------------------------------------------------- #
    @staticmethod
    def _ns(tree: etree._Element) -> Optional[str]:
        """Detect whether the document uses the TEI namespace."""
        tag = etree.QName(tree).namespace
        return tag

    def _q(self, ns: Optional[str], local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    # -- text extraction ----------------------------------------------------- #
    @staticmethod
    def _flatten_text(el: etree._Element) -> str:
        """All descendant text of an element, whitespace-normalized."""
        return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

    def _componentized(
        self, el: etree._Element, ns: Optional[str]
    ) -> Optional[Reference]:
        """If the bibl already has author/title/biblScope children, read them
        directly instead of segmenting a flat string."""
        author = el.find(self._q(ns, "author"))
        title = el.find(self._q(ns, "title"))
        scope = el.find(self._q(ns, "biblScope"))
        scope2 = el.find(self._q(ns, "citedRange"))
        if author is None and title is None and scope is None and scope2 is None:
            return None
        scope_el = scope if scope is not None else scope2
        return Reference(
            raw=self._flatten_text(el),
            author_token=(author.text or "").strip() if author is not None else None,
            work_token=(title.text or "").strip() if title is not None else None,
            scope_surface=(
                self._flatten_text(scope_el) if scope_el is not None else None
            ),
        )

    # -- per-reference resolution ------------------------------------------- #
    def _resolve_reference(self, ref: Reference) -> Resolved:
        if ref.author_token is None:
            return Resolved(reference=ref, status="skipped", note="no KB author match")
        cand = match(ref, self.kb)
        if cand is None:
            return Resolved(
                reference=ref,
                status="review",
                note="author matched but work unresolved",
            )
        parsed = self.scope_parser.parse(ref.scope_surface)
        if ref.scope_surface and parsed.passage is None:
            # e.g. a fragment "fr. 31 V." — canonical author, non-CTS scope.
            return Resolved(
                reference=ref,
                candidate=cand,
                status="review",
                note="scope not CTS-parseable (fragment / editor scheme?)",
            )
        urn = cand.work_urn
        if parsed.passage:
            urn = f"{cand.work_urn}:{parsed.passage}"
        status = "review" if cand.ambiguous else "linked"
        note = "ambiguous abbreviation; verify" if cand.ambiguous else ""
        return Resolved(
            reference=ref, candidate=cand, urn=urn, status=status, note=note
        )

    # -- the walk ------------------------------------------------------------ #
    def run(self, tree: etree._ElementTree) -> LinkStats:
        root = tree.getroot()
        ns = self._ns(root)
        etree.register_namespace("nel", NEL_NS)

        bibl_tag = self._q(ns, "bibl")
        cit_tag = self._q(ns, "cit")
        quote_tag = self._q(ns, "quote")

        for el in root.iter():
            if el.tag == cit_tag:
                self.stats.cit_seen += 1
                self._handle_cit(el, ns, quote_tag)
            elif el.tag == bibl_tag:
                # Skip bibls that are inside a cit; those are handled via the cit
                # so we can use the quote for verification.
                if self._has_ancestor(el, cit_tag):
                    continue
                self.stats.bibl_seen += 1
                self._handle_bibl(el, ns, quote=None)
        return self.stats

    @staticmethod
    def _has_ancestor(el: etree._Element, tag: str) -> bool:
        p = el.getparent()
        while p is not None:
            if p.tag == tag:
                return True
            p = p.getparent()
        return False

    def _handle_cit(
        self, cit: etree._Element, ns: Optional[str], quote_tag: str
    ) -> None:
        bibl = cit.find(self._q(ns, "bibl"))
        quote_el = cit.find(quote_tag)
        quote = self._flatten_text(quote_el) if quote_el is not None else None
        if bibl is None:
            return
        resolveds = self._handle_bibl(bibl, ns, quote=quote)
        # Mirror a single confident URN up onto <cit source="...">.
        linked = [r for r in resolveds if r.status == "linked" and r.urn]
        if len(linked) == 1:
            cit.set("source", linked[0].urn)

    def _handle_bibl(
        self, bibl: etree._Element, ns: Optional[str], quote: Optional[str]
    ) -> list[Resolved]:
        # Respect an explicit @type hint if present.
        btype = (bibl.get("type") or "").lower()
        if btype in ("secondary", "modern"):
            self.stats.skipped += 1
            return []

        comp = self._componentized(bibl, ns)
        if comp is not None:
            refs = [comp]
        else:
            refs = segment(self._flatten_text(bibl), self.kb, prev_author=None)

        resolveds = [self._resolve_reference(r) for r in refs]

        # Quote verification (only meaningful for a single-reference cit).
        if quote and len([r for r in resolveds if r.urn]) == 1:
            r = next(r for r in resolveds if r.urn)
            assert r.urn is not None
            text = self.resolver.fetch(r.urn)
            if text is not None:
                if quote_matches(quote, text):
                    self.stats.verified += 1
                    r.note = (r.note + "; quote verified").strip("; ")
                    if r.status == "review" and "ambiguous" in r.note:
                        # Verification breaks the ambiguity tie.
                        r.status = "linked"
                else:
                    self.stats.verify_failed += 1
                    r.status = "review"
                    r.note = (r.note + "; quote MISMATCH").strip("; ")

        self._write_back(bibl, ns, resolveds, comp is not None)
        return resolveds

    # -- writing attributes back -------------------------------------------- #
    def _write_back(
        self,
        bibl: etree._Element,
        ns: Optional[str],
        resolveds: list[Resolved],
        was_componentized: bool,
    ) -> None:
        linked = [r for r in resolveds if r.status == "linked" and r.urn]
        review = [r for r in resolveds if r.status == "review"]
        skipped_all = resolveds and all(r.status == "skipped" for r in resolveds)

        if skipped_all:
            self.stats.skipped += 1
            return

        if linked:
            # @ref is multi-valued (space-separated) — fine for a reference list.
            bibl.set("ref", " ".join(r.urn for r in linked if r.urn is not None))
            self.stats.linked += len(linked)
            if was_componentized:
                self._annotate_components(bibl, ns, linked[0])
            elif self.decompose and len(linked) == 1:
                self._decompose_interior(bibl, ns, linked[0])

        for r in review:
            self.stats.review += 1
            bibl.set(f"{{{NEL_NS}}}status", "review")
            if r.note:
                bibl.set(f"{{{NEL_NS}}}note", r.note)
            self.stats.review_items.append(
                {
                    "raw": r.reference.raw,
                    "author": r.reference.author_token,
                    "work": r.reference.work_token,
                    "scope": r.reference.scope_surface,
                    "partial_urn": r.candidate.work_urn if r.candidate else None,
                    "note": r.note,
                }
            )

    def _annotate_components(
        self, bibl: etree._Element, ns: Optional[str], r: Resolved
    ) -> None:
        """Put work/textgroup URNs onto existing author/title and add citedRange."""
        if r.candidate is None or r.urn is None:
            return
        work_urn = r.candidate.work_urn
        tg_urn = work_urn.rsplit(".", 1)[0]
        author = bibl.find(self._q(ns, "author"))
        title = bibl.find(self._q(ns, "title"))
        if author is not None:
            author.set("ref", tg_urn)
        if title is not None:
            title.set("ref", work_urn)
        parsed = self.scope_parser.parse(r.reference.scope_surface)
        scope_el = bibl.find(self._q(ns, "biblScope"))
        if scope_el is None:
            scope_el = bibl.find(self._q(ns, "citedRange"))
        if scope_el is not None and parsed.from_ref:
            scope_el.set("from", parsed.from_ref)
            if parsed.to_ref:
                scope_el.set("to", parsed.to_ref)

    def _decompose_interior(
        self, bibl: etree._Element, ns: Optional[str], r: Resolved
    ) -> None:
        """Optional: rewrite a flat interior into author/title/citedRange.

        Disabled by default because it rewrites mixed content. Only fires for a
        single resolved reference.
        """
        if r.candidate is None:
            return
        work_urn = r.candidate.work_urn
        tg_urn = work_urn.rsplit(".", 1)[0]
        parsed = self.scope_parser.parse(r.reference.scope_surface)
        for child in list(bibl):
            bibl.remove(child)
        bibl.text = None
        if r.reference.author_token and not r.reference.inherited_author:
            a = etree.SubElement(bibl, self._q(ns, "author"))
            a.text = r.reference.author_token
            a.set("ref", tg_urn)
            a.tail = " "
        if r.reference.work_token:
            t = etree.SubElement(bibl, self._q(ns, "title"))
            t.text = r.reference.work_token
            t.set("ref", work_urn)
            t.tail = " "
        if parsed.from_ref:
            cr = etree.SubElement(bibl, self._q(ns, "citedRange"))
            cr.text = r.reference.scope_surface
            cr.set("from", parsed.from_ref)
            if parsed.to_ref:
                cr.set("to", parsed.to_ref)


_DEFAULT_GAZETTEER = (
    Path(__file__).resolve().parent.parent.parent / "kb" / "data" / "gazetteer.json"
)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Map tagged TEI bibl/cit to CTS URNs.")
    p.add_argument("input", help="input TEI XML file")
    p.add_argument("-o", "--output", help="output TEI XML file")
    p.add_argument(
        "--gazetteer",
        default=str(_DEFAULT_GAZETTEER),
        help="JSON abbreviation->URN gazetteer (default: kb/data/gazetteer.json)",
    )
    p.add_argument("--report", help="write a JSON report of items needing review")
    p.add_argument(
        "--decompose",
        action="store_true",
        help="split flat bibl interiors into author/title/citedRange",
    )
    p.add_argument(
        "--comma",
        choices=["list", "level"],
        default="list",
        help="treat ',' in scopes as a reference list or a citation level",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    gaz = Gazetteer.from_json(args.gazetteer)
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
    tree = etree.parse(args.input, parser)

    linker = TEILinker(
        kb=gaz,
        scope_parser=ScopeParser(comma=args.comma),
        resolver=NullResolver(),
        decompose=args.decompose,
    )
    stats = linker.run(tree)

    out_bytes = etree.tostring(
        tree, xml_declaration=True, encoding="UTF-8", pretty_print=False
    )
    if args.output:
        with open(args.output, "wb") as fh:
            fh.write(out_bytes)
    else:
        sys.stdout.buffer.write(out_bytes)
        sys.stdout.write("\n")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(
                {"stats": dataclasses.asdict(stats)}, fh, ensure_ascii=False, indent=2
            )

    sys.stderr.write(
        f"\n[summary] bibl={stats.bibl_seen} cit={stats.cit_seen} "
        f"linked={stats.linked} review={stats.review} skipped={stats.skipped} "
        f"verified={stats.verified} verify_failed={stats.verify_failed}\n"
    )
    for item in stats.review_items:
        sys.stderr.write(f"  [review] {item['raw']!r} -> {item['note']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
