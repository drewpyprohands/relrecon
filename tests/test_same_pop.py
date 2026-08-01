"""Tests for same-population matching (Issue #132).

Verifies that when source == destination in a step, records don't
match against themselves, and duplicates are properly handled.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import polars as pl

from matching import match_names_exact, match_names_fuzzy


class TestSamePopExact:
    """Same-population exact matching with self-exclusion."""

    def _make_df(self):
        return pl.DataFrame({
            "id": ["T1", "T2", "T3", "T4"],
            "name": ["Alpha Inc", "Alpha Inc", "Beta Corp", "Gamma LLC"],
        })

    def test_self_match_excluded(self):
        """A record should not match itself."""
        df = self._make_df()
        result = match_names_exact(
            df, df, "name", "name",
            tiers=["raw"], dedup_field="id",
            exclude_self_key="id",
        )
        # T1 and T2 both have "Alpha Inc" -- they should match each other
        # but NOT themselves
        for row in result.iter_rows(named=True):
            assert row["id"] != row.get("id_dst"), (
                f"Self-match found: {row['id']} matched itself"
            )

    def test_duplicates_match_each_other(self):
        """Records with the same name should match each other (not themselves)."""
        df = self._make_df()
        result = match_names_exact(
            df, df, "name", "name",
            tiers=["raw"], dedup_field="id",
            exclude_self_key="id",
        )
        # T1 should match T2 (or vice versa) since both are "Alpha Inc"
        matched_ids = result["id"].to_list()
        assert "T1" in matched_ids or "T2" in matched_ids

    def test_unique_record_unmatched(self):
        """A record with no duplicates should not appear in results."""
        df = self._make_df()
        result = match_names_exact(
            df, df, "name", "name",
            tiers=["raw"], dedup_field="id",
            exclude_self_key="id",
        )
        matched_ids = result["id"].to_list()
        # Beta Corp (T3) and Gamma LLC (T4) have no duplicates
        assert "T3" not in matched_ids
        assert "T4" not in matched_ids

    def test_no_self_exclusion_when_different_pops(self):
        """Without exclude_self_key, normal matching behavior."""
        df = self._make_df()
        result = match_names_exact(
            df, df, "name", "name",
            tiers=["raw"], dedup_field="id",
        )
        # Without self-exclusion, all records match (including self)
        assert result.height >= 3  # at least T1, T2, T3, T4 match themselves


class TestSamePopFuzzy:
    """Same-population fuzzy matching with self-exclusion."""

    def _make_df(self):
        return pl.DataFrame({
            "id": ["T1", "T2", "T3", "T4"],
            "name": [
                "Company XYZ Incorporated",
                "Company XYZ Inc",
                "Company XYZ",
                "Totally Different LLC",
            ],
        })

    def test_self_match_excluded(self):
        """No record should match itself in fuzzy mode."""
        df = self._make_df()
        result = match_names_fuzzy(
            df, df, "name", "name",
            tiers=["raw"], threshold=60, dedup_field="id",
            exclude_self_key="id",
        )
        for row in result.iter_rows(named=True):
            assert row["id"] != row.get("id_dst"), (
                f"Self-match found: {row['id']} matched itself"
            )

    def test_similar_records_match(self):
        """Fuzzy matches between similar names should still work."""
        df = self._make_df()
        result = match_names_fuzzy(
            df, df, "name", "name",
            tiers=["raw"], threshold=60, dedup_field="id",
            exclude_self_key="id",
        )
        # T1, T2, T3 are all variations of "Company XYZ" -- should match
        matched_ids = set(result["id"].to_list())
        assert len(matched_ids & {"T1", "T2", "T3"}) >= 2

    def test_unique_record_unmatched(self):
        """A record with no similar names should not appear."""
        df = self._make_df()
        result = match_names_fuzzy(
            df, df, "name", "name",
            tiers=["raw"], threshold=60, dedup_field="id",
            exclude_self_key="id",
        )
        matched_ids = result["id"].to_list()
        assert "T4" not in matched_ids
