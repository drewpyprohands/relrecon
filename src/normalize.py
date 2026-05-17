"""
Shared normalization module for the relational matching framework.

Provides three normalization tiers (Raw, Clean, Normalized) and
unicode profiling/normalization. Used by both signal analysis and
the matching engine. Single source of truth.

Unicode handling is configurable: normalize | profile_only | skip
"""

import bisect
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Unicode range classification
# ---------------------------------------------------------------------------

_RANGE_CACHE: Optional[dict] = None


def _load_ranges(path: Optional[str] = None) -> dict:
    """Load unicode range map from JSON config."""
    global _RANGE_CACHE
    use_cache = path is None
    if use_cache and _RANGE_CACHE is not None:
        return _RANGE_CACHE

    if path is None:
        path = str(Path(__file__).parent.parent / "config" / "unicode_ranges.json")

    with open(path) as f:
        data = json.load(f)

    # Remove comment keys
    ranges = {k: v for k, v in data.items() if not k.startswith("_")}

    # Build a flat sorted list for bisect lookup
    flat = []
    for bucket, pairs in ranges.items():
        for start, end in pairs:
            flat.append((start, end, bucket))
    flat.sort(key=lambda x: x[0])

    result = {"buckets": ranges, "flat": flat}
    if use_cache:
        _RANGE_CACHE = result
    return result


def classify_codepoint(cp: int, ranges: Optional[dict] = None) -> str:
    """Classify a single code point into a unicode bucket.

    Uses bisect for O(log n) lookup against sorted range list.
    Returns the bucket name (e.g., 'ascii_alnum', 'latin', 'cjk')
    or 'unknown' if no range matches.
    """
    if ranges is None:
        ranges = _load_ranges()

    flat = ranges["flat"]
    # bisect to find the rightmost range whose start <= cp
    idx = bisect.bisect_right(flat, (cp, float('inf'), '')) - 1
    if idx >= 0:
        start, end, bucket = flat[idx]
        if start <= cp <= end:
            return bucket
    return "unknown"


def profile_string(value: str, ranges: Optional[dict] = None) -> dict:
    """Profile a string by unicode bucket.

    Returns a dict of bucket_name -> count, plus 'total' and 'unknown_pct'.
    """
    if ranges is None:
        ranges = _load_ranges()

    counts = {}
    total = 0
    for ch in value:
        bucket = classify_codepoint(ord(ch), ranges)
        counts[bucket] = counts.get(bucket, 0) + 1
        total += 1

    unknown = counts.get("unknown", 0)
    counts["_total"] = total
    counts["_unknown_pct"] = round(unknown / total * 100, 1) if total > 0 else 0.0
    return counts


def profile_column(series, ranges: Optional[dict] = None) -> dict:
    """Aggregate unicode profile for a column of values.

    Accepts a Polars Series (calls .to_list()) or any iterable of strings.
    Returns summary stats: per-bucket totals, % of cells with unknown chars,
    flagged cells (high unknown %, mixed scripts).

    Optimized: uses Polars to filter ASCII-only rows and only runs
    per-character classification on non-ASCII cells.
    """
    if ranges is None:
        ranges = _load_ranges()

    import polars as pl

    # Normalize input to Polars Series
    if not isinstance(series, pl.Series):
        series = pl.Series(list(series))
    s = series.cast(pl.String).fill_null("").str.strip_chars()

    # Count non-empty cells (after stripping whitespace)
    non_empty = s.str.len_chars() > 0
    total_cells = int(non_empty.sum())
    if total_cells == 0:
        return {
            "total_cells": 0, "bucket_totals": {},
            "cells_with_unknown": 0, "cells_with_unknown_pct": 0.0,
            "mixed_script_cells": 0, "flagged_indices": [],
        }

    # Fast path: count ASCII chars in bulk via Polars
    # Total chars across all non-empty cells
    char_lens = s.str.len_chars()
    int(char_lens.filter(non_empty).sum())

    # Count ASCII alnum and punct/space chars using Polars regex
    alnum_counts = s.str.count_matches(r'[0-9A-Za-z]')
    punct_counts = s.str.count_matches(r'[\t\n\r \x0b\x0c!-/:-@\[-`{-~]')
    total_alnum = int(alnum_counts.filter(non_empty).sum())
    total_punct = int(punct_counts.filter(non_empty).sum())
    total_alnum + total_punct

    bucket_totals = {}
    if total_alnum > 0:
        bucket_totals["ascii_alnum"] = total_alnum
    if total_punct > 0:
        bucket_totals["ascii_punct_space"] = total_punct

    cells_with_unknown = 0
    mixed_script_cells = 0
    flagged_set = set()

    # Only process non-ASCII cells (the expensive path)
    non_ascii_chars = char_lens - alnum_counts - punct_counts
    has_non_ascii = non_empty & (non_ascii_chars > 0)
    non_ascii_count = int(has_non_ascii.sum())

    if non_ascii_count > 0:
        # Get indices and values of non-ASCII rows
        idx_series = pl.arange(0, s.len(), eager=True).filter(has_non_ascii)
        val_series = s.filter(has_non_ascii)

        for orig_idx, val in zip(idx_series.to_list(), val_series.to_list(), strict=False):
            if not val:
                continue
            profile = profile_string(val, ranges)

            for k, v in profile.items():
                if k.startswith("_") or k in ("ascii_alnum", "ascii_punct_space"):
                    continue
                bucket_totals[k] = bucket_totals.get(k, 0) + v

            if profile.get("unknown", 0) > 0:
                cells_with_unknown += 1
            if profile["_unknown_pct"] > 20:
                flagged_set.add(orig_idx)

            scripts = [
                k for k, v in profile.items()
                if not k.startswith("_") and k not in ("ascii_alnum", "ascii_punct_space", "common_unicode", "unknown") and v > 0
            ]
            if len(scripts) > 1:
                mixed_script_cells += 1
                flagged_set.add(orig_idx)

    return {
        "total_cells": total_cells,
        "bucket_totals": bucket_totals,
        "cells_with_unknown": cells_with_unknown,
        "cells_with_unknown_pct": round(cells_with_unknown / total_cells * 100, 1) if total_cells > 0 else 0.0,
        "mixed_script_cells": mixed_script_cells,
        "flagged_indices": sorted(flagged_set)[:100],
    }


