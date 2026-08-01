"""Tests for final scoring tie-breaker (Issue #133).

Verifies that when multiple matches exist for the same source record,
the tie-breaker column selects the correct winner.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import polars as pl

from matching import _tb_sort_key_expr


def _make_matched(source_df, dest_df):
    """Create a raw matched DataFrame (no dedup) via join."""
    # Simulate what exact matching does: join on name, keep all matches
    matched = source_df.join(
        dest_df, on="name", how="inner", suffix="_dst"
    )
    return matched


def _run_with_tie_breaker(source_df, dest_df, tie_breaker):
    """Simulate the pipeline dedup with tie-breaker."""
    matched = _make_matched(source_df, dest_df)
    if matched.height == 0:
        return matched

    # Apply tie-breaker logic using shared helper (same as run_pipeline)
    tb_col = tie_breaker["column"]
    tb_dst = tb_col + "_dst" if tb_col + "_dst" in matched.columns else tb_col
    tb_order = tie_breaker.get("order", "asc")

    matched = matched.with_columns(_tb_sort_key_expr(tb_dst, tie_breaker))
    sort_cols = ["_tb_sort_key"]
    sort_desc = [tb_order == "desc"]

    matched = matched.sort(sort_cols, descending=sort_desc)
    matched = matched.unique(subset=["id"], keep="first")
    matched = matched.drop([c for c in matched.columns if c.startswith("_")])
    return matched


class TestTieBreakerAlphaPrefix:
    """Tie-breaking by stripping alpha prefix and sorting numeric."""

    def test_lowest_supplier_id_wins(self):
        """S1013 should win over S2456 when order=asc."""
        source = pl.DataFrame({
            "id": ["T1"],
            "name": ["Company XYZ"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2", "D3"],
            "name": ["Company XYZ", "Company XYZ", "Company XYZ"],
            "supplier_id": ["S2456", "S1013", "S2023"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "supplier_id", "strip_prefix": "alpha", "order": "asc"},
        )
        assert result.height == 1
        assert result["supplier_id"][0] == "S1013"

    def test_highest_supplier_id_wins_desc(self):
        """S2456 should win when order=desc."""
        source = pl.DataFrame({
            "id": ["T1"],
            "name": ["Company XYZ"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2", "D3"],
            "name": ["Company XYZ", "Company XYZ", "Company XYZ"],
            "supplier_id": ["S2456", "S1013", "S2023"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "supplier_id", "strip_prefix": "alpha", "order": "desc"},
        )
        assert result.height == 1
        assert result["supplier_id"][0] == "S2456"

    def test_multiple_sources_each_get_lowest(self):
        """Each source record should get its own lowest-ID match."""
        source = pl.DataFrame({
            "id": ["T1", "T2"],
            "name": ["Alpha Corp", "Beta Inc"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2", "D3", "D4"],
            "name": ["Alpha Corp", "Alpha Corp", "Beta Inc", "Beta Inc"],
            "supplier_id": ["S5000", "S1000", "S3000", "S2000"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "supplier_id", "strip_prefix": "alpha", "order": "asc"},
        )
        assert result.height == 2
        t1_row = result.filter(pl.col("id") == "T1")
        t2_row = result.filter(pl.col("id") == "T2")
        assert t1_row["supplier_id"][0] == "S1000"
        assert t2_row["supplier_id"][0] == "S2000"

    def test_non_numeric_after_strip_handled(self):
        """IDs that can't be parsed as int should sort last."""
        source = pl.DataFrame({
            "id": ["T1"],
            "name": ["Test Corp"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2", "D3"],
            "name": ["Test Corp", "Test Corp", "Test Corp"],
            "supplier_id": ["S1000", "UNKNOWN", "S500"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "supplier_id", "strip_prefix": "alpha", "order": "asc"},
        )
        assert result.height == 1
        assert result["supplier_id"][0] == "S500"


class TestTieBreakerLiteralPrefix:
    """Tie-breaking with a literal string prefix strip."""

    def test_literal_prefix_strips_start_only(self):
        """strip_prefix: 'S' should only remove leading 'S', not all occurrences."""
        source = pl.DataFrame({
            "id": ["T1"],
            "name": ["Test Corp"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2", "D3"],
            "name": ["Test Corp", "Test Corp", "Test Corp"],
            "code": ["S100S", "S050S", "S200S"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "code", "strip_prefix": "S", "order": "asc"},
        )
        assert result.height == 1
        # "S050S" -> "050S" (strip leading S only) -> sorts first alphabetically
        assert result["code"][0] == "S050S"

    def test_no_prefix_match_unchanged(self):
        """Values without the prefix should be unchanged."""
        source = pl.DataFrame({
            "id": ["T1"],
            "name": ["Test Corp"],
        })
        dest = pl.DataFrame({
            "id": ["D1", "D2"],
            "name": ["Test Corp", "Test Corp"],
            "code": ["X500", "A100"],
        })
        result = _run_with_tie_breaker(
            source, dest,
            {"column": "code", "strip_prefix": "X", "order": "asc"},
        )
        assert result.height == 1
        # "A100" unchanged (no X prefix), "X500" -> "500"
        # "500" < "A100" alphabetically
        assert result["code"][0] == "X500"
