"""Shared utilities for kb/ pipeline scripts."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path


def _norm(s: str) -> str:
    """Casefold + strip accents + collapse whitespace for fuzzy name matching."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.casefold().split())


def _build_name_index(gazetteer: dict) -> dict[str, list[str]]:
    """{normalised_name: [tg_urn, ...]} from canonical_name and name_abbrevs."""
    idx: dict[str, list[str]] = {}
    for tg_urn, rec in gazetteer.items():
        names: list[str] = []
        cn = rec.get("canonical_name", "")
        if cn:
            names.append(cn)
        for abbr in rec.get("name_abbrevs", []):
            stripped = abbr.rstrip(".")
            if len(stripped) > 2:
                names.append(stripped)
        for name in names:
            key = _norm(name)
            if key:
                idx.setdefault(key, []).append(tg_urn)
    return idx


def _load_tracking(repo: Path, warn: bool = True) -> dict:
    """Load *.tracking.json from a canonical-*Lit repo, or {} on any error."""
    candidates = list(repo.glob("*.tracking.json"))
    if not candidates:
        return {}
    path = candidates[0]
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        if warn:
            print(
                f"  warning: {path.name} not parseable ({exc}); "
                "falling back to in-file refsDecl detection",
                file=sys.stderr,
            )
        return {}


def _has_cts_refsDecl_file(xml_path: Path) -> bool:
    """Scan a file for <refsDecl n="CTS"> without a full parse."""
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "refsDecl" in line and 'n="CTS"' in line:
                    return True
    except OSError:
        pass
    return False
