"""
Tests for src/signal_analysis.py

Runs signal analysis against our synthetic datasets and validates
column type detection, token analysis, stopword/alias suggestions,
and data quality profiling.

Results written to tests/results/signal_analysis_results.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl
from signal_analysis import (
    detect_column_type, top_tokens, suggest_stopwords,
    suggest_aliases, data_quality_summary, analyze_column, analyze_dataset,
)


def load_datasets():
    """Load our synthetic CSV datasets."""
    base = Path(__file__).parent / "data"
    core = pl.read_csv(str(base / "core_parent_export.csv"))
    multi = pl.read_csv(str(base / "tp_multi_pop_dataset.csv"))
    return core, multi


def test_detect_column_type():
    """Auto-detect column types from synthetic data."""
    _, multi = load_datasets()

    cases = [
        ("l3_fmly_nm", "name"),
        ("vendor_id", "id"),
        ("hq_addr1", "address"),
    ]
    results = []
    for col, expected in cases:
        actual = detect_column_type(multi[col])
        passed = actual == expected
        results.append({"column": col, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_top_tokens_raw():
    """Top tokens at Raw tier should preserve case."""
    core, _ = load_datasets()
    tokens = top_tokens(core["Vendor Name"], tier="raw", n=10)
    results = []

    # Should have mixed case tokens
    has_upper = any(t[0] != t[0].lower() for t, _ in tokens)
    results.append({"check": "has_uppercase_tokens", "passed": has_upper,
                     "actual": str(tokens[:5])})

    # Should have some results
    results.append({"check": "has_results", "passed": len(tokens) > 0,
                     "actual": len(tokens)})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_top_tokens_clean():
    """Top tokens at Clean tier should be lowercase."""
    core, _ = load_datasets()
    tokens = top_tokens(core["Vendor Name"], tier="clean", n=10)
    results = []

    # All tokens should be lowercase
    all_lower = all(t == t.lower() for t, _ in tokens)
    results.append({"check": "all_lowercase", "passed": all_lower,
                     "actual": str(tokens[:5])})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_suggest_stopwords():
    """Stopword suggestions should include known company suffixes."""
    _, multi = load_datasets()
    suggestions = suggest_stopwords(multi["l3_fmly_nm"], col_type="name")
    results = []

    tokens_found = {s["token"] for s in suggestions}

    # Should find common name suffixes
    for expected in ["inc", "llc"]:
        found = expected in tokens_found
        results.append({"check": f"found_{expected}", "passed": found,
                         "actual": str(tokens_found)})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_suggest_aliases():
    """Alias suggestions should find case/punctuation variants."""
    _, multi = load_datasets()
    aliases = suggest_aliases(multi["l3_fmly_nm"])
    results = []

    # Should find at least some alias groups
    results.append({"check": "has_aliases", "passed": len(aliases) > 0,
                     "actual": len(aliases)})

    # Each alias should have multiple variants
    if aliases:
        multi_variant = all(len(a["variants"]) > 1 for a in aliases)
        results.append({"check": "all_multi_variant", "passed": multi_variant,
                         "actual": str(aliases[0]) if aliases else "none"})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_data_quality():
    """Data quality summary should report nulls, uniques, lengths."""
    core, _ = load_datasets()
    summary = data_quality_summary(core, ["Vendor Name", "Address1", "Address2"])
    results = []

    # Vendor Name should have no nulls
    vn = summary.get("Vendor Name", {})
    results.append({"check": "vendor_name_no_nulls", "passed": vn.get("null_count", -1) == 0,
                     "actual": vn.get("null_count")})

    # Address2 should have some nulls (it's often empty)
    a2 = summary.get("Address2", {})
    results.append({"check": "address2_exists", "passed": "null_count" in a2,
                     "actual": str(a2)})

    # Should have length stats
    results.append({"check": "has_lengths", "passed": vn.get("lengths") is not None,
                     "actual": str(vn.get("lengths"))})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_analyze_column():
    """Full column analysis should return all expected fields."""
    _, multi = load_datasets()
    result = analyze_column(multi["l3_fmly_nm"], "l3_fmly_nm", unicode_mode="profile_only")
    results = []

    expected_keys = ["column", "detected_type", "top_tokens_raw", "top_tokens_clean",
                     "suggested_stopwords", "suggested_aliases", "unicode_profile"]
    for key in expected_keys:
        results.append({"check": f"has_{key}", "passed": key in result,
                         "actual": key in result})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_analyze_dataset():
    """Full dataset analysis with config output."""
    _, multi = load_datasets()
    out_dir = Path(__file__).parent / "results" / "signal_analysis_output"
    result = analyze_dataset(
        multi,
        columns=["l3_fmly_nm", "hq_addr1", "vendor_id"],
        unicode_mode="profile_only",
        output_dir=str(out_dir),
    )
    results = []

    # Should have data quality
    results.append({"check": "has_data_quality", "passed": "data_quality" in result,
                     "actual": "data_quality" in result})

    # Should have per-column results
    results.append({"check": "has_columns", "passed": len(result.get("columns", {})) == 3,
                     "actual": len(result.get("columns", {}))})

    # Should have aggregated outputs keyed by type
    agg_sw = result.get("aggregated_stopwords", {})
    results.append({"check": "has_stopwords", "passed": len(agg_sw) > 0,
                     "actual": len(agg_sw)})
    # Stopwords should be typed (name, address, etc.), not a flat list
    results.append({"check": "stopwords_typed", "passed": isinstance(agg_sw, dict),
                     "actual": type(agg_sw).__name__})

    # Should have written config files
    sw_path = out_dir / "stopwords.json"
    al_path = out_dir / "aliases.json"
    results.append({"check": "stopwords_file_written", "passed": sw_path.exists(),
                     "actual": str(sw_path)})
    results.append({"check": "aliases_file_written", "passed": al_path.exists(),
                     "actual": str(al_path)})

    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_detect_date_column():
    """Auto-detect date columns."""
    dates = pl.Series("dates", ["2026-01-15", "2025-12-01", "2026-03-22", "2024-06-10", None])
    results = []
    actual = detect_column_type(dates)
    results.append({"check": "detects_date", "passed": actual == "date", "actual": actual})

    us_dates = pl.Series("us", ["01/15/2026", "12/01/2025", "03/22/2026", "06/10/2024"])
    actual2 = detect_column_type(us_dates)
    results.append({"check": "detects_us_date", "passed": actual2 == "date", "actual": actual2})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_analyze_column_skip_unicode():
    """analyze_column with unicode_mode=skip should omit unicode_profile."""
    _, multi = load_datasets()
    result = analyze_column(multi["l3_fmly_nm"], "l3_fmly_nm", unicode_mode="skip")
    results = []
    results.append({"check": "no_unicode_profile", "passed": "unicode_profile" not in result,
                     "actual": "unicode_profile" in result})
    results.append({"check": "has_tokens", "passed": "top_tokens_raw" in result,
                     "actual": "top_tokens_raw" in result})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_top_tokens_invalid_tier():
    """top_tokens with invalid tier should raise ValueError."""
    core, _ = load_datasets()
    results = []
    try:
        top_tokens(core["Vendor Name"], tier="normalized")
        results.append({"check": "raises_error", "passed": False, "actual": "no exception"})
    except ValueError:
        results.append({"check": "raises_error", "passed": True, "actual": "ValueError raised"})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_empty_series():
    """Handle empty or all-null series gracefully."""
    empty = pl.Series("empty", [], dtype=pl.String)
    null_series = pl.Series("nulls", [None, None, None])

    results = []

    # Empty series
    col_type = detect_column_type(empty)
    results.append({"check": "empty_detect_type", "passed": col_type == "freetext",
                     "actual": col_type})

    tokens = top_tokens(empty, n=5)
    results.append({"check": "empty_top_tokens", "passed": len(tokens) == 0,
                     "actual": len(tokens)})

    # All-null series
    sw = suggest_stopwords(null_series)
    results.append({"check": "null_stopwords", "passed": len(sw) == 0,
                     "actual": len(sw)})

    for r in results:
        assert r["passed"], f"Failed: {r}"


def run_all():
    """Run all tests and write results."""
    all_results = {
        "test_detect_column_type": test_detect_column_type(),
        "test_top_tokens_raw": test_top_tokens_raw(),
        "test_top_tokens_clean": test_top_tokens_clean(),
        "test_suggest_stopwords": test_suggest_stopwords(),
        "test_suggest_aliases": test_suggest_aliases(),
        "test_data_quality": test_data_quality(),
        "test_analyze_column": test_analyze_column(),
        "test_analyze_dataset": test_analyze_dataset(),
        "test_detect_date_column": test_detect_date_column(),
        "test_analyze_column_skip_unicode": test_analyze_column_skip_unicode(),
        "test_top_tokens_invalid_tier": test_top_tokens_invalid_tier(),
        "test_empty_series": test_empty_series(),
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

    out_path = Path(__file__).parent / "results" / "signal_analysis_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"summary": summary}, f, indent=2, default=str)

    print(f"Tests: {passed}/{total} passed ({summary['pass_rate']})")
    if failed_details:
        print("FAILURES:")
        for fd in failed_details:
            print(f"  {fd['test']}: {fd.get('check', '')} expected={fd.get('expected', '')}, actual={fd.get('actual', '')}")
    return summary


if __name__ == "__main__":
    summary = run_all()
    sys.exit(0 if summary["failed"] == 0 else 1)
