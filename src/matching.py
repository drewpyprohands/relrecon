"""
Core matching engine. ADR Option C aligned.

Uses Polars for all data ops (joins, filtering, expressions).
Uses RapidFuzz process.cdist for batch fuzzy matching (chunked to bound memory).
No Python row-level loops for matching. Vectorized throughout.
Address scoring uses RapidFuzz batch ops where possible.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
from rapidfuzz import fuzz as rfuzz
from rapidfuzz import process as rprocess

from address import score_address_multi_tier
from normalize import clean, normalized

# Max source rows per cdist chunk. Controls peak memory:
# chunk_size x dest_rows x 4 bytes (float32).
# 1000 x 3.3M = ~12.5 GB. Adjust down for constrained environments.
_CDIST_CHUNK_SIZE = 1000


def _next_dst_suffix(current: str) -> str:
    """Increment a _dst suffix: _dst -> _dst2 -> _dst3 -> _dst4 etc."""
    if current == "_dst":
        return "_dst2"
    n = int(current.removeprefix("_dst"))
    return f"_dst{n + 1}"

# ---------------------------------------------------------------------------
# Date gate (Polars native)
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S%.f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def apply_date_gate(df: pl.DataFrame, field: str, max_age_years: int) -> pl.DataFrame:
    """Filter records within max_age_years. Pure Polars, no Python loops."""
    cutoff = datetime.now() - timedelta(days=max_age_years * 365)

    for fmt in _DATE_FORMATS:
        try:
            result = df.with_columns(
                pl.col(field).cast(pl.String)
                .str.strptime(pl.Date, fmt, strict=False)
                .alias("_date_gate")
            ).filter(
                pl.col("_date_gate").is_not_null() &
                (pl.col("_date_gate") >= cutoff.date())
            ).drop("_date_gate")

            if result.height > 0:
                return result
        except Exception:  # noqa: S112
            continue

    # Fallback: string comparison (ISO dates sort correctly)
    return df.filter(pl.col(field).cast(pl.String) >= cutoff.strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Name matching (Polars native joins, no Python loops)
# ---------------------------------------------------------------------------

def _normalized_column(df: pl.DataFrame, col: str, alias: str,
                       aliases: dict = None, stopwords: list = None) -> pl.DataFrame:
    """Add a normalized version of a column (clean + aliases + stopwords)."""
    def _norm(val):
        return normalized(val, aliases=aliases, stopwords=stopwords)
    return df.with_columns(
        pl.col(col).cast(pl.String).map_elements(
            _norm, return_dtype=pl.String
        ).alias(alias)
    )


def _clean_column(df: pl.DataFrame, col: str, alias: str) -> pl.DataFrame:
    """Add a cleaned version of a column using normalize.clean().

    Uses the shared normalization function to ensure consistency
    between signal analysis and matching (per README requirement).
    Polars map_elements is used here. Acceptable because this runs
    once per join setup, not per-row in a matching loop.
    """
    return df.with_columns(
        pl.col(col).cast(pl.String).map_elements(
            clean, return_dtype=pl.String
        ).alias(alias)
    )


def match_names_exact(source_df: pl.DataFrame, dest_df: pl.DataFrame,
                      src_field: str, dst_field: str,
                      tiers: list = None,
                      aliases: dict = None,
                      stopwords: list = None,
                      dedup_field: str = None,
                      exclude_self_key: str = None) -> pl.DataFrame:
    """Exact name matching via Polars joins. No Python loops.

    Tries tiers in order (default: raw, clean). Deduplicates by tier priority
    using dedup_field (defaults to src_field if not provided).
    If 'normalized' is in tiers, applies alias replacement + stopword removal
    using the provided aliases/stopwords (requires both to be effective).
    """
    dedup_key = dedup_field or src_field
    if tiers is None:
        tiers = ["raw", "clean"]

    results = []
    # Priority from position in recipe tier list (first = preferred)
    tier_priority = {t: i for i, t in enumerate(tiers)}

    for tier in tiers:
        if tier == "raw":
            src = source_df.with_columns(pl.col(src_field).cast(pl.String).alias("_match_key"))
            dst = dest_df.with_columns(pl.col(dst_field).cast(pl.String).alias("_match_key"))
        elif tier == "normalized":
            src = _normalized_column(source_df, src_field, "_match_key", aliases, stopwords)
            dst = _normalized_column(dest_df, dst_field, "_match_key", aliases, stopwords)
        else:
            src = _clean_column(source_df, src_field, "_match_key")
            dst = _clean_column(dest_df, dst_field, "_match_key")

        # Pick a suffix that won't collide with existing columns.
        # In multi-phase pipelines, _previous_matched may already carry
        # _dst columns from prior phases.
        suffix = "_dst"
        src_cols = set(src.columns)
        dst_cols_to_add = set(dst.columns) - {"_match_key"}
        while any((c + suffix) in src_cols for c in dst_cols_to_add if c in src_cols):
            suffix = _next_dst_suffix(suffix)

        matched = src.join(
            dst, on="_match_key", how="inner", suffix=suffix,
            maintain_order="right",  # preserve dest ordering for tie-breaker pre-sort
        )

        # Exclude self-matches for same-population matching
        if exclude_self_key and matched.height > 0:
            dst_key = exclude_self_key + suffix if exclude_self_key + suffix in matched.columns else None
            if dst_key and exclude_self_key in matched.columns:
                matched = matched.filter(
                    pl.col(exclude_self_key).cast(pl.String) != pl.col(dst_key).cast(pl.String)
                )

        if matched.height > 0:
            matched = matched.with_columns(
                pl.lit(tier).alias("match_tier"),
                pl.lit(tier_priority.get(tier, 99)).alias("_tier_priority"),
            )
            results.append(matched)

    if not results:
        return pl.DataFrame()

    combined = pl.concat(results, how="diagonal")

    # Dedup: keep highest priority tier per source record
    combined = (
        combined
        .sort("_tier_priority")
        .unique(subset=[dedup_key], keep="first")
        .drop([c for c in combined.columns if c.startswith("_")])
    )
    return combined


def match_names_fuzzy(source_df: pl.DataFrame, dest_df: pl.DataFrame,
                      src_field: str, dst_field: str,
                      tiers: list = None,
                      threshold: int = 80,
                      scorer: str = "token_sort_ratio",
                      aliases: dict = None,
                      stopwords: list = None,
                      dedup_field: str = None,
                      exclude_self_key: str = None) -> pl.DataFrame:
    """Fuzzy name matching via RapidFuzz cdist (chunked to bound memory).

    Tries tiers in order, deduplicates by tier priority (earlier wins).

    Returns matched DataFrame with name_score column (0-100).
    """
    dedup_key = dedup_field or src_field
    if tiers is None:
        tiers = ["raw", "clean"]

    # Resolve scorer function
    scorer_map = {
        "token_sort_ratio": rfuzz.token_sort_ratio,
        "token_set_ratio": rfuzz.token_set_ratio,
        "ratio": rfuzz.ratio,
        "partial_ratio": rfuzz.partial_ratio,
        "WRatio": rfuzz.WRatio,
    }
    scorer_fn = scorer_map.get(scorer, rfuzz.token_sort_ratio)

    # Priority from position in recipe tier list (first = preferred)
    tier_priority = {t: i for i, t in enumerate(tiers)}
    results = []

    for tier in tiers:
        # Build match keys for this tier (same logic as exact)
        if tier == "raw":
            src = source_df.with_columns(pl.col(src_field).cast(pl.String).alias("_match_key"))
            dst = dest_df.with_columns(pl.col(dst_field).cast(pl.String).alias("_match_key"))
        elif tier == "normalized":
            src = _normalized_column(source_df, src_field, "_match_key", aliases, stopwords)
            dst = _normalized_column(dest_df, dst_field, "_match_key", aliases, stopwords)
        else:  # clean
            src = _clean_column(source_df, src_field, "_match_key")
            dst = _clean_column(dest_df, dst_field, "_match_key")

        src_keys = src["_match_key"].cast(pl.String).fill_null("").to_list()
        dst_keys = dst["_match_key"].cast(pl.String).fill_null("").to_list()
        if not dst_keys or not src_keys:
            continue

        # Pre-extract self-match keys if needed (once, not per chunk)
        dst_self_keys = None
        if exclude_self_key:
            dst_self_keys = dst[exclude_self_key].cast(pl.String).fill_null("").to_numpy()

        # Chunk source rows to bound memory: chunk_size x M x 4 bytes
        # Default 1000 rows -- at 3.3M dest cols that's ~12.5 GB per chunk
        chunk_size = _CDIST_CHUNK_SIZE
        all_src_idxs = []
        all_dst_idxs = []
        all_scores = []

        for chunk_start in range(0, len(src_keys), chunk_size):
            chunk_end = min(chunk_start + chunk_size, len(src_keys))
            chunk_keys = src_keys[chunk_start:chunk_end]

            score_matrix = rprocess.cdist(
                chunk_keys, dst_keys,
                scorer=scorer_fn, score_cutoff=threshold,
                dtype=np.float32, workers=-1,
            )

            # Exclude self-matches for same-population matching
            if exclude_self_key and dst_self_keys is not None:
                src_self_chunk = (
                    src[exclude_self_key]
                    .cast(pl.String).fill_null("")
                    .to_numpy()[chunk_start:chunk_end]
                )
                self_mask = src_self_chunk[:, None] == dst_self_keys[None, :]
                score_matrix[self_mask] = 0.0

            best_dst_idxs_chunk = score_matrix.argmax(axis=1)
            best_scores_chunk = score_matrix[
                np.arange(len(chunk_keys)), best_dst_idxs_chunk
            ]

            # cdist sets below-threshold scores to 0
            mask = best_scores_chunk >= threshold
            if mask.any():
                # Offset source indices back to global positions
                src_hits = np.where(mask)[0] + chunk_start
                all_src_idxs.extend(src_hits.tolist())
                all_dst_idxs.extend(best_dst_idxs_chunk[mask].tolist())
                all_scores.extend(best_scores_chunk[mask].tolist())

            # Free chunk memory immediately
            del score_matrix

        if not all_src_idxs:
            continue

        src_idxs = all_src_idxs
        dst_idxs = all_dst_idxs
        scores = all_scores

        matched_src = src[src_idxs].drop("_match_key")
        matched_dst = dst[dst_idxs].drop("_match_key")

        # Use a suffix that won't collide with existing _dst columns
        # from prior multi-phase matches
        fsuffix = "_dst"
        src_col_set = set(matched_src.columns)
        while any((c + fsuffix) in src_col_set for c in matched_dst.columns if c in src_col_set):
            fsuffix = _next_dst_suffix(fsuffix)
        dst_renames = {}
        for col in matched_dst.columns:
            if col in matched_src.columns:
                dst_renames[col] = col + fsuffix
        if dst_renames:
            matched_dst = matched_dst.rename(dst_renames)

        matched = pl.concat([matched_src, matched_dst], how="horizontal")
        matched = matched.with_columns(
            pl.Series("name_score", scores),
            pl.lit(tier).alias("match_tier"),
            pl.lit(tier_priority.get(tier, 99)).alias("_tier_priority"),
        )

        results.append(matched)

    if not results:
        return pl.DataFrame()

    combined = pl.concat(results, how="diagonal")

    # Dedup: keep best tier per source record, then highest score within tier
    combined = (
        combined
        .sort(["_tier_priority", "name_score"], descending=[False, True])
        .unique(subset=[dedup_key], keep="first")
        .drop([c for c in combined.columns if c.startswith("_")])
    )
    return combined


# ---------------------------------------------------------------------------
# Tie-breaker helpers
# ---------------------------------------------------------------------------


def _tb_sort_key_expr(col_name: str, tie_breaker: dict) -> pl.Expr:
    """Build a Polars expression for tie-breaker sort key.

    Args:
        col_name: Column to build sort key from.
        tie_breaker: Config dict with 'strip_prefix' and 'order'.

    Returns:
        Polars expression aliased as '_tb_sort_key'.
    """
    strip_prefix = tie_breaker.get("strip_prefix", "")
    if strip_prefix == "alpha":
        # Strip leading letters, parse remainder as integer
        return (
            pl.col(col_name).cast(pl.String)
            .str.replace(r"^[A-Za-z]+", "")
            .str.strip_chars()
            .cast(pl.Int64, strict=False)
            .fill_null(2**63 - 1)
            .alias("_tb_sort_key")
        )
    else:
        expr = pl.col(col_name).cast(pl.String)
        if strip_prefix:
            # Strip prefix from start of string only (anchored)
            expr = expr.str.replace(r"^" + strip_prefix, "")
        return expr.alias("_tb_sort_key")


def _presort_by_tie_breaker(df: pl.DataFrame, tie_breaker: dict,
                            col_name: str) -> pl.DataFrame:
    """Pre-sort a DataFrame by tie-breaker column.

    Used to sort destination before joining so per-step dedup
    (unique keep='first') picks the tie-breaker winner.
    """
    tb_order = tie_breaker.get("order", "asc")
    return (
        df.with_columns(_tb_sort_key_expr(col_name, tie_breaker))
        .sort("_tb_sort_key", descending=(tb_order == "desc"))
        .drop("_tb_sort_key")
    )


# ---------------------------------------------------------------------------
# Address scoring (RapidFuzz batch ops)
# ---------------------------------------------------------------------------

def score_addresses_batch(matched_df: pl.DataFrame,
                          src_cols: list, dst_cols: list,
                          parser: str = "auto",
                          aliases: dict = None,
                          stopwords: list = None,
                          tiers: list = None,
                          street_weight: float = 0.6) -> pl.DataFrame:
    """Score address pairs using Phase 3 address module.

    Uses score_address_multi_tier which:
    - Builds variants and cross-compares (merged + all NxN field combos)
    - Parses via libpostal or built-in tokenizer for street name extraction
    - Applies normalization tiers (default: raw, clean, normalized)
    - Weights street name component (default 0.6 street + 0.4 full string)

    Supports N source and M destination address columns.
    Iterates over matched pairs (post-join, not N×M cartesian).
    """
    if tiers is None:
        tiers = ["raw", "clean", "normalized"]

    # Read each address column into a list
    src_val_lists = [matched_df[c].to_list() for c in src_cols]
    dst_val_lists = [matched_df[c].to_list() for c in dst_cols]
    n_rows = matched_df.height

    scores = []
    street_matches = []
    comparisons = []
    tiers_used = []

    for row_i in range(n_rows):
        src_addrs = [str(src_val_lists[c][row_i] or "") for c in range(len(src_cols))]
        dst_addrs = [str(dst_val_lists[c][row_i] or "") for c in range(len(dst_cols))]
        result = score_address_multi_tier(
            src_addrs, dst_addrs,
            tiers=tiers,
            parser=parser,
            aliases=aliases,
            stopwords=stopwords,
            street_weight=street_weight,
        )
        scores.append(result["best_score"])
        street_matches.append(result.get("street_match", False))
        comparisons.append(result.get("best_comparison", ""))
        tiers_used.append(result.get("tier_used", ""))

    return matched_df.with_columns(
        pl.Series("addr_score", scores),
        pl.Series("addr_street_match", street_matches),
        pl.Series("addr_comparison", comparisons),
        pl.Series("addr_tier", tiers_used),
    )


# ---------------------------------------------------------------------------
# Step filters (generic, replaces date_gate)
# ---------------------------------------------------------------------------


def _normalize_step_filters(step_config: dict) -> dict:
    """Convert legacy date_gate to generic filters list.

    If both date_gate and filters exist, date_gate is appended to filters.
    Returns a new dict (does not mutate the original).
    """
    if "date_gate" not in step_config:
        return step_config

    step_config = dict(step_config)  # shallow copy
    dg = step_config.pop("date_gate")
    filters = list(step_config.get("filters", []))
    filters.append({
        "field": dg["field"],
        "op": "max_age_years",
        "value": dg["max_age_years"],
        "applies_to": dg.get("applies_to", "destination"),
    })
    step_config["filters"] = filters
    return step_config


def _apply_step_filter(df: pl.DataFrame, filt: dict) -> pl.DataFrame:
    """Apply a single step filter to a DataFrame.

    Supports all population filter ops plus:
    - max_age_years: date recency filter (same as legacy date_gate)
    """
    field = filt["field"]
    op = filt["op"]

    if op == "max_age_years":
        return apply_date_gate(df, field, filt["value"])

    # Delegate to the standard filter DSL
    from recipe import build_filter_expr
    expr = build_filter_expr([{"field": field, "op": op,
                               "value": filt.get("value"),
                               "values": filt.get("values")}])
    return df.filter(expr)


# ---------------------------------------------------------------------------
# Single matching step
# ---------------------------------------------------------------------------

def run_matching_step(source_df: pl.DataFrame, dest_df: pl.DataFrame,
                      step_config: dict,
                      aliases: dict = None, stopwords: list = None,
                      dedup_field: str = None,
                      collect_rejections: bool = False) -> pl.DataFrame | tuple:
    """Execute one matching step. Returns (matched_df, rejections) when collect_rejections=True."""

    step_config = _normalize_step_filters(step_config)

    for filt in step_config.get("filters", []):
        applies_to = filt.get("applies_to", "destination")
        if applies_to in ("destination", "both"):
            dest_df = _apply_step_filter(dest_df, filt)
        if applies_to in ("source", "both"):
            source_df = _apply_step_filter(source_df, filt)
    if dest_df.height == 0:
        if collect_rejections:
            return pl.DataFrame(), {"filtered_by_step_filter": set()}
        return pl.DataFrame()

    mf = step_config["match_fields"][0]
    method = mf.get("method", "exact")

    # Detect same-population matching -- exclude self-matches
    _self_key = None
    if step_config.get("_same_pop") and dedup_field:
        _self_key = dedup_field

    if method == "fuzzy":
        matched = match_names_fuzzy(
            source_df, dest_df,
            mf["source"], mf["destination"],
            mf.get("tiers", ["raw", "clean"]),
            threshold=mf.get("threshold", 80),
            scorer=mf.get("scorer", "token_sort_ratio"),
            aliases=aliases,
            stopwords=stopwords,
            dedup_field=dedup_field,
            exclude_self_key=_self_key,
        )
    else:
        matched = match_names_exact(
            source_df, dest_df,
            mf["source"], mf["destination"],
            mf.get("tiers", ["raw", "clean"]),
            aliases=aliases,
            stopwords=stopwords,
            dedup_field=dedup_field,
            exclude_self_key=_self_key,
        )

    if matched.height == 0:
        if collect_rejections:
            return pl.DataFrame(), {}
        return pl.DataFrame()

    rejections = {}
    if "address_support" in step_config:
        ac = step_config["address_support"]
        src_cols = list(ac["source"])
        dst_cols = list(ac["destination"])

        # Prefer _dst suffixed columns (join disambiguation) over unsuffixed
        # (which is the source side when names collide).
        dst_cols = [c + "_dst" if c + "_dst" in matched.columns else c for c in dst_cols]

        # Score using Phase 3 address module (multi-tier, street weighting, cross-compare)
        matched = score_addresses_batch(
            matched, src_cols, dst_cols,
            parser=ac.get("parser", "auto"),
            aliases=aliases,
            stopwords=stopwords,
            tiers=ac.get("tiers"),
            street_weight=ac.get("weights", {}).get("street_name", 0.6),
        )

        # Street match gate: reject when street doesn't match (Issue #110)
        if ac.get("require_street_match") and "addr_street_match" in matched.columns:
            street_fail = matched.filter(~pl.col("addr_street_match"))
            if collect_rejections and dedup_field and dedup_field in matched.columns:
                if street_fail.height > 0:
                    rej = street_fail.select(dedup_field, "addr_score")
                    rejections["street_mismatch"] = dict(
                        zip(rej[dedup_field].cast(pl.String).to_list(),
                            rej["addr_score"].to_list(), strict=False)
                    )
            matched = matched.filter(pl.col("addr_street_match"))

        if "threshold" in ac and "addr_score" in matched.columns:
            cutoff = ac["threshold"]
            if collect_rejections and dedup_field and dedup_field in matched.columns:
                below = matched.filter(pl.col("addr_score") < cutoff)
                if below.height > 0:
                    rej = below.select(dedup_field, "addr_score")
                    rejections["addr_below_threshold"] = dict(
                        zip(rej[dedup_field].cast(pl.String).to_list(),
                            rej["addr_score"].to_list(), strict=False)
                    )
            matched = matched.filter(pl.col("addr_score") >= cutoff)

    matched = matched.with_columns(pl.lit(step_config["name"]).alias("match_step"))

    for inherit_cfg in step_config.get("inherit", []):
        src_col = inherit_cfg["source"]
        as_col = inherit_cfg["as"]
        # Find the actual suffixed column (handles _dst, _dst2, _dst3, etc.)
        # Cap at 20 iterations -- supports up to 20 phases with overlapping columns
        found = None
        suffix = "_dst"
        for _ in range(20):
            candidate = src_col + suffix
            if candidate in matched.columns:
                found = candidate
                break
            suffix = _next_dst_suffix(suffix)
        if found:
            matched = matched.rename({found: as_col})
        elif src_col in matched.columns:
            matched = matched.rename({src_col: as_col})

    if collect_rejections:
        return matched, rejections
    return matched


# ---------------------------------------------------------------------------
# Full pipeline -- helper functions
# ---------------------------------------------------------------------------

def _build_populations(recipe_pops: dict, sources: dict) -> dict:
    """Build population DataFrames from recipe config and loaded sources.

    Handles filtered populations, remainder populations (empty filter),
    and _previous_matched references for multi-phase pipelines.
    """
    from recipe import build_filter_expr, filter_population

    populations = {}
    for pop_name, pop_cfg in recipe_pops.items():
        src_name = pop_cfg["source"]
        if src_name == "_previous_matched":
            populations[pop_name] = {"config": pop_cfg, "df": None, "source": src_name}
            continue
        src_df = sources[src_name]

        if "filter" in pop_cfg and pop_cfg["filter"]:
            filtered = filter_population(src_df, pop_cfg)
            populations[pop_name] = {"config": pop_cfg, "df": filtered, "source": src_name}
        else:
            populations[pop_name] = {"config": pop_cfg, "df": None, "source": src_name}

    # Compute remainder populations
    for pop_name, pop_data in populations.items():
        if pop_data["df"] is not None or pop_data["source"] == "_previous_matched":
            continue

        src_df = sources[pop_data["source"]]
        remainder = src_df

        for other_name, other_data in populations.items():
            if other_name == pop_name or other_data["source"] != pop_data["source"]:
                continue
            other_cfg = other_data["config"]
            if "filter" in other_cfg and other_cfg["filter"]:
                remainder = remainder.filter(~build_filter_expr(other_cfg["filter"]))

        for garb_name, garb_cfg in recipe_pops.items():
            if garb_name == pop_name:
                continue
            if garb_cfg.get("action") == "exclude" and "filter" in garb_cfg and garb_cfg["filter"]:
                remainder = remainder.filter(~build_filter_expr(garb_cfg["filter"]))

        pop_data["df"] = remainder

    return populations


def _run_phase_steps(steps, populations, sources,
                     aliases=None, stopwords=None,
                     track_field=None, tie_breaker=None,
                     step_offset=0):
    """Run matching steps for a single phase. Returns results dict."""
    all_matched = []
    matched_source_keys = set()
    all_rejections = {}

    for step_idx, step in enumerate(steps):
        src_pop = step["source"]
        dst_pop = step["destination"]

        if src_pop == dst_pop:
            step = {**step, "_same_pop": True}
            if track_field:
                import sys
                print(
                    f'[INFO] Step {step_offset+step_idx+1} ("{step.get("name", "?")}") matches '
                    f'{src_pop} against itself -- self-matches on {track_field} will be excluded.',
                    file=sys.stderr,
                )

        src_df = populations.get(src_pop, {}).get("df")
        if src_df is None or src_df.height == 0:
            continue

        dst_df = populations.get(dst_pop, {}).get("df")
        if dst_df is None:
            dst_df = sources.get(dst_pop)
        if dst_df is None or dst_df.height == 0:
            continue

        if tie_breaker:
            tb_col = tie_breaker["column"]
            if tb_col in dst_df.columns:
                dst_df = _presort_by_tie_breaker(dst_df, tie_breaker, tb_col)

        step_result = run_matching_step(src_df, dst_df, step,
                                        aliases=aliases, stopwords=stopwords,
                                        dedup_field=track_field,
                                        collect_rejections=True)
        matched, rejections = step_result

        if "street_mismatch" in rejections:
            for key, score in rejections["street_mismatch"].items():
                if key not in all_rejections:
                    all_rejections[key] = {
                        "reason": "street_mismatch",
                        "step": step["name"],
                        "best_addr_score": score,
                    }

        if "addr_below_threshold" in rejections:
            for key, score in rejections["addr_below_threshold"].items():
                if key not in all_rejections:
                    all_rejections[key] = {
                        "reason": "addr_below_threshold",
                        "step": step["name"],
                        "best_addr_score": score,
                    }

        if matched.height > 0:
            matched = matched.with_columns(
                pl.lit(step_offset + step_idx).alias("_step_order")
            )
            all_matched.append(matched)
            if track_field in matched.columns:
                matched_source_keys.update(
                    matched[track_field].cast(pl.String).to_list()
                )

    return {
        "all_matched": all_matched,
        "matched_source_keys": matched_source_keys,
        "all_rejections": all_rejections,
    }


def _resolve_matches(all_matched, track_field, match_mode, tie_breaker=None):
    """Resolve multi-step matches into a single best-match DataFrame."""
    if not all_matched:
        return pl.DataFrame()

    combined = pl.concat(all_matched, how="diagonal")

    if match_mode == "best_match":
        sort_cols = ["_step_order"]
        sort_desc = [False]
        if "name_score" in combined.columns:
            sort_cols.append("name_score")
            sort_desc.append(True)
        if "addr_score" in combined.columns:
            sort_cols.append("addr_score")
            sort_desc.append(True)

        if tie_breaker:
            tb_col = tie_breaker["column"]
            tb_dst = tb_col + "_dst" if tb_col + "_dst" in combined.columns else tb_col
            if tb_dst in combined.columns:
                tb_order = tie_breaker.get("order", "asc")
                combined = combined.with_columns(
                    _tb_sort_key_expr(tb_dst, tie_breaker)
                )
                sort_cols.append("_tb_sort_key")
                sort_desc.append(tb_order == "desc")

        combined = combined.sort(sort_cols, descending=sort_desc)
        combined = combined.unique(subset=[track_field], keep="first")

    if "name_score" in combined.columns:
        combined = combined.with_columns(
            pl.col("name_score").fill_null(100.0).alias("name_score")
        )

    combined = combined.drop([c for c in combined.columns if c.startswith("_")])
    return combined


def _build_unmatched(pop1_df, matched_source_keys, track_field, all_rejections):
    """Build unmatched DataFrame with reason codes."""
    if pop1_df.height > 0 and track_field in pop1_df.columns:
        unmatched = pop1_df.filter(
            ~pl.col(track_field).is_in(list(matched_source_keys))
        )
    else:
        unmatched = pl.DataFrame()

    if unmatched.height > 0 and track_field in unmatched.columns and all_rejections:
        rej_df = pl.DataFrame([
            {track_field: k, "reason_code": v["reason"],
             "rejection_step": v["step"],
             "best_rejected_score": v.get("best_addr_score")}
            for k, v in all_rejections.items()
        ])
        unmatched = unmatched.join(rej_df, on=track_field, how="left")
        unmatched = unmatched.with_columns(
            pl.col("reason_code").fill_null("no_name_match"),
        )
    elif unmatched.height > 0:
        unmatched = unmatched.with_columns(
            pl.lit("no_name_match").alias("reason_code"),
            pl.lit(None).cast(pl.String).alias("rejection_step"),
            pl.lit(None).cast(pl.Float64).alias("best_rejected_score"),
        )

    return unmatched


def _load_normalization(norm_cfg, base_dir):
    """Load aliases and stopwords from normalization config."""
    aliases = None
    stopwords = None
    if norm_cfg.get("aliases"):
        aliases_path = Path(norm_cfg["aliases"])
        if not aliases_path.exists():
            aliases_path = Path(base_dir) / norm_cfg["aliases"]
        if aliases_path.exists():
            aliases = json.loads(aliases_path.read_text())
    if norm_cfg.get("stopwords"):
        sw_path = Path(norm_cfg["stopwords"])
        if not sw_path.exists():
            sw_path = Path(base_dir) / norm_cfg["stopwords"]
        if sw_path.exists():
            sw_data = json.loads(sw_path.read_text())
            if isinstance(sw_data, dict):
                stopwords = [w for words in sw_data.values() for w in words]
            else:
                stopwords = sw_data
    return aliases, stopwords


def _resolve_track_field(steps, populations):
    """Determine the tracking field for dedup and unmatched detection."""
    pop1_name = steps[0]["source"]
    src_match_field = steps[0]["match_fields"][0]["source"]
    pop1_df = populations.get(pop1_name, {}).get("df", pl.DataFrame())
    pop1_cfg = populations.get(pop1_name, {}).get("config", {})
    record_key = pop1_cfg.get("record_key")
    if record_key:
        if record_key not in pop1_df.columns:
            from recipe import RecipeValidationError
            raise RecipeValidationError(
                f'Population "{pop1_name}" record_key "{record_key}" not found '
                f'in source data. Available: {", ".join(sorted(pop1_df.columns)[:10])}'
            )
        return record_key
    else:
        import sys
        print(
            f'[WARN] Population "{pop1_name}" has no record_key. '
            f"Falling back to match field '{src_match_field}' for dedup -- "
            "records with duplicate names may be collapsed.",
            file=sys.stderr,
        )
        return src_match_field


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline(recipe: dict, base_dir: str = ".") -> dict:
    """Run the complete matching pipeline from a recipe.

    Supports single-phase (classic) and multi-phase recipes.
    Multi-phase recipes use a ``phases`` key with per-phase
    populations and steps. Each phase can reference
    ``_previous_matched`` as a population source.

    Returns:
        dict with keys:
        - matched: pl.DataFrame of matched records
        - unmatched: pl.DataFrame of unmatched records with reason codes
        - populations: dict of {name: DataFrame} for each population
        - stats: {total_source, matched_count, unmatched_count}
        - timing: {load, setup, match, resolve} in seconds
        - phases: list of per-phase stats (multi-phase only)
    """
    import time as _time

    from recipe import load_source
    timings = {}

    # Expand step_defaults if not already done (e.g. dict passed directly)
    if "step_defaults" in recipe:
        from recipe import _apply_step_defaults
        recipe = _apply_step_defaults(recipe)

    t = _time.time()
    sources = {}
    for name, cfg in recipe["sources"].items():
        loader_type = cfg.get("loader", "file")
        src_t = _time.time()
        print(f"  Loading source '{name}' ({loader_type})...", flush=True)
        sources[name] = load_source(
            cfg, base_dir,
            recipe_name=recipe.get("name", ""),
            source_name=name,
        )
        src_elapsed = _time.time() - src_t
        print(f"    -> {sources[name].height:,} rows x {sources[name].width} cols ({src_elapsed:.1f}s)", flush=True)

    print("Running matching pipeline...", flush=True)

    norm_cfg = recipe.get("normalization", {})
    aliases, stopwords = _load_normalization(norm_cfg, base_dir)
    timings["load"] = _time.time() - t

    if "phases" in recipe:
        return _run_multi_phase(recipe, sources, aliases, stopwords, timings)
    else:
        return _run_single_phase(recipe, sources, aliases, stopwords, timings)


def _run_single_phase(recipe, sources, aliases, stopwords, timings):
    """Run a classic single-phase pipeline (backward compatible)."""
    import time as _time

    from recipe import RecipeValidationError, validate_fields

    t = _time.time()

    # Validate filter fields
    filter_errors = []
    for pop_name, pop_cfg in recipe["populations"].items():
        src_name = pop_cfg.get("source", "")
        if src_name not in sources:
            continue
        src_cols = set(sources[src_name].columns)
        for cond in pop_cfg.get("filter", []):
            if "field" in cond and cond["field"] not in src_cols:
                available = ", ".join(sorted(src_cols)[:10])
                filter_errors.append(
                    f'Population "{pop_name}" filter field "{cond["field"]}" '
                    f"not found. Available: {available}"
                )
    if filter_errors:
        import sys
        for e in filter_errors:
            print(f"[ERROR] {e}", file=sys.stderr)
        raise RecipeValidationError(
            f"Recipe has {len(filter_errors)} filter field error(s). Fix recipe config and retry."
        )

    populations = _build_populations(recipe["populations"], sources)

    # Semantic field validation
    val_errors, val_warnings = validate_fields(recipe, sources, populations)
    for w in val_warnings:
        import sys
        print(f"[WARN] {w}", file=sys.stderr)
    if val_errors:
        import sys
        for e in val_errors:
            print(f"[ERROR] {e}", file=sys.stderr)
        raise RecipeValidationError(
            f"Recipe has {len(val_errors)} field error(s). Fix recipe config and retry."
        )

    timings["load"] = timings.get("load", 0) + (_time.time() - t)

    t = _time.time()
    match_mode = recipe.get("output", {}).get("match_mode", "best_match")
    tie_breaker = recipe.get("output", {}).get("tie_breaker")
    track_field = _resolve_track_field(recipe["steps"], populations)

    phase_result = _run_phase_steps(
        recipe["steps"], populations, sources,
        aliases=aliases, stopwords=stopwords,
        track_field=track_field, tie_breaker=tie_breaker,
    )
    timings["match"] = _time.time() - t

    t = _time.time()
    combined = _resolve_matches(
        phase_result["all_matched"], track_field, match_mode, tie_breaker
    )

    pop1_name = recipe["steps"][0]["source"]
    pop1_df = populations.get(pop1_name, {}).get("df", pl.DataFrame())
    unmatched = _build_unmatched(
        pop1_df, phase_result["matched_source_keys"],
        track_field, phase_result["all_rejections"]
    )
    timings["resolve"] = _time.time() - t

    return {
        "matched": combined,
        "unmatched": unmatched,
        "populations": {k: v["df"] for k, v in populations.items() if v["df"] is not None},
        "stats": {
            "total_source": pop1_df.height,
            "matched_count": combined.height,
            "unmatched_count": unmatched.height,
        },
        "timing": timings,
    }


def _run_multi_phase(recipe, sources, aliases, stopwords, timings):
    """Run a multi-phase pipeline.

    Each phase has its own populations and steps. Phases execute
    sequentially. A phase can reference ``_previous_matched`` as
    a population source to chain phase outputs.
    """
    import sys
    import time as _time

    match_mode = recipe.get("output", {}).get("match_mode", "best_match")
    tie_breaker = recipe.get("output", {}).get("tie_breaker")
    phases = recipe["phases"]
    phase_stats = []
    phase_snapshots = []  # per-phase matched DataFrames for phase-level output
    previous_matched = None
    step_offset = 0
    total_match_time = 0

    first_phase_pop1_df = None
    first_phase_track_field = None
    first_phase_matched = None  # Store Phase 1 output for partial-match recovery
    cumulative_matched_keys = set()
    cumulative_rejections = {}

    for phase_idx, phase in enumerate(phases):
        t = _time.time()
        phase_name = phase.get("name", f"Phase {phase_idx + 1}")
        print(f"[PHASE {phase_idx + 1}] {phase_name}", file=sys.stderr)

        phase_pops_cfg = phase.get("populations", {})
        populations = _build_populations(phase_pops_cfg, sources)

        # Inject _previous_matched
        for pop_name, pop_data in populations.items():
            if pop_data["source"] == "_previous_matched":
                if previous_matched is None or previous_matched.height == 0:
                    print(
                        f'[WARN] Phase {phase_idx + 1} population "{pop_name}" '
                        f"references _previous_matched but no matches from prior phase.",
                        file=sys.stderr,
                    )
                    pop_data["df"] = pl.DataFrame()
                else:
                    pop_data["df"] = previous_matched

        phase_steps = phase.get("steps", [])
        if not phase_steps:
            print(f"[WARN] Phase {phase_idx + 1} has no steps, skipping.",
                  file=sys.stderr)
            continue

        # Check if all source populations are empty (e.g. _previous_matched with no data)
        pop1_name = phase_steps[0]["source"]
        pop1_df_check = populations.get(pop1_name, {}).get("df", pl.DataFrame())
        if pop1_df_check.height == 0:
            print("  Input: 0 | Matched: 0 | Skipped (empty source)",
                  file=sys.stderr)
            phase_stats.append({
                "name": phase_name,
                "matched_count": 0,
                "input_count": 0,
                "time": _time.time() - t,
            })
            previous_matched = pl.DataFrame()
            phase_snapshots.append(pl.DataFrame())
            step_offset += len(phase_steps)
            continue

        # Validate fields for this phase
        from recipe import validate_fields
        mini_recipe = {
            "steps": phase_steps,
            "populations": phase_pops_cfg,
        }
        val_errors, val_warnings = validate_fields(mini_recipe, sources, populations)
        for w in val_warnings:
            print(f"[WARN] Phase {phase_idx + 1}: {w}", file=sys.stderr)
        if val_errors:
            from recipe import RecipeValidationError
            for e in val_errors:
                print(f"[ERROR] Phase {phase_idx + 1}: {e}", file=sys.stderr)
            raise RecipeValidationError(
                f"Phase {phase_idx + 1} has {len(val_errors)} field error(s)."
            )

        track_field = _resolve_track_field(phase_steps, populations)

        if phase_idx == 0:
            pop1_name = phase_steps[0]["source"]
            first_phase_pop1_df = populations.get(pop1_name, {}).get(
                "df", pl.DataFrame()
            )
            first_phase_track_field = track_field

        phase_result = _run_phase_steps(
            phase_steps, populations, sources,
            aliases=aliases, stopwords=stopwords,
            track_field=track_field, tie_breaker=tie_breaker,
            step_offset=step_offset,
        )

        phase_time = _time.time() - t
        total_match_time += phase_time

        phase_matched = _resolve_matches(
            phase_result["all_matched"], track_field, match_mode, tie_breaker
        )

        pop1_name = phase_steps[0]["source"]
        input_count = populations.get(pop1_name, {}).get(
            "df", pl.DataFrame()
        ).height

        # Per-step counts within this phase (for summary reporting)
        phase_step_counts = {}
        if phase_matched.height > 0 and "match_step" in phase_matched.columns:
            for row in phase_matched.group_by("match_step").len().iter_rows():
                phase_step_counts[row[0]] = row[1]

        phase_stats.append({
            "name": phase_name,
            "matched_count": phase_matched.height,
            "input_count": input_count,
            "time": phase_time,
            "step_counts": phase_step_counts,
        })

        print(
            f"  Input: {input_count} | Matched: {phase_matched.height} | Time: {phase_time:.2f}s",
            file=sys.stderr,
        )

        if phase_matched.height > 0:
            if first_phase_track_field and first_phase_track_field in phase_matched.columns:
                cumulative_matched_keys.update(
                    phase_matched[first_phase_track_field].cast(pl.String).to_list()
                )
            # Capture first phase matched for partial-match recovery
            if phase_idx == 0:
                first_phase_matched = phase_matched

        cumulative_rejections.update(phase_result["all_rejections"])
        phase_snapshots.append(phase_matched.clone())
        previous_matched = phase_matched
        step_offset += len(phase_steps)

    timings["match"] = total_match_time

    t = _time.time()
    # Final output: last phase's matched + partial matches from earlier phases.
    # Partial matches are records that matched Phase 1 but dropped in later
    # phases (e.g. no parent relationship found). They appear in the matched
    # output with null columns for the phases they missed.
    combined = previous_matched if previous_matched is not None else pl.DataFrame()

    if (
        first_phase_matched is not None
        and combined.height > 0
        and first_phase_track_field
        and first_phase_track_field in first_phase_matched.columns
        and first_phase_track_field in combined.columns
    ):
        final_keys = set(combined[first_phase_track_field].cast(pl.String).to_list())
        phase1_keys = set(
            first_phase_matched[first_phase_track_field].cast(pl.String).to_list()
        )
        partial_keys = phase1_keys - final_keys
        if partial_keys:
            partial_df = first_phase_matched.filter(
                pl.col(first_phase_track_field).cast(pl.String).is_in(partial_keys)
            )
            # Align columns -- add missing columns as null (match dtype)
            for col in combined.columns:
                if col not in partial_df.columns:
                    col_dtype = combined.schema[col]
                    partial_df = partial_df.with_columns(
                        pl.lit(None).cast(col_dtype).alias(col)
                    )
            # Select same columns in same order
            partial_df = partial_df.select(combined.columns)
            combined = pl.concat([combined, partial_df], how="vertical_relaxed")

    track_field = first_phase_track_field or "_unknown"
    pop1_df = first_phase_pop1_df if first_phase_pop1_df is not None else pl.DataFrame()
    unmatched = _build_unmatched(
        pop1_df, cumulative_matched_keys, track_field, cumulative_rejections
    )
    timings["resolve"] = _time.time() - t

    return {
        "matched": combined,
        "unmatched": unmatched,
        "populations": {},
        "stats": {
            "total_source": pop1_df.height,
            "matched_count": combined.height,
            "unmatched_count": unmatched.height,
            "phases": phase_stats,
        },
        "timing": timings,
        "phases": phase_stats,
        "phase_snapshots": phase_snapshots,
    }
