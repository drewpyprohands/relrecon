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


def write_sidecar_tab(ws, path: Path, text: str) -> None:
    """Write one sidecar dump: row 1 = path, row 2+ = file lines, column A."""
    from openpyxl.styles import Font

    mono = Font(name="Consolas", size=10)
    rows = [str(path.resolve()), *text.splitlines()]
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
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"[WARN] {title} tab skipped -- cannot read {ref}: {exc}",
                  file=sys.stderr)
            continue
        write_sidecar_tab(wb.create_sheet(title), path, text)
