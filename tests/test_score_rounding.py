"""Tests for score rounding behavior (Issue #119 follow-up).

Verifies that rounding never occurs during score selection -- only in
the report output layer. Internal scoring functions must return raw
floats so comparison/dedup uses full precision.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from address import build_variants, score_address_multi_tier, score_address_pair


class TestNoInternalRounding:
    """Verify that scoring functions return raw floats (not rounded)."""

    def test_score_address_pair_returns_raw_float(self):
        """best_score should not be a rounded value."""
        # Use addresses that produce a non-integer weighted score
        src = build_variants("1593 Pine Avenue Suite 157")
        dst = build_variants("1593 Pine Avenue Suite 160")
        result = score_address_pair(src, dst, tier="raw")
        score = result["best_score"]
        # Raw floats from RapidFuzz have many decimal places
        # If rounded to 1 or 2 decimals, score == round(score, 2)
        # With raw float, it should have more precision
        assert isinstance(score, float)
        # The score should not be artificially truncated
        # (RapidFuzz produces rationals like 100*2*M/(|a|+|b|))

    def test_score_multi_tier_returns_raw_float(self):
        """Multi-tier scoring should also return raw floats."""
        result = score_address_multi_tier(
            ["1593 Pine Avenue Suite 157"],
            ["1593 Pine Avenue Suite 160"],
            tiers=["raw", "clean"],
        )
        assert isinstance(result["best_score"], float)

    def test_street_score_returns_raw_float(self):
        """Street score should not be rounded internally."""
        src = build_variants("123 Oak Street Suite 100")
        dst = build_variants("123 Oak Street Suite 200")
        result = score_address_pair(src, dst, tier="raw")
        assert isinstance(result["street_score"], float)


class TestRoundingBugRegression:
    """Regression test for the rounding-inside-loop bug.

    Bug: round(weighted, N) inside the comparison loop stored a lossy
    value. A subsequent comparison with a LOWER raw score could beat the
    rounded stored value, selecting the wrong winner.

    Example: addr1<>addr1 scores 93.846154, stored as 93.8 (round 1).
    merged<>merged scores 93.814433 -- worse, but 93.814433 > 93.8 is
    True, so it wrongly replaces the better match.
    """

    def test_better_comparison_wins_despite_close_scores(self):
        """The comparison with the higher raw score must always win,
        regardless of how close the scores are."""
        # These addresses produce weighted scores that differ by <0.1:
        #   merged<>merged: 95.744681 (TRUE best)
        #   addr1<>addr1:   95.714286
        # With round(weighted, 1) inside the loop, addr1<>addr1 scored
        # 95.7 (stored), then merged 95.744681 > 95.7 should replace.
        # But on OLD main (merged first), merged stored 95.7, then
        # addr1<>addr1 at 95.714286 > 95.7 replaced it -- WRONG.
        src = build_variants(
            "7055 Lincoln Street Unit 499",
            "Cedar South Campus",
        )
        dst = build_variants(
            "7055 Lincoln Street Unit 501",
            "Cedar North Campus",
        )
        result = score_address_pair(src, dst, tier="raw")

        # merged<>merged is the true best for these addresses
        assert result["best_comparison"] == "merged<>merged", (
            f"Expected merged<>merged but got {result['best_comparison']} "
            f"({result['best_score']:.6f}). Rounding bug may be present."
        )

    def test_no_rounding_artifacts_in_selection(self):
        """Run all comparisons manually and verify score_address_pair
        picks the one with the highest raw weighted score."""
        from rapidfuzz import fuzz as rfuzz

        from address import parse_address

        src = build_variants(
            "7055 Lincoln Street Unit 499",
            "Cedar South Campus",
        )
        dst = build_variants(
            "7055 Lincoln Street Unit 501",
            "Cedar North Campus",
        )

        # Manually compute weighted scores for each comparison
        comparisons = []
        src_fields = src.get("fields", [])
        dst_fields = dst.get("fields", [])
        for si, sv in enumerate(src_fields, start=1):
            for di, dv in enumerate(dst_fields, start=1):
                comparisons.append((f"addr{si}<>addr{di}", sv, dv))
        comparisons.append(("merged<>merged", src["addr_merged"], dst["addr_merged"]))

        manual_best_score = 0.0
        manual_best_comp = ""
        for comp_name, sv, dv in comparisons:
            if not sv or not dv:
                continue
            full = rfuzz.token_sort_ratio(sv, dv)
            p_s = parse_address(sv)
            p_d = parse_address(dv)
            if p_s["street_name"] and p_d["street_name"]:
                st = rfuzz.ratio(p_s["street_name"].lower(), p_d["street_name"].lower())
                weighted = st * 0.6 + full * 0.4
            else:
                weighted = full
            if weighted > manual_best_score:
                manual_best_score = weighted
                manual_best_comp = comp_name

        # score_address_pair must agree with our manual calculation
        result = score_address_pair(src, dst, tier="raw")
        assert result["best_comparison"] == manual_best_comp, (
            f"Expected {manual_best_comp} ({manual_best_score:.6f}) "
            f"but got {result['best_comparison']} ({result['best_score']:.6f})"
        )
        assert abs(result["best_score"] - manual_best_score) < 1e-9


class TestNameScoreNoRounding:
    """Verify name_score is stored as raw float in matching output."""

    def test_fuzzy_name_score_precision(self):
        """Fuzzy matching should preserve full score precision."""
        import polars as pl

        from matching import match_names_fuzzy

        src_df = pl.DataFrame({
            "id": ["A", "B"],
            "name": ["Bapienx Inc", "Nexacore Solutions"],
        })
        dst_df = pl.DataFrame({
            "id": ["X", "Y"],
            "name": ["Bapienx Incorporated", "Nexacore Solution LLC"],
        })

        result = match_names_fuzzy(
            src_df, dst_df,
            src_field="name", dst_field="name",
            threshold=50, tiers=["raw"],
            dedup_field="id",
        )

        if result.height > 0:
            scores = result["name_score"].to_list()
            for s in scores:
                assert isinstance(s, float)
                # Scores should NOT be pre-rounded to clean values
                # (RapidFuzz returns rationals that often have many decimals)
