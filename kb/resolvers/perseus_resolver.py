#!/usr/bin/env python3
"""
perseus_resolver.py
===================

PassageResolver fetches passage text from local clones of
PerseusDL/canonical-greekLit and canonical-latinLit.

Satisfies the PassageResolver protocol defined in tei_cts_linker.py:

    class PassageResolver(Protocol):
        def fetch(self, urn: str) -> Optional[str]: ...

Given a work-level CTS URN with a passage component (e.g.
``urn:cts:greekLit:tlg0012.tlg001:1.1``), the resolver:

1. Locates the best available edition XML for that work (preferring CTS-
   compliant, original-language files over P4-era and translation files).
2. Parses the TEI XML once and caches the element tree in memory.
3. Navigates the ``<div type="textpart">`` hierarchy (or Stephanus milestone
   chain) to find the requested passage.
4. Returns the whitespace-normalized text, or ``None`` on any failure.

The caller (TEILinker) uses the returned text for quote verification only —
a ``None`` result means "skip verification", not "mismatch".

Passage extraction strategy
----------------------------
- Div-based schemes (``book.line``, ``book.chapter.section``, …):
  navigate by ``n`` attribute through ``<div type="textpart">`` children,
  then for the final level fall back to any non-milestone element with that
  ``n`` (covers ``<l>``, ``<p>``, etc.).
- Stephanus scheme: find the target ``<milestone unit="section" n="327a"
  resp="Stephanus">`` and collect its ``tail`` plus sibling text until the
  next Stephanus section milestone.  Falls back to the full page div text
  if the section milestone is absent.

Usage
-----
    from pathlib import Path
    import json
    from kb.resolvers.perseus_resolver import LocalPassageResolver

    with open("kb/data/cts.skeleton.json") as f:
        skeleton = json.load(f)

    resolver = LocalPassageResolver(
        greek_repo=Path("~/code/PerseusDL/canonical-greekLit").expanduser(),
        latin_repo=Path("~/code/PerseusCode/canonical-latinLit").expanduser(),
        skeleton=skeleton,
    )
    text = resolver.fetch("urn:cts:greekLit:tlg0012.tlg001:1.1")
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _utils import _has_cts_refsDecl_file, _load_tracking  # noqa: E402

TEI_NS = "http://www.tei-c.org/ns/1.0"
TEI_DIV = f"{{{TEI_NS}}}div"
TEI_MILESTONE = f"{{{TEI_NS}}}milestone"

_LANG_PREF = {
    "grc": 10,  # Greek original
    "lat": 10,  # Latin original
    "eng": 1,
    "ger": 1,
    "fre": 1,
    "ita": 1,
}


# --------------------------------------------------------------------------- #
# File selection
# --------------------------------------------------------------------------- #
def _pick_best_file(
    work_dir: Path,
    work_urn: str,
    tracking: dict,
    corpus: str,
) -> Optional[Path]:
    """Return the best edition XML for quote verification.

    Score (higher is better):
      - CTS-compliant (tracking confirmed): +2
      - CTS-compliant (in-file detection): +1
      - Not CTS-compliant: 0
      - Original language (grc/lat): +10 language bonus
      - P4-era (version suffix ends with "1"): -1
    """
    xml_files = sorted(f for f in work_dir.glob("*.xml") if f.name != "__cts__.xml")
    if not xml_files:
        return None

    def _score(path: Path) -> int:
        stem = path.stem
        parts = stem.rsplit(".", 1)
        version_suffix = parts[-1] if len(parts) > 1 else ""
        version_urn = f"{work_urn}.{version_suffix}"

        tr = tracking.get(version_urn)
        if tr is None:
            cts_ok = 1 if _has_cts_refsDecl_file(path) else 0
        elif tr.get("has_cts_refsDecl"):
            cts_ok = 2
        else:
            cts_ok = 0

        # Language preference: orig_lang gets max bonus
        lang_bonus = 0
        for lang_key, bonus in _LANG_PREF.items():
            if lang_key in version_suffix:
                lang_bonus = bonus
                break

        p4_penalty = -1 if version_suffix.endswith("1") else 0
        return cts_ok + lang_bonus + p4_penalty

    ranked = sorted(xml_files, key=_score, reverse=True)
    best = ranked[0]
    if _score(best) <= 0:
        return None
    return best


# --------------------------------------------------------------------------- #
# TEI passage extraction
# --------------------------------------------------------------------------- #
def _collect_text(el: ET.Element) -> Optional[str]:
    text = " ".join(el.itertext())
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _find_content_root(root: ET.Element) -> Optional[ET.Element]:
    """Find the first <div type='edition'> or <div type='translation'>."""
    for el in root.iter(TEI_DIV):
        if el.get("type") in ("edition", "translation"):
            return el
    return None


def _find_child_div_n(parent: ET.Element, n_value: str) -> Optional[ET.Element]:
    """Find a direct child <div> with n=n_value."""
    for child in parent:
        if child.tag == TEI_DIV and child.get("n") == n_value:
            return child
    return None


def _find_child_element_n(parent: ET.Element, n_value: str) -> Optional[ET.Element]:
    """Find any direct child element (not milestone, not div) with n=n_value."""
    for child in parent:
        if child.tag not in (TEI_DIV, TEI_MILESTONE) and child.get("n") == n_value:
            return child
    return None


def _extract_div_passage(root: ET.Element, levels: list[str]) -> Optional[str]:
    """Navigate textpart div hierarchy by n values and return passage text."""
    container = _find_content_root(root)
    if container is None:
        return None

    current = container
    for level_n in levels[:-1]:
        child = _find_child_div_n(current, level_n)
        if child is None:
            return None
        current = child

    last_n = levels[-1]
    final = _find_child_div_n(current, last_n)
    if final is None:
        final = _find_child_element_n(current, last_n)
    if final is None:
        return None
    return _collect_text(final)


_STEPHANUS_RE = re.compile(r"^(\d+)([a-e]?)$")


def _extract_stephanus(root: ET.Element, passage: str) -> Optional[str]:
    """Extract text for a Stephanus passage like '327a' or '327'."""
    m = _STEPHANUS_RE.match(passage)
    if not m:
        return None
    page_n, section_letter = m.group(1), m.group(2)

    container = _find_content_root(root)
    if container is None:
        return None

    # Page div (any textpart div with this n — may be nested inside a book div)
    page_div = None
    for el in container.iter(TEI_DIV):
        if el.get("n") == page_n:
            page_div = el
            break
    if page_div is None:
        return None

    if not section_letter:
        return _collect_text(page_div)

    target_n = passage  # e.g. "327a"

    # Walk all descendants in document order; collect text between the target
    # Stephanus section milestone and the next one.
    in_target = False
    texts: list[str] = []

    for el in page_div.iter():
        if el.tag == TEI_MILESTONE:
            is_stephanus_section = (
                el.get("unit") == "section"
                and el.get("resp", "").lower() == "stephanus"
            )
            if is_stephanus_section:
                if el.get("n") == target_n:
                    in_target = True
                    if el.tail:
                        texts.append(el.tail)
                elif in_target:
                    break  # next section boundary
            elif in_target and el.tail:
                texts.append(el.tail)
        elif in_target and el.tag != TEI_DIV:
            if el.text:
                texts.append(el.text)
            if el.tail:
                texts.append(el.tail)

    if texts:
        return re.sub(r"\s+", " ", " ".join(texts)).strip() or None

    # Fallback: section milestone absent, return full page text
    return _collect_text(page_div)


def _extract_passage(root: ET.Element, passage: str, scheme: str) -> Optional[str]:
    if scheme == "stephanus":
        return _extract_stephanus(root, passage)
    levels = passage.split(".")
    return _extract_div_passage(root, levels)


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
class LocalPassageResolver:
    """Fetch passage text from local canonical-*Lit checkouts."""

    def __init__(
        self,
        greek_repo: Path,
        latin_repo: Path,
        skeleton: dict,
        verbose: bool = False,
    ):
        self._repos = {
            "greekLit": greek_repo,
            "latinLit": latin_repo,
        }
        self._skeleton = skeleton
        self._verbose = verbose
        # Cache: work_urn -> (xml_path | None, ET.Element root | None, scheme)
        self._cache: dict[str, tuple[Optional[Path], Optional[ET.Element], str]] = {}
        self._tracking: dict[str, dict] = {}

    def _get_tracking(self, corpus: str) -> dict:
        if corpus not in self._tracking:
            repo = self._repos.get(corpus)
            self._tracking[corpus] = (
                _load_tracking(repo, warn=False) if repo and repo.exists() else {}
            )
        return self._tracking[corpus]

    def _resolve_work(
        self, work_urn: str
    ) -> tuple[Optional[Path], Optional[ET.Element], str]:
        """Return (xml_path, parsed_root, scheme) for a work URN, cached."""
        if work_urn in self._cache:
            return self._cache[work_urn]

        result: tuple[Optional[Path], Optional[ET.Element], str] = (None, None, "flat")

        # Determine corpus and path components
        # work_urn: urn:cts:greekLit:tlg0012.tlg001
        parts = work_urn.split(":")
        if len(parts) < 4:
            self._cache[work_urn] = result
            return result

        corpus = parts[2]  # e.g. "greekLit"
        work_path = parts[3]  # e.g. "tlg0012.tlg001"
        work_parts = work_path.split(".")
        if len(work_parts) < 2:
            self._cache[work_urn] = result
            return result

        tg_id, work_id = work_parts[0], work_parts[1]
        repo = self._repos.get(corpus)
        if repo is None or not repo.exists():
            self._cache[work_urn] = result
            return result

        work_dir = repo / "data" / tg_id / work_id
        if not work_dir.is_dir():
            self._cache[work_urn] = result
            return result

        tracking = self._get_tracking(corpus)
        xml_path = _pick_best_file(work_dir, work_urn, tracking, corpus)
        if xml_path is None:
            self._cache[work_urn] = result
            return result

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError as exc:
            if self._verbose:
                print(
                    f"  [resolver] XML parse error {xml_path}: {exc}", file=sys.stderr
                )
            self._cache[work_urn] = result
            return result

        # Get scheme from skeleton
        tg_rec = self._skeleton.get(f"urn:cts:{corpus}:{tg_id}", {})
        w_rec = tg_rec.get("works", {}).get(work_urn, {})
        scheme = w_rec.get("scheme", "flat")

        result = (xml_path, root, scheme)
        self._cache[work_urn] = result
        return result

    def fetch(self, urn: str) -> Optional[str]:
        """Return passage text for a CTS URN, or None if unavailable.

        Accepts work-level URNs with a passage component:
            urn:cts:greekLit:tlg0012.tlg001:1.1
        Version-level URNs (with a version suffix) are also accepted; the
        version suffix is stripped before file selection.
        """
        # Split passage off the end: everything after the 4th colon
        # urn:cts:greekLit:tlg0012.tlg001:1.1  -> work=...tlg001, passage=1.1
        try:
            prefix, passage = urn.rsplit(":", 1)
        except ValueError:
            return None

        # prefix is urn:cts:<corpus>:<work_path>[.<version_suffix>]
        # Strip version suffix if present (e.g. tlg0012.tlg001.perseus-grc2 → tlg0012.tlg001)
        colon_parts = prefix.split(":")
        if len(colon_parts) < 4:
            return None
        work_path = colon_parts[3]
        work_path_parts = work_path.split(".")
        if len(work_path_parts) > 2:
            # Has version suffix — drop it to get the work-level URN
            colon_parts[3] = ".".join(work_path_parts[:2])
            prefix = ":".join(colon_parts)

        work_urn = prefix

        _, root, scheme = self._resolve_work(work_urn)
        if root is None:
            return None

        if not passage or scheme == "flat":
            return None

        try:
            text = _extract_passage(root, passage, scheme)
        except Exception as exc:
            if self._verbose:
                print(
                    f"  [resolver] extraction error for {urn}: {exc}", file=sys.stderr
                )
            return None

        if self._verbose and text:
            print(f"  [resolver] {urn} -> {text[:60]!r}…", file=sys.stderr)
        return text


# --------------------------------------------------------------------------- #
# Factory helper
# --------------------------------------------------------------------------- #
def make_resolver(
    skeleton_path: str = "kb/data/cts.skeleton.json",
    greek_repo: Optional[str] = None,
    latin_repo: Optional[str] = None,
    verbose: bool = False,
) -> LocalPassageResolver:
    """Convenience factory; uses default repo paths when not specified."""
    with open(skeleton_path, encoding="utf-8") as fh:
        skeleton = json.load(fh)
    return LocalPassageResolver(
        greek_repo=Path(
            greek_repo or "~/code/PerseusDL/canonical-greekLit"
        ).expanduser(),
        latin_repo=Path(
            latin_repo or "~/code/PerseusCode/canonical-latinLit"
        ).expanduser(),
        skeleton=skeleton,
        verbose=verbose,
    )


# --------------------------------------------------------------------------- #
# Quick smoke-test CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Fetch a passage from local canonical repos."
    )
    p.add_argument(
        "urn", help="CTS URN with passage, e.g. urn:cts:greekLit:tlg0012.tlg001:1.1"
    )
    p.add_argument("--skeleton", default="kb/data/cts.skeleton.json")
    p.add_argument("--greek", default=None)
    p.add_argument("--latin", default=None)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    resolver = make_resolver(
        skeleton_path=args.skeleton,
        greek_repo=args.greek,
        latin_repo=args.latin,
        verbose=args.verbose,
    )
    text = resolver.fetch(args.urn)
    if text:
        print(text)
    else:
        print(f"[not found: {args.urn}]", file=sys.stderr)
        raise SystemExit(1)
