"""Tests for N-field address support (Issue #111)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import polars as pl

from address import build_variants, score_address_multi_tier, score_address_pair
from matching import score_addresses_batch

# ---------------------------------------------------------------------------
# build_variants with 1, 2, 3, 4 fields
# ---------------------------------------------------------------------------

def test_build_variants_single_field():
    """Single field produces addr1_only and merged."""
    v = build_variants("123 Main St")
    assert v["addr1_only"] == "123 Main St"
    assert v["addr_merged"] == "123 Main St"
    assert v["fields"] == ["123 Main St"]
    assert "addr2_only" not in v


def test_build_variants_two_fields():
    """Two fields -- backward compatible with existing behavior."""
    v = build_variants("123 Main St", "New York NY 10001")
    assert v["addr1_only"] == "123 Main St"
    assert v["addr2_only"] == "New York NY 10001"
    assert v["addr_merged"] == "123 Main St New York NY 10001"
    assert v["fields"] == ["123 Main St", "New York NY 10001"]


def test_build_variants_three_fields():
    """Three fields -- street, city/state, country."""
    v = build_variants("123 Main St", "New York NY 10001", "US")
    assert v["addr1_only"] == "123 Main St"
    assert v["addr2_only"] == "New York NY 10001"
    assert v["addr3_only"] == "US"
    assert v["addr_merged"] == "123 Main St New York NY 10001 US"
    assert len(v["fields"]) == 3


def test_build_variants_four_fields():
    """Four fields -- extra granularity."""
    v = build_variants("Suite 100", "123 Main St", "New York NY", "US")
    assert v["addr4_only"] == "US"
    assert v["addr_merged"] == "Suite 100 123 Main St New York NY US"
    assert len(v["fields"]) == 4


def test_build_variants_empty_middle_field():
    """Empty middle field is skipped in merged but preserved in fields list."""
    v = build_variants("123 Main St", "", "US")
    assert v["addr_merged"] == "123 Main St US"
    assert v["fields"] == ["123 Main St", "", "US"]
    assert v["addr2_only"] == ""


# ---------------------------------------------------------------------------
# score_address_pair with N-field variants
# ---------------------------------------------------------------------------

def test_score_pair_three_fields_cross_compare():
    """3-field variants generate 9 individual + 1 merged comparison."""
    src = build_variants("123 Main St", "Suite 200", "New York NY")
    dst = build_variants("123 Main St", "Floor 3", "New York NY")
    result = score_address_pair(src, dst, tier="clean", parser="default")
    # addr1<>addr1 (123 Main St vs 123 Main St) should be perfect
    assert result["best_score"] >= 90


def test_score_pair_country_in_third_field():
    """Country in addr3 contributes to merged comparison."""
    src = build_variants("123 Main St", "Sydney NSW", "AU")
    dst = build_variants("123 Main St", "Sydney NSW", "AU")
    result = score_address_pair(src, dst, tier="clean", parser="default")
    assert result["best_score"] >= 95


def test_score_pair_different_country_lowers_merged():
    """Different country in addr3 lowers the merged score."""
    src = build_variants("123 Main St", "Sydney NSW", "AU")
    dst = build_variants("123 Main St", "Sydney NSW", "US")
    result_diff = score_address_pair(src, dst, tier="clean", parser="default")

    src_same = build_variants("123 Main St", "Sydney NSW", "AU")
    dst_same = build_variants("123 Main St", "Sydney NSW", "AU")
    result_same = score_address_pair(src_same, dst_same, tier="clean", parser="default")

    # Same-country should score >= different-country (merged includes country)
    assert result_same["best_score"] >= result_diff["best_score"]


# ---------------------------------------------------------------------------
# score_address_multi_tier with lists
# ---------------------------------------------------------------------------

def test_multi_tier_three_fields():
    """Multi-tier scoring with 3 source and 3 dest fields."""
    result = score_address_multi_tier(
        ["123 Main St", "Suite 200", "New York NY"],
        ["123 Main St", "Floor 3", "New York NY"],
        tiers=["clean"],
        parser="default",
    )
    assert result["best_score"] > 0
    assert result["tier_used"] == "clean"


def test_multi_tier_single_field():
    """Single field per side still works."""
    result = score_address_multi_tier(
        ["123 Main St"],
        ["123 Main St"],
        tiers=["clean"],
        parser="default",
    )
    assert result["best_score"] >= 95


def test_multi_tier_asymmetric_fields():
    """Different number of fields on source vs dest."""
    result = score_address_multi_tier(
        ["123 Main St", "New York NY", "US"],
        ["123 Main St New York NY US"],
        tiers=["clean"],
        parser="default",
    )
    # merged<>merged should catch this even with different field counts
    assert result["best_score"] >= 80


# ---------------------------------------------------------------------------
# score_addresses_batch with N columns
# ---------------------------------------------------------------------------

def test_batch_three_columns():
    """Batch scoring with 3 source and 3 dest columns."""
    df = pl.DataFrame({
        "src_a1": ["123 Main St"],
        "src_a2": ["Suite 200"],
        "src_a3": ["New York NY"],
        "dst_a1": ["123 Main St"],
        "dst_a2": ["Floor 3"],
        "dst_a3": ["New York NY"],
    })
    result = score_addresses_batch(
        df,
        ["src_a1", "src_a2", "src_a3"],
        ["dst_a1", "dst_a2", "dst_a3"],
        tiers=["clean"],
    )
    assert "addr_score" in result.columns
    assert result["addr_score"][0] >= 80


def test_batch_single_column():
    """Batch with just 1 column per side."""
    df = pl.DataFrame({
        "src_addr": ["123 Main St New York NY"],
        "dst_addr": ["123 Main St New York NY"],
    })
    result = score_addresses_batch(
        df, ["src_addr"], ["dst_addr"],
        tiers=["clean"],
    )
    assert result["addr_score"][0] >= 95


def test_batch_asymmetric_columns():
    """Source has 3 columns, dest has 2."""
    df = pl.DataFrame({
        "s1": ["123 Main St"],
        "s2": ["Suite 200"],
        "s3": ["New York NY"],
        "d1": ["123 Main St Suite 200"],
        "d2": ["New York NY"],
    })
    result = score_addresses_batch(
        df, ["s1", "s2", "s3"], ["d1", "d2"],
        tiers=["clean"],
    )
    assert "addr_score" in result.columns
    assert result["addr_score"][0] >= 70


def test_batch_backward_compat_two_columns():
    """Two-column batch still works (backward compat)."""
    df = pl.DataFrame({
        "src_a1": ["100 Harris St Level 2"],
        "src_a2": ["Sydney NSW 2000"],
        "dst_a1": ["100 Harris St Level 1"],
        "dst_a2": ["Sydney NSW 2000"],
    })
    result = score_addresses_batch(
        df, ["src_a1", "src_a2"], ["dst_a1", "dst_a2"],
        tiers=["clean"],
    )
    assert result["addr_score"][0] >= 80
    assert "addr_street_match" in result.columns
    assert "addr_comparison" in result.columns
    assert "addr_tier" in result.columns
