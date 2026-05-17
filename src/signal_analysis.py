"""
Signal Analysis module for the relational matching framework.

Profiles source data to bootstrap normalization config:
- Top N tokens per column at Raw and Clean tiers
- Bigram and trigram frequency analysis
- Auto-detect column type (name, address, date, ID)
- Suggested stopwords from frequency distribution
- Suggested alias groups from variant detection
- Singleton token detection (appear exactly once -- potential typos)
- Near-duplicate token detection (edit distance 1-2 via RapidFuzz)
- Token position frequency (first/last/middle)
- Token length distribution
- Numeric token ratio
- Unicode profile per column
- Data quality summary

Uses normalize.py for all transformations. Single source of truth.
Polars-native operations used throughout per ADR-001.
"""

import json
import re
from pathlib import Path
from typing import Optional

import polars as pl

from normalize import clean
from normalize import profile_column as unicode_profile_column

# ---------------------------------------------------------------------------
# Column type detection
# ---------------------------------------------------------------------------

# Patterns for auto-detection
_DATE_PATTERNS = [
    re.compile(r'^\d{4}-\d{2}-\d{2}'),           # ISO date
    re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}'),     # US date
    re.compile(r'^\d{1,2}-\d{1,2}-\d{2,4}'),     # Dash date
]
_ID_PATTERN = re.compile(r'^[A-Za-z]?\d{3,}$')    # Alphanumeric ID-like
_NAME_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "group", "pty", "gmbh", "sa", "ag"}
_ADDR_TOKENS = {"street", "st", "avenue", "ave", "blvd", "boulevard", "drive", "dr",
                "road", "rd", "lane", "ln", "suite", "ste", "floor", "fl", "pkwy"}


def select_columns(df: pl.DataFrame, columns_arg: str | None) -> tuple[list[str], str]:
    """Resolve --columns arg to a column list. Returns (columns, mode_message)."""
    if columns_arg and columns_arg.lower() == "auto":
        columns = [c for c in df.columns if detect_column_type(df[c]) in ("name", "address")]
        if not columns:
            columns = [c for c in df.columns if df[c].dtype == pl.String]
        return columns, f"Auto-selected columns: {', '.join(columns)}"
    elif columns_arg:
        columns = [c.strip() for c in columns_arg.split(",")]
        missing = [c for c in columns if c not in df.columns]
        if missing:
            raise ValueError(
                f"Columns not found: {', '.join(missing)}\n"
                f"Available: {', '.join(df.columns)}"
            )
        return columns, ""
    else:
        columns = [c for c in df.columns if df[c].dtype == pl.String]
        return columns, f"Analyzing all string columns: {', '.join(columns)}"


def detect_column_type(series: pl.Series) -> str:
    """Auto-detect column type based on value patterns.

    Returns: 'name', 'address', 'date', 'id', or 'freetext'
    """
    sample = series.drop_nulls().head(100).to_list()
    if not sample:
        return "freetext"

    date_hits = 0
    id_hits = 0
    name_hits = 0
    addr_hits = 0

    for val in sample:
        s = str(val).strip()
        if not s:
            continue

        # Date check
        for pat in _DATE_PATTERNS:
            if pat.match(s):
                date_hits += 1
                break

        # ID check
        if _ID_PATTERN.match(s):
            id_hits += 1

        # Name/address check via tokens
        tokens = {t.lower().rstrip(".,") for t in s.split()}
        if tokens & _NAME_SUFFIXES:
            name_hits += 1
        if tokens & _ADDR_TOKENS:
            addr_hits += 1

    total = len(sample)
    threshold = 0.3  # 30% of sample must match

    if date_hits / total > threshold:
        return "date"
    if id_hits / total > threshold:
        return "id"
    if addr_hits / total > threshold:
        return "address"
    if name_hits / total > threshold:
        return "name"

    # Heuristic: high uniqueness + short values = ID
    unique_ratio = series.n_unique() / series.len() if series.len() > 0 else 0
    if unique_ratio > 0.9:
        return "id"

    return "freetext"


