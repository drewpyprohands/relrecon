"""
Core matching engine. ADR Option C aligned.

Uses Polars for all data ops (joins, filtering, expressions).
Uses RapidFuzz process.cdist for batch fuzzy matching (full C++ matrix).
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

        matched = src.join(
            dst, on="_match_key", how="inner", suffix="_dst",
            maintain_order="right",  # preserve dest ordering for tie-breaker pre-sort
        )

        # Exclude self-matches for same-population matching
        if exclude_self_key and matched.height > 0:
            dst_key = exclude_self_key + "_dst" if exclude_self_key + "_dst" in matched.columns else None
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
    """Fuzzy name matching via RapidFuzz cdist (full C++ matrix, no Python loops).

    For each tier, builds match-key columns (same as exact), then uses
    RapidFuzz cdist to compute the full score matrix in C++. Extracts
    the best match per source row above threshold.
    Tries tiers in order; deduplicates by tier priority (earlier tier wins),
    then by highest score within tier.

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

        # workers=-1 uses all available cores
        score_matrix = rprocess.cdist(
            src_keys, dst_keys,
            scorer=scorer_fn, score_cutoff=threshold,
            dtype=np.float32, workers=-1,
        )

        # Exclude self-matches for same-population matching
        if exclude_self_key:
            src_self = src[exclude_self_key].cast(pl.String).fill_null("").to_numpy()
            dst_self = dst[exclude_self_key].cast(pl.String).fill_null("").to_numpy()
            # Boolean mask: True where src key == dst key (self-match)
            self_mask = src_self[:, None] == dst_self[None, :]
            score_matrix[self_mask] = 0.0

        best_dst_idxs = score_matrix.argmax(axis=1)
        best_scores = score_matrix[np.arange(len(src_keys)), best_dst_idxs]

        # cdist sets below-threshold scores to 0
        mask = best_scores >= threshold
        if not mask.any():
            continue

        src_idxs = np.where(mask)[0].tolist()
        dst_idxs = best_dst_idxs[mask].tolist()
        scores = best_scores[mask].tolist()

        matched_src = src[src_idxs].drop("_match_key")
        matched_dst = dst[dst_idxs].drop("_match_key")

        dst_renames = {}
        for col in matched_dst.columns:
            if col in matched_src.columns:
                dst_renames[col] = col + "_dst"
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
        if src_col + "_dst" in matched.columns:
            matched = matched.rename({src_col + "_dst": as_col})
        elif src_col in matched.columns:
            matched = matched.rename({src_col: as_col})

    if collect_rejections:
        return matched, rejections
    return matched


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_pipeline(recipe: dict, base_dir: str = ".") -> dict:
    """Run the complete matching pipeline from a recipe.

    Returns:
        dict with keys:
        - matched: pl.DataFrame of matched records
        - unmatched: pl.DataFrame of unmatched records with reason codes
        - populations: dict of {name: DataFrame} for each population
        - stats: {total_source, matched_count, unmatched_count}
        - timing: {load, setup, match, resolve} in seconds
    """
    import time as _time

    from recipe import build_filter_expr, filter_population, load_source
    timings = {}

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

    # Pre-validate filter fields before building populations
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

        from recipe import RecipeValidationError
        for e in filter_errors:
            print(f"[ERROR] {e}", file=sys.stderr)
        raise RecipeValidationError(
            f"Recipe has {len(filter_errors)} filter field error(s). Fix recipe config and retry."
        )

    # Build populations
    populations = {}
    for pop_name, pop_cfg in recipe["populations"].items():
        src_name = pop_cfg["source"]
        src_df = sources[src_name]

        if "filter" in pop_cfg and pop_cfg["filter"]:
            filtered = filter_population(src_df, pop_cfg)
            populations[pop_name] = {"config": pop_cfg, "df": filtered, "source": src_name}
        else:
            # Remainder: computed after other pops
            populations[pop_name] = {"config": pop_cfg, "df": None, "source": src_name}

    # Compute remainder populations (exclude Pop1 + Garbage from same source)
    for pop_name, pop_data in populations.items():
        if pop_data["df"] is not None:
            continue

        src_df = sources[pop_data["source"]]
        remainder = src_df

        for other_name, other_data in populations.items():
            if other_name == pop_name or other_data["source"] != pop_data["source"]:
                continue
            other_cfg = other_data["config"]
            if "filter" in other_cfg and other_cfg["filter"]:
                remainder = remainder.filter(~build_filter_expr(other_cfg["filter"]))

        # Also exclude garbage populations
        for garb_name, garb_cfg in recipe["populations"].items():
            if garb_name == pop_name:
                continue
            if garb_cfg.get("action") == "exclude" and "filter" in garb_cfg and garb_cfg["filter"]:
                remainder = remainder.filter(~build_filter_expr(garb_cfg["filter"]))

        pop_data["df"] = remainder

    # Semantic field validation (runs every time, not just dry-run)
    from recipe import RecipeValidationError, validate_fields
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

    timings["load"] = _time.time() - t
    print("Running matching pipeline...", flush=True)

    t = _time.time()
    norm_cfg = recipe.get("normalization", {})
    aliases = None
    stopwords = None
    if norm_cfg.get("aliases"):
        aliases_path = Path(norm_cfg["aliases"])
        # Try relative to cwd first, then relative to base_dir
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
            # Flatten stopwords if categorized by type (name/address)
            if isinstance(sw_data, dict):
                stopwords = [w for words in sw_data.values() for w in words]
            else:
                stopwords = sw_data

    timings["setup"] = _time.time() - t

    t = _time.time()
    match_mode = recipe.get("output", {}).get("match_mode", "best_match")
    all_matched = []
    matched_source_keys = set()
    all_rejections = {}  # track_field_value -> {reason, step, score}

    # Determine the unique record identifier for dedup and unmatched tracking.
    # Uses record_key from the source population config. Falls back to match
    # field only if record_key is not set (legacy recipes).
    pop1_name = recipe["steps"][0]["source"]
    src_match_field = recipe["steps"][0]["match_fields"][0]["source"]
    pop1_df_check = populations.get(pop1_name, {}).get("df", pl.DataFrame())
    pop1_cfg = recipe["populations"].get(pop1_name, {})
    record_key = pop1_cfg.get("record_key")
    if record_key:
        if record_key not in pop1_df_check.columns:
            from recipe import RecipeValidationError
            raise RecipeValidationError(
                f'Population "{pop1_name}" record_key "{record_key}" not found '
                f'in source data. Available: {", ".join(sorted(pop1_df_check.columns)[:10])}'
            )
        track_field = record_key
    else:
        import sys
        print(
            f'[WARN] Population "{pop1_name}" has no record_key. '
            f"Falling back to match field '{src_match_field}' for dedup -- "
            "records with duplicate names may be collapsed.",
            file=sys.stderr,
        )
        track_field = src_match_field

    for step_idx, step in enumerate(recipe["steps"]):
        src_pop = step["source"]
        dst_pop = step["destination"]

        # Detect same-population matching
        if src_pop == dst_pop:
            step = {**step, "_same_pop": True}
            if track_field:
                import sys
                print(
                    f'[INFO] Step {step_idx+1} ("{step.get("name", "?")}") matches '
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

        # Pre-sort destination by tie-breaker so per-step dedup picks the right winner
        tie_breaker = recipe.get("output", {}).get("tie_breaker")
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
            # Tag step order for multi-match resolution
            matched = matched.with_columns(pl.lit(step_idx).alias("_step_order"))
            all_matched.append(matched)

            if track_field in matched.columns:
                matched_source_keys.update(matched[track_field].cast(pl.String).to_list())

    timings["match"] = _time.time() - t

    t = _time.time()
    if all_matched:
        combined = pl.concat(all_matched, how="diagonal")

        if match_mode == "best_match":
            # Sort: prefer earlier step, then higher name score, then higher address score
            sort_cols = ["_step_order"]
            sort_desc = [False]
            if "name_score" in combined.columns:
                sort_cols.append("name_score")
                sort_desc.append(True)
            if "addr_score" in combined.columns:
                sort_cols.append("addr_score")
                sort_desc.append(True)

            # Tie-breaker: secondary sort after score columns.
            # NOTE: Per-step dedup already picks the tie-breaker winner via
            # pre-sorted dest ordering. This cross-step sort is a safety net
            # in case future changes alter per-step dedup behavior.
            tie_breaker = recipe.get("output", {}).get("tie_breaker")
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

        # Exact matches get name_score=100 (fuzzy steps already have scores)
        if "name_score" in combined.columns:
            combined = combined.with_columns(
                pl.col("name_score").fill_null(100.0).alias("name_score")
            )

        combined = combined.drop([c for c in combined.columns if c.startswith("_")])
    else:
        combined = pl.DataFrame()

    # Unmatched
    pop1_name = recipe["steps"][0]["source"]
    pop1_df = populations.get(pop1_name, {}).get("df", pl.DataFrame())
    recipe["steps"][0]["match_fields"][0]["source"]

    if pop1_df.height > 0 and track_field in pop1_df.columns:
        unmatched = pop1_df.filter(~pl.col(track_field).is_in(list(matched_source_keys)))
    else:
        unmatched = pl.DataFrame()

    if unmatched.height > 0 and track_field in unmatched.columns and all_rejections:
        rej_df = pl.DataFrame([
            {track_field: k, "reason_code": v["reason"],
             "rejection_step": v["step"], "best_rejected_score": v.get("best_addr_score")}
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
