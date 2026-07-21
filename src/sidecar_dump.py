"""Dump configured sidecar files into xlsx report tabs, verbatim.

Review provenance: the report carries the exact exclusions/stopwords/
aliases/groups inputs that produced it. No parsing, no translation.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Tab order after the Recipe tab, paired with the recipe key path each
# sidecar lives under. Exclusions accepts a bare string or {file: ...}.
SIDECAR_TABS = [
    ("Exclusions", ("exclusions",)),
    ("Stopwords", ("normalization", "stopwords")),
    ("Aliases", ("normalization", "aliases")),
    ("Groups", ("output", "groups", "file")),
]


def _dig(recipe: dict, keys: tuple[str, ...]):
    node = recipe
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def sidecar_ref(recipe: dict, keys: tuple[str, ...]) -> str | None:
    """Return the configured path for one sidecar, or None if unconfigured."""
    node = _dig(recipe, keys)
    if isinstance(node, dict):
        node = node.get("file")
    return node if isinstance(node, str) and node else None


def resolve_sidecar(ref: str, base_dir: str) -> Path:
    """Resolve a sidecar path literal-first, then relative to base_dir."""
    path = Path(ref)
    if not path.exists():
        path = Path(base_dir) / ref
    return path


def sidecar_rows(path: Path) -> list[str]:
    """Read a sidecar into dump rows: resolved path, then verbatim file lines.

    Every way a sidecar can defeat the dump -- unreadable, wrong encoding,
    bytes openpyxl refuses -- raises here, before the caller creates a
    worksheet. That ordering is what keeps a failure from leaving a
    truncated tab behind: a dump tab is exact or absent, never partial.
    """
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    rows = [str(path.resolve()), *path.read_text(encoding="utf-8-sig").splitlines()]
    for i, row in enumerate(rows):
        if ILLEGAL_CHARACTERS_RE.search(row):
            raise ValueError(f"line {i} has characters Excel cannot store")
    return rows


def write_sidecar_tab(ws, rows: list[str]) -> None:
    """Write pre-validated dump rows into column A, one row per line."""
    from openpyxl.styles import Font

    mono = Font(name="Consolas", size=10)
    for i, line in enumerate(rows, start=1):
        cell = ws.cell(row=i, column=1, value=line if line else None)
        cell.font = mono
    ws.column_dimensions["A"].width = 100


def write_sidecar_tabs(wb, recipe: dict, base_dir: str = ".") -> None:
    """Append a raw-dump tab per configured sidecar, in SIDECAR_TABS order.

    A dump tab never fails a report: an unreadable sidecar is skipped with
    a [WARN]. Real misconfiguration is caught by loader-level validation.
    """
    if not recipe:
        return
    for title, keys in SIDECAR_TABS:
        ref = sidecar_ref(recipe, keys)
        if not ref:
            continue
        path = resolve_sidecar(ref, base_dir)
        try:
            rows = sidecar_rows(path)
        except Exception as exc:
            # Deliberately broad: one bad sidecar must cost its own tab and
            # nothing else -- not the report, not the sidecars after it.
            # Safe because sidecar_rows raises before any sheet is created.
            print(f"[WARN] {title} tab skipped -- cannot dump {ref}: {exc}",
                  file=sys.stderr)
            continue
        write_sidecar_tab(wb.create_sheet(title), rows)