# ---------------------------------------------------------------------------
# Internal helpers: Polars-native tokenization
# ---------------------------------------------------------------------------

def _tokenize_series(series: pl.Series, tier: str = "raw") -> pl.DataFrame:
    """Split a series into tokens at the given tier. Returns DataFrame with 'tok' column.

    Polars-native: no Python lambdas or map_elements.
    """
    s = series.drop_nulls().cast(pl.String)
    if tier == "clean":
        s = s.str.to_lowercase().str.replace_all(r'[,.;:]', '')
    return s.str.split(" ").explode().to_frame("tok").filter(pl.col("tok") != "")


def _tokenize_with_index(series: pl.Series, tier: str = "raw") -> pl.DataFrame:
    """Split series into tokens preserving row index and position.

    Returns DataFrame with columns: row_idx, pos, tok, total_tokens.
    Polars-native.
    """
    s = series.drop_nulls().cast(pl.String)
    if tier == "clean":
        s = s.str.to_lowercase().str.replace_all(r'[,.;:]', '')

    # Split and track row index
    df = s.to_frame("val").with_row_index("row_idx")
    df = df.with_columns(pl.col("val").str.split(" ").alias("tokens"))
    df = df.with_columns(pl.col("tokens").list.len().alias("total_tokens"))
    df = df.explode("tokens").rename({"tokens": "tok"})
    df = df.filter(pl.col("tok") != "")

    # Add position within each row
    df = df.with_columns(
        pl.col("tok").cum_count().over("row_idx").alias("pos")
    )
    return df.select("row_idx", "pos", "tok", "total_tokens")


# ---------------------------------------------------------------------------
# Token analysis
# ---------------------------------------------------------------------------

def top_tokens(series: pl.Series, tier: str = "raw", n: int = 50) -> list[tuple[str, int]]:
    """Extract top N tokens from a column at a given normalization tier.

    Polars-native: split, explode, group_by, sort.
    """
    if tier not in ("raw", "clean"):
        raise ValueError(f"top_tokens only supports 'raw' or 'clean' tiers, got '{tier}'")

    df = _tokenize_series(series, tier)
    counts = df.group_by("tok").len().sort("len", descending=True).head(n)
    return [(row[0], row[1]) for row in counts.iter_rows()]


def top_ngrams(series: pl.Series, tier: str = "raw", n_gram: int = 2,
              n: int = 50) -> list[tuple[str, int]]:
    """Extract top N bigrams or trigrams from a column.

    Uses Polars list operations where possible, with a targeted Python
    loop for n-gram window construction (no Polars-native sliding window).
    """
    if tier not in ("raw", "clean"):
        raise ValueError(f"top_ngrams only supports 'raw' or 'clean' tiers, got '{tier}'")

    s = series.drop_nulls().cast(pl.String)
    if tier == "clean":
        s = s.str.to_lowercase().str.replace_all(r'[,.;:]', '')

    # Build ngrams per row using list operations
    # Get token lists, filter empties, then build ngrams
    token_lists = (
        s.str.split(" ")
        .list.eval(pl.element().filter(pl.element() != ""))
        .to_list()
    )

    # N-gram construction (targeted Python -- no Polars sliding window primitive)
    ngram_counts: dict[str, int] = {}
    for tokens in token_lists:
        for i in range(len(tokens) - n_gram + 1):
            gram = " ".join(tokens[i:i + n_gram])
            ngram_counts[gram] = ngram_counts.get(gram, 0) + 1

    sorted_grams = sorted(ngram_counts.items(), key=lambda x: -x[1])
    return sorted_grams[:n]


# ---------------------------------------------------------------------------
# Singleton tokens (appear exactly once -- potential typos/errors)
# ---------------------------------------------------------------------------

def singleton_tokens(series: pl.Series, tier: str = "clean",
                     n: int = 50) -> list[tuple[str, int]]:
    """Find tokens that appear exactly once in a column.

    Singletons are potential typos, data entry errors, or unique identifiers.
    Returns up to n singleton tokens sorted alphabetically.
    Polars-native.
    """
    df = _tokenize_series(series, tier)
    singletons = (
        df.group_by("tok").len()
        .filter(pl.col("len") == 1)
        .sort("tok")
        .head(n)
    )
    return [(row[0], row[1]) for row in singletons.iter_rows()]