# ---------------------------------------------------------------------------
# Unicode normalization (when mode='normalize')
# ---------------------------------------------------------------------------

def normalize_unicode(value: str) -> str:
    """Normalize problematic unicode in a string via NFKC.

    Handles:
    - Fullwidth characters -> ASCII equivalents
    - Compatibility decomposition (ligatures, etc.)

    Does NOT strip combining marks (accents are preserved).
    Does NOT remove unknown codepoints (emoji, symbols). That's a
    recipe-level decision handled by signal analysis + profile_only mode.
    """
    # NFKC handles fullwidth -> ASCII and compatibility forms
    value = unicodedata.normalize("NFKC", value)
    return value


# ---------------------------------------------------------------------------
# Alias pre-compilation for performance at scale
# ---------------------------------------------------------------------------

def compile_aliases(aliases: dict) -> list:
    """Pre-compile alias regex patterns for use in normalized().

    Returns a list of (compiled_pattern, replacement) tuples.
    Call once, pass result to normalized() via compiled_aliases param.
    """
    compiled = []
    for original, replacement in aliases.items():
        pattern = re.compile(r'\b' + re.escape(original.lower()) + r'\b')
        compiled.append((pattern, replacement.lower()))
    return compiled


# ---------------------------------------------------------------------------
# Normalization tiers
# ---------------------------------------------------------------------------

def raw(value: str) -> str:
    """Raw tier: no transformation. Returns value as-is."""
    return str(value) if value is not None else ""


def clean(value: str) -> str:
    """Clean tier: lowercase, strip whitespace, remove punctuation.

    Removes all commas, periods, semicolons, and colons from the string
    so that formatting differences (e.g., 'Vanteon Systems, Inc.' vs
    'VANTEON SYSTEMS INC') do not block exact matching.
    Does NOT include unicode normalization. That's a separate step.
    """
    if value is None:
        return ""
    s = str(value).strip().lower()
    # Remove punctuation (commas, periods, semicolons, colons)
    s = re.sub(r'[,.;:]+', '', s)
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def normalized(value: str, aliases: Optional[dict | list] = None,
               stopwords: Optional[set | list] = None) -> str:
    """Normalized tier: clean + alias replacement + stopword removal.

    Used for addresses only, never for names.
    Accepts aliases as a dict (compiled on the fly) or a pre-compiled list
    from compile_aliases() for performance at scale.
    Accepts stopwords as a list or pre-built set/frozenset.
    """
    s = clean(value)

    if aliases:
        if isinstance(aliases, dict):
            # On-the-fly compilation (convenience, slower in loops)
            for original, replacement in aliases.items():
                pattern = r'\b' + re.escape(original.lower()) + r'\b'
                s = re.sub(pattern, replacement.lower(), s)
        else:
            # Pre-compiled patterns from compile_aliases()
            for pattern, replacement in aliases:
                s = pattern.sub(replacement, s)

    if stopwords:
        # .lower() on both sides is intentional. Stopword config may have mixed case
        sw_set = stopwords if isinstance(stopwords, (set, frozenset)) else {sw.lower() for sw in stopwords}
        tokens = s.split()
        tokens = [t for t in tokens if t.lower() not in sw_set]
        s = " ".join(tokens)

    # Re-collapse spaces after removals
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ---------------------------------------------------------------------------
# Convenience: apply a tier by name
# ---------------------------------------------------------------------------

def apply_tier(value: str, tier: str, aliases: Optional[dict | list] = None,
               stopwords: Optional[set | list] = None, unicode_mode: str = "skip") -> str:
    """Apply a normalization tier by name.

    Args:
        value: Input string
        tier: 'raw', 'clean', or 'normalized'
        aliases: Alias replacement dict (for normalized tier)
        stopwords: Stopword list (for normalized tier)
        unicode_mode: 'normalize' | 'profile_only' | 'skip'

    Returns:
        Normalized string
    """
    # Apply unicode normalization first if enabled
    if unicode_mode == "normalize":
        value = normalize_unicode(value)

    if tier == "raw":
        return raw(value)
    elif tier == "clean":
        return clean(value)
    elif tier == "normalized":
        return normalized(value, aliases, stopwords)
    else:
        raise ValueError(f"Unknown tier: {tier}. Must be 'raw', 'clean', or 'normalized'.")
