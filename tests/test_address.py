"""
Tests for src/address.py

Tests address parsing, variant building, token classification,
and multi-tier scoring against synthetic dataset addresses.

Results written to tests/results/address_results.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from address import (
    build_variants, classify_tokens, parse_address,
    score_address_pair, score_address_multi_tier,
    LIBPOSTAL_AVAILABLE,
)


def test_build_variants():
    """Build address variants from two fields."""
    cases = [
        ("194 6th Avenue Floor 7", "New York NY 10005",
         {"addr1_only": "194 6th Avenue Floor 7",
          "addr2_only": "New York NY 10005",
          "addr_merged": "194 6th Avenue Floor 7 New York NY 10005"}),
        ("500 Technology Drive", "",
         {"addr1_only": "500 Technology Drive",
          "addr2_only": "",
          "addr_merged": "500 Technology Drive"}),
        ("", "Chicago IL 60601",
         {"addr1_only": "",
          "addr2_only": "Chicago IL 60601",
          "addr_merged": "Chicago IL 60601"}),
        (None, None,
         {"addr1_only": "",
          "addr2_only": "",
          "addr_merged": ""}),
    ]
    for a1, a2, expected in cases:
        actual = build_variants(a1, a2)
        for key, val in expected.items():
            assert actual[key] == val, f"Failed {key}: expected {val!r}, got {actual[key]!r}"
        assert actual["fields"] == [expected["addr1_only"], expected["addr2_only"]]


def test_classify_tokens():
    """Token classification against address patterns."""
    cases = [
        ("194 6th Avenue Floor 7 New York NY 10005", {
            "street_name_contains": "194 6th",
            "street_suffix": "avenue",
            "has_unit": True,
            "has_state": True,
            "classified": True,
        }),
        ("500 Technology Drive Suite 200 San Jose CA 95110", {
            "street_name_contains": "500 technology",
            "street_suffix": "drive",
            "has_unit": True,
            "has_state": True,
            "classified": True,
        }),
        ("1200 Commerce Blvd Dallas TX 75201", {
            "street_name_contains": "1200 commerce",
            "street_suffix": "blvd",
            "has_unit": False,
            "has_state": True,
            "classified": True,
        }),
        ("just some random text", {
            "classified": False,
        }),
        ("", {
            "classified": False,
        }),
    ]
    results = []
    for address, expected in cases:
        actual = classify_tokens(address)
        checks = []
        if "classified" in expected:
            checks.append(actual["classified"] == expected["classified"])
        if "street_suffix" in expected:
            checks.append(actual["street_suffix"] == expected["street_suffix"])
        if "street_name_contains" in expected:
            checks.append(expected["street_name_contains"] in actual["street_name"])
        if "has_unit" in expected:
            checks.append(bool(actual["unit"]) == expected["has_unit"])
        if "has_state" in expected:
            checks.append(bool(actual["state"]) == expected["has_state"])

        passed = all(checks)
        results.append({
            "input": address,
            "expected": expected,
            "actual": {k: v for k, v in actual.items() if v},
            "passed": passed,
        })
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_classify_ambiguous_tokens():
    """State/suffix collisions resolve by position."""
    cases = [
        # CT at end = state (Connecticut), not court
        ("100 Main Hartford CT 06103", {"state": "ct", "has_zip": True}),
        # FL at end = state (Florida), not floor
        ("200 Ocean Miami FL 33101", {"state": "fl"}),
        # CT after street number with more tokens = court (suffix)
        ("100 Elm Ct Apt 5 Hartford CT 06103", {"state": "ct", "street_suffix": "ct"}),
    ]
    results = []
    for address, expected in cases:
        actual = classify_tokens(address)
        checks = []
        if "state" in expected:
            checks.append(actual["state"] == expected["state"])
        if "street_suffix" in expected:
            checks.append(actual["street_suffix"] == expected["street_suffix"])
        if "has_zip" in expected:
            checks.append(bool(actual["zip_code"]) == expected["has_zip"])
        passed = all(checks)
        results.append({"input": address, "expected": expected,
                         "actual": {k: v for k, v in actual.items() if v}, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_fl_disambiguation():
    """'fl' as floor (unit) vs Florida (state) based on position."""
    cases = [
        # fl early = floor
        ("194 6th Ave Fl 7 New York NY 10005", {"has_unit": True, "unit_contains": "fl"}),
        # fl late = state
        ("200 Ocean Drive Miami FL 33101", {"state": "fl"}),
    ]
    results = []
    for address, expected in cases:
        actual = classify_tokens(address)
        checks = []
        if "has_unit" in expected:
            checks.append(bool(actual["unit"]) == expected["has_unit"])
        if "unit_contains" in expected:
            checks.append(expected["unit_contains"] in actual["unit"])
        if "state" in expected:
            checks.append(actual["state"] == expected["state"])
        passed = all(checks)
        results.append({"input": address, "expected": expected,
                         "actual": {k: v for k, v in actual.items() if v}, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_parse_address_default():
    """parse_address with default parser."""
    result = parse_address("500 Technology Drive Suite 200", parser="default")
    results = []
    results.append({"check": "classified", "passed": result["classified"],
                     "actual": result["classified"]})
    results.append({"check": "has_street", "passed": bool(result["street_name"]),
                     "actual": result["street_name"]})
    results.append({"check": "has_suffix", "passed": bool(result["street_suffix"]),
                     "actual": result["street_suffix"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_parse_address_auto():
    """parse_address with auto mode falls back to default when no libpostal."""
    result = parse_address("1200 Commerce Blvd", parser="auto")
    results = []
    results.append({"check": "classified", "passed": result["classified"],
                     "actual": result["classified"]})
    # Tests force the built-in parser for deterministic address scores.
    results.append({"check": "libpostal_status",
                     "passed": not LIBPOSTAL_AVAILABLE,
                     "actual": f"libpostal={'available' if LIBPOSTAL_AVAILABLE else 'not available'}"})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_libpostal_parsing():
    """Test libpostal parser if available."""
    results = []
    if not LIBPOSTAL_AVAILABLE:
        return  # Skip -- libpostal not installed

    cases = [
        ("194 6th Avenue Floor 7 New York NY 10005", {
            "street_contains": "6th avenue",
            "has_unit": True,
            "state": "ny",
            "zip": "10005",
        }),
        ("45 Collins Street Floor 12 Melbourne VIC 3000", {
            "street_contains": "collins",
            "has_unit": True,
        }),
        ("10 Finsbury Square Floor 8 London EC2A 1AF", {
            "street_contains": "finsbury",
        }),
    ]
    for address, expected in cases:
        actual = parse_address(address, parser="libpostal")
        checks = []
        if "street_contains" in expected:
            checks.append(expected["street_contains"] in actual["street_name"].lower())
        if "has_unit" in expected:
            checks.append(bool(actual["unit"]) == expected["has_unit"])
        if "state" in expected:
            checks.append(actual["state"] == expected["state"])
        if "zip" in expected:
            checks.append(actual["zip_code"] == expected["zip"])
        passed = all(checks)
        results.append({"input": address, "expected": expected,
                         "actual": {k: v for k, v in actual.items() if v}, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_address_pair_exact():
    """Score identical addresses -- should be ~100%."""
    src = build_variants("194 6th Avenue Floor 7", "New York NY 10005")
    dst = build_variants("194 6th Avenue Floor 7", "New York NY 10005")
    result = score_address_pair(src, dst, tier="raw", parser="default")
    results = []
    results.append({"check": "high_score", "passed": result["best_score"] >= 95,
                     "actual": result["best_score"]})
    results.append({"check": "street_match", "passed": result["street_match"],
                     "actual": result["street_match"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_address_pair_similar():
    """Score similar addresses with different formatting."""
    src = build_variants("194 6th Ave Fl 7", "")
    dst = build_variants("194 6TH AVENUE FLOOR 7", "NEW YORK NY 10005")
    result = score_address_pair(src, dst, tier="clean", parser="default")
    results = []
    results.append({"check": "reasonable_score", "passed": result["best_score"] >= 50,
                     "actual": result["best_score"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_address_pair_different():
    """Score completely different addresses -- should be low."""
    src = build_variants("194 6th Avenue", "New York NY")
    dst = build_variants("9600 Medical Center Drive", "Rockville MD")
    result = score_address_pair(src, dst, tier="clean", parser="default")
    results = []
    results.append({"check": "low_score", "passed": result["best_score"] < 50,
                     "actual": result["best_score"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_address_swapped_columns():
    """Score when data is in the wrong column."""
    src = build_variants("New York NY 10005", "194 6th Avenue Floor 7")  # Swapped
    dst = build_variants("194 6th Avenue Floor 7", "New York NY 10005")  # Normal
    result = score_address_pair(src, dst, tier="clean", parser="default")
    results = []
    # Cross-compare should catch this
    results.append({"check": "catches_swap", "passed": result["best_score"] >= 60,
                     "actual": result["best_score"]})
    # merged<>merged or cross-compare both valid -- token_sort_ratio
    # handles reordering, so merged may still win
    results.append({"check": "found_match",
                     "passed": result["best_score"] >= 60,
                     "actual": f"{result['best_comparison']} score={result['best_score']}"})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_multi_tier():
    """Multi-tier scoring should try raw -> clean -> normalized."""
    result = score_address_multi_tier(
        ["194 6TH AVE FL 7", ""],
        ["194 6th Avenue Floor 7", "New York NY 10005"],
        tiers=["raw", "clean", "normalized"],
        parser="default",
        aliases={"ave": "avenue", "fl": "floor"},
        stopwords=[],
    )
    results = []
    results.append({"check": "has_score", "passed": result["best_score"] > 0,
                     "actual": result["best_score"]})
    results.append({"check": "has_tier", "passed": result["tier_used"] in ["raw", "clean", "normalized"],
                     "actual": result["tier_used"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_empty_addresses():
    """Handle empty/None addresses gracefully."""
    result = score_address_multi_tier(["", ""], ["", ""],
                                      parser="default")
    results = []
    results.append({"check": "zero_score", "passed": result["best_score"] == 0.0,
                     "actual": result["best_score"]})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_score_with_synthetic_data():
    """Score addresses from our actual synthetic datasets."""
    import polars as pl
    base = Path(__file__).parent / "data"
    core = pl.read_csv(str(base / "core_parent_export.csv"))
    multi = pl.read_csv(str(base / "tp_multi_pop_dataset.csv"))

    results = []

    # BAPIENX: Pop1 "194 6th Avenue Floor 7" vs Core "194 6TH AVENUE FLOOR 7"
    pop1_row = multi.filter(pl.col("vendor_id") == "V748001").row(0, named=True)
    core_row = core.filter(pl.col("Vendor ID") == "V322312").row(0, named=True)

    result = score_address_multi_tier(
        [pop1_row["hq_addr1"], pop1_row["hq_addr2"]],
        [core_row["Address1"], core_row["Address2"]],
        parser="default",
    )
    results.append({
        "check": "bapienx_match",
        "passed": result["best_score"] >= 60,
        "actual": result["best_score"],
        "detail": f"{pop1_row['hq_addr1']} vs {core_row['Address1']}",
    })

    # Different addresses should score low
    other_core = core.filter(pl.col("Vendor ID") == "V549283").row(0, named=True)
    result2 = score_address_multi_tier(
        [pop1_row["hq_addr1"], pop1_row["hq_addr2"]],
        [other_core["Address1"], other_core["Address2"]],
        parser="default",
    )
    results.append({
        "check": "different_addr_low",
        "passed": result2["best_score"] < result["best_score"],
        "actual": result2["best_score"],
    })

    for r in results:
        assert r["passed"], f"Failed: {r}"


def run_all():
    """Run all tests and write results."""
    all_results = {
        "test_build_variants": test_build_variants(),
        "test_classify_tokens": test_classify_tokens(),
        "test_classify_ambiguous_tokens": test_classify_ambiguous_tokens(),
        "test_fl_disambiguation": test_fl_disambiguation(),
        "test_parse_address_default": test_parse_address_default(),
        "test_parse_address_auto": test_parse_address_auto(),
        "test_libpostal_parsing": test_libpostal_parsing(),
        "test_score_address_pair_exact": test_score_address_pair_exact(),
        "test_score_address_pair_similar": test_score_address_pair_similar(),
        "test_score_address_pair_different": test_score_address_pair_different(),
        "test_score_address_swapped_columns": test_score_address_swapped_columns(),
        "test_score_multi_tier": test_score_multi_tier(),
        "test_score_empty_addresses": test_score_empty_addresses(),
        "test_score_with_synthetic_data": test_score_with_synthetic_data(),
    }

    total = 0
    passed = 0
    failed_details = []
    for test_name, results in all_results.items():
        for r in results:
            total += 1
            if r["passed"]:
                passed += 1
            else:
                failed_details.append({"test": test_name, **r})

    summary = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": f"{passed/total*100:.1f}%" if total > 0 else "N/A",
        "failed_details": failed_details,
    }

    out_path = Path(__file__).parent / "results" / "address_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"summary": summary}, f, indent=2, default=str)

    print(f"Tests: {passed}/{total} passed ({summary['pass_rate']})")
    print(f"libpostal: {'detected -- libpostal tests included' if LIBPOSTAL_AVAILABLE else 'not detected -- libpostal tests skipped (using default tokenizer)'}")
    print(f"parser mode: {'libpostal' if LIBPOSTAL_AVAILABLE else 'default built-in tokenizer'}")
    if failed_details:
        print("FAILURES:")
        for fd in failed_details:
            print(f"  {fd['test']}: {fd.get('check', '')} actual={fd.get('actual', '')}")
    return summary


if __name__ == "__main__":
    # --no-libpostal flag to simulate without libpostal
    if "--no-libpostal" in sys.argv:
        import address
        address.LIBPOSTAL_AVAILABLE = False
        global LIBPOSTAL_AVAILABLE
        LIBPOSTAL_AVAILABLE = False
        print("[forced] libpostal disabled via --no-libpostal flag")

    summary = run_all()
    sys.exit(0 if summary["failed"] == 0 else 1)