# ---------------------------------------------------------------------------
# Near-duplicate tokens (edit distance 1-2 -- probable typos)
# ---------------------------------------------------------------------------

def near_duplicate_tokens(series: pl.Series, tier: str = "clean",
                          max_tokens: int = 200, threshold: int = 85,
                          n: int = 30) -> list[dict]:
    """Find token pairs with high similarity (edit distance 1-2).

    Uses RapidFuzz for efficient pairwise comparison on top-K unique tokens.
    Only compares tokens of similar length (within 2 chars) for performance.

    Returns list of {"token1": str, "token2": str, "similarity": int,
                     "count1": int, "count2": int} dicts.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return []  # Graceful skip if RapidFuzz not installed

    df = _tokenize_series(series, tier)
    token_counts = (
        df.group_by("tok").len()
        .sort("len", descending=True)
        .head(max_tokens)
    )

    tokens = token_counts["tok"].to_list()
    counts = {row[0]: row[1] for row in token_counts.iter_rows()}

    if len(tokens) < 2:
        return []

    # Group by length for efficient comparison (only compare similar lengths)
    by_len: dict[int, list[str]] = {}
    for t in tokens:
        tlen = len(t)
        by_len.setdefault(tlen, []).append(t)

    pairs = []
    seen = set()
    sorted_lens = sorted(by_len.keys())

    for i, length in enumerate(sorted_lens):
        group = by_len[length]
        # Compare within same length
        for a_idx in range(len(group)):
            for b_idx in range(a_idx + 1, len(group)):
                a, b = group[a_idx], group[b_idx]
                sim = fuzz.ratio(a, b)
                if sim >= threshold:
                    key = (min(a, b), max(a, b))
                    if key not in seen:
                        seen.add(key)
                        pairs.append({
                            "token1": a, "token2": b,
                            "similarity": sim,
                            "count1": counts[a], "count2": counts[b],
                        })
        # Compare with adjacent length (+1)
        if i + 1 < len(sorted_lens) and sorted_lens[i + 1] == length + 1:
            adj_group = by_len[length + 1]
            for a in group:
                for b in adj_group:
                    sim = fuzz.ratio(a, b)
                    if sim >= threshold:
                        key = (min(a, b), max(a, b))
                        if key not in seen:
                            seen.add(key)
                            pairs.append({
                                "token1": a, "token2": b,
                                "similarity": sim,
                                "count1": counts[a], "count2": counts[b],
                            })
        # Compare with length +2
        if i + 1 < len(sorted_lens):
            for j in range(i + 1, len(sorted_lens)):
                if sorted_lens[j] > length + 2:
                    break
                if sorted_lens[j] == length + 2:
                    adj_group = by_len[sorted_lens[j]]
                    for a in group:
                        for b in adj_group:
                            sim = fuzz.ratio(a, b)
                            if sim >= threshold:
                                key = (min(a, b), max(a, b))
                                if key not in seen:
                                    seen.add(key)
                                    pairs.append({
                                        "token1": a, "token2": b,
                                        "similarity": sim,
                                        "count1": counts[a],
                                        "count2": counts[b],
                                    })

    pairs.sort(key=lambda x: -x["similarity"])
    return pairs[:n]


# ---------------------------------------------------------------------------
# Token position frequency
# ---------------------------------------------------------------------------

def token_position_frequency(series: pl.Series, tier: str = "clean",
                             n: int = 30) -> dict:
    """Analyze where tokens appear: first word, last word, or middle.

    Returns {"first": [(token, count), ...], "last": [...], "middle": [...]}.
    Polars-native.
    """
    df = _tokenize_with_index(series, tier)

    if df.height == 0:
        return {"first": [], "last": [], "middle": []}

    # First token: pos == 1
    first = (
        df.filter(pl.col("pos") == 1)
        .group_by("tok").len()
        .sort("len", descending=True)
        .head(n)
    )

    # Last token: pos == total_tokens
    last = (
        df.filter(pl.col("pos") == pl.col("total_tokens"))
        .group_by("tok").len()
        .sort("len", descending=True)
        .head(n)
    )

    # Middle: everything else (multi-token rows only)
    middle = (
        df.filter(
            (pl.col("pos") > 1) &
            (pl.col("pos") < pl.col("total_tokens")) &
            (pl.col("total_tokens") > 2)
        )
        .group_by("tok").len()
        .sort("len", descending=True)
        .head(n)
    )

    return {
        "first": [(r[0], r[1]) for r in first.iter_rows()],
        "last": [(r[0], r[1]) for r in last.iter_rows()],
        "middle": [(r[0], r[1]) for r in middle.iter_rows()],
    }


# ---------------------------------------------------------------------------
# Token length distribution
# ---------------------------------------------------------------------------

def token_length_distribution(series: pl.Series, tier: str = "clean") -> dict:
    """Compute token length statistics and histogram.

    Returns {"min": int, "max": int, "mean": float, "median": float,
             "histogram": [(length, count), ...]}.
    Polars-native.
    """
    df = _tokenize_series(series, tier)

    if df.height == 0:
        return {"min": 0, "max": 0, "mean": 0.0, "median": 0.0, "histogram": []}

    df = df.with_columns(pl.col("tok").str.len_chars().alias("tok_len"))

    stats = df.select(
        pl.col("tok_len").min().alias("min"),
        pl.col("tok_len").max().alias("max"),
        pl.col("tok_len").mean().alias("mean"),
        pl.col("tok_len").median().alias("median"),
    ).row(0, named=True)

    histogram = (
        df.group_by("tok_len").len()
        .sort("tok_len")
    )

    return {
        "min": int(stats["min"]),
        "max": int(stats["max"]),
        "mean": round(float(stats["mean"]), 2),
        "median": float(stats["median"]),
        "histogram": [(r[0], r[1]) for r in histogram.iter_rows()],
    }


# ---------------------------------------------------------------------------
# Numeric token ratio
# ---------------------------------------------------------------------------

def numeric_token_ratio(series: pl.Series, tier: str = "raw") -> dict:
    """Compute ratio of numeric tokens vs word tokens.

    Returns {"total_tokens": int, "numeric": int, "alpha": int,
             "mixed": int, "numeric_pct": float}.
    Polars-native.
    """
    df = _tokenize_series(series, tier)

    if df.height == 0:
        return {"total_tokens": 0, "numeric": 0, "alpha": 0,
                "mixed": 0, "numeric_pct": 0.0}

    # Classify tokens
    classified = df.with_columns(
        pl.col("tok").str.contains(r'^\d+\.?\d*$').alias("is_numeric"),
        pl.col("tok").str.contains(r'^[A-Za-z]+$').alias("is_alpha"),
    )

    total = classified.height
    numeric = classified.filter(pl.col("is_numeric")).height
    alpha = classified.filter(pl.col("is_alpha")).height
    mixed = total - numeric - alpha

    return {
        "total_tokens": total,
        "numeric": numeric,
        "alpha": alpha,
        "mixed": mixed,
        "numeric_pct": round(numeric / total * 100, 1) if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Stopword suggestion (Polars-native)
# ---------------------------------------------------------------------------

def suggest_stopwords(series: pl.Series, col_type: str = "name",
                      threshold: float = 0.15, n: int = 20) -> list:
    """Suggest stopwords based on token frequency distribution.

    Tokens appearing in > threshold fraction of non-null rows are candidates.
    Polars-native: uses list.unique() instead of map_elements.
    """
    total_rows = series.drop_nulls().len()
    if total_rows == 0:
        return []

    # Clean, split, deduplicate within each row (Polars-native)
    cleaned = series.drop_nulls().cast(pl.String).str.to_lowercase().str.replace_all(r'[,.;:]', '')
    per_row_unique = (
        cleaned.str.split(" ")
        .list.unique()
        .list.eval(pl.element().filter(pl.element() != ""))
    )

    # Explode, count across rows
    row_counts = (
        per_row_unique.explode()
        .to_frame("tok")
        .filter(pl.col("tok").is_not_null())
        .group_by("tok").len()
        .sort("len", descending=True)
    )

    # Known stopword candidates by type
    known_stopwords = {
        "name": {"inc", "llc", "ltd", "corp", "co", "the", "of", "and", "group",
                 "pty", "gmbh", "sa", "ag", "limited", "incorporated", "corporation"},
        "address": {"suite", "ste", "floor", "fl", "unit", "apt", "building", "bldg",
                    "room", "rm", "dept", "level"},
    }
    known = known_stopwords.get(col_type, set())

    suggestions = []
    for row in row_counts.head(n * 3).iter_rows():
        token, count = row[0], row[1]
        freq = count / total_rows
        if freq >= threshold or token in known:
            suggestions.append({
                "token": token,
                "frequency": round(freq, 3),
                "count": count,
                "known": token in known,
            })
        if len(suggestions) >= n:
            break

    return suggestions


# ---------------------------------------------------------------------------
# Alias suggestion (Polars-native where possible)
# ---------------------------------------------------------------------------

def _alias_group_key(token: str) -> str:
    """Strip all non-alnum for grouping (O'Brien/OBrien -> obrien)."""
    return re.sub(r'[^a-z0-9]', '', token.lower())


def suggest_aliases(series: pl.Series, n: int = 30) -> list:
    """Find punctuation variants (O'Brien/OBrien, AT&T/ATT, Co-Op/Coop).

    Uses Polars for tokenization and counting, Python for group key logic
    (regex per token has no Polars equivalent).
    """
    # Polars-native tokenization and counting
    df = _tokenize_series(series, "raw")
    token_counts = df.group_by("tok").len()

    # Group by alias key (Python -- no Polars regex substitution per token)
    groups: dict[str, list[tuple[str, int]]] = {}
    for token, count in token_counts.iter_rows():
        key = _alias_group_key(token)
        if not key:
            continue
        groups.setdefault(key, []).append((token, count))

    # Only suggest groups with multiple variants
    aliases = []
    for canonical, variants in groups.items():
        if len(variants) > 1:
            total = sum(c for _, c in variants)
            aliases.append({
                "canonical": canonical,
                "variants": [{"raw": v, "count": c}
                             for v, c in sorted(variants, key=lambda x: -x[1])],
                "total_count": total,
            })

    aliases.sort(key=lambda x: -x["total_count"])
    return aliases[:n]


# ---------------------------------------------------------------------------
# Data quality summary
# ---------------------------------------------------------------------------

def data_quality_summary(df: pl.DataFrame, columns: Optional[list] = None) -> dict:
    """Generate data quality summary for selected columns.

    Polars-native aggregation.
    """
    if columns is None:
        columns = df.columns

    summary = {}
    total_rows = df.height

    for col in columns:
        if col not in df.columns:
            continue

        series = df[col]
        null_count = series.null_count()
        non_null = total_rows - null_count
        n_unique = series.n_unique()

        # Value length stats (for string columns) -- Polars-native
        lengths = None
        if series.dtype == pl.String:
            len_series = series.drop_nulls().cast(pl.String).str.len_chars()
            if len_series.len() > 0:
                stats = len_series.to_frame("l").select(
                    pl.col("l").min().alias("min"),
                    pl.col("l").max().alias("max"),
                    pl.col("l").mean().alias("mean"),
                ).row(0, named=True)
                lengths = {
                    "min": int(stats["min"]),
                    "max": int(stats["max"]),
                    "mean": round(float(stats["mean"]), 1),
                }

        # Numeric token ratio
        num_ratio = numeric_token_ratio(series)

        summary[col] = {
            "total_rows": total_rows,
            "null_count": null_count,
            "null_pct": round(null_count / total_rows * 100, 1) if total_rows > 0 else 0.0,
            "non_null": non_null,
            "unique_count": n_unique,
            "unique_pct": round(n_unique / total_rows * 100, 1) if total_rows > 0 else 0.0,
            "duplicate_count": total_rows - n_unique,
            "lengths": lengths,
            "numeric_token_pct": num_ratio["numeric_pct"],
        }

    return summary


# ---------------------------------------------------------------------------
# Full analysis pipeline
# ---------------------------------------------------------------------------

def analyze_column(series: pl.Series, col_name: str,
                   col_type: Optional[str] = None,
                   unicode_mode: str = "profile_only",
                   top_n: int = 30) -> dict:
    """Run full signal analysis on a single column.

    Includes token analysis (topk, bigrams, trigrams), singletons,
    near-duplicates, position frequency, length distribution,
    numeric ratio, stopwords, aliases and unicode profiling.
    """
    if col_type is None:
        col_type = detect_column_type(series)

    result = {
        "column": col_name,
        "detected_type": col_type,
        "top_tokens_raw": top_tokens(series, tier="raw", n=top_n),
        "top_tokens_clean": top_tokens(series, tier="clean", n=top_n),
        "bigrams_raw": top_ngrams(series, tier="raw", n_gram=2, n=top_n),
        "bigrams_clean": top_ngrams(series, tier="clean", n_gram=2, n=top_n),
        "trigrams_raw": top_ngrams(series, tier="raw", n_gram=3, n=top_n),
        "trigrams_clean": top_ngrams(series, tier="clean", n_gram=3, n=top_n),
        "singletons": singleton_tokens(series, tier="clean", n=top_n),
        "near_duplicates": near_duplicate_tokens(series, tier="clean",
                                                  max_tokens=200, n=top_n),
        "token_positions": token_position_frequency(series, tier="clean", n=top_n),
        "token_lengths": token_length_distribution(series, tier="clean"),
        "numeric_ratio": numeric_token_ratio(series),
        "suggested_stopwords": suggest_stopwords(series, col_type=col_type),
        "suggested_aliases": suggest_aliases(series),
    }

    if unicode_mode == "profile_only":
        result["unicode_profile"] = unicode_profile_column(series)

    return result


def analyze_dataset(df: pl.DataFrame, columns: list,
                    col_types: Optional[dict] = None,
                    unicode_mode: str = "profile_only",
                    output_dir: Optional[str] = None) -> dict:
    """Run signal analysis on multiple columns of a dataset.

    Returns dict with per-column analysis + data quality summary.
    """
    col_types = col_types or {}

    results = {
        "data_quality": data_quality_summary(df, columns),
        "columns": {},
        "_raw_series": {},
    }

    all_stopwords = {}  # Keyed by column type: {"name": set(), "address": set(), ...}
    all_aliases = {}

    for col in columns:
        if col not in df.columns:
            continue

        col_type = col_types.get(col)
        analysis = analyze_column(df[col], col, col_type=col_type,
                                  unicode_mode=unicode_mode)
        results["columns"][col] = analysis
        results["_raw_series"][col] = df[col].to_list()

        # Aggregate stopwords by type (known always included, others need 0.2+ frequency)
        col_type = analysis["detected_type"]
        if col_type not in all_stopwords:
            all_stopwords[col_type] = set()
        for sw in analysis["suggested_stopwords"]:
            if sw["known"] or sw["frequency"] >= 0.2:
                all_stopwords[col_type].add(sw["token"])

        # Aggregate aliases ({"variant_clean": "canonical_stripped"} for normalized())
        for alias in analysis["suggested_aliases"]:
            if len(alias["variants"]) > 1:
                canonical = alias["canonical"]
                for variant in alias["variants"]:
                    variant_clean = clean(variant["raw"])
                    if variant_clean != canonical:
                        all_aliases[variant_clean] = canonical

    results["aggregated_stopwords"] = {k: sorted(v) for k, v in all_stopwords.items()}
    results["aggregated_aliases"] = all_aliases

    # Write config files if output_dir specified
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        with open(out / "stopwords.json", "w") as f:
            json.dump({k: sorted(v) for k, v in all_stopwords.items()}, f, indent=2)

        with open(out / "aliases.json", "w") as f:
            json.dump(all_aliases, f, indent=2, ensure_ascii=False)

    return results
