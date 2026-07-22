"""
Tests for src/normalize.py

Runs all normalization tiers and unicode profiling against synthetic data
and edge cases. Results written to tests/results/normalize_results.json
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from normalize import (
    raw, clean, normalized, apply_tier,
    classify_codepoint, profile_string, normalize_unicode,
)


def test_raw():
    """Raw tier should return value unchanged."""
    cases = [
        ("  BAPIENX INC  ", "  BAPIENX INC  "),
        ("nexacore solutions llc,", "nexacore solutions llc,"),
        ("", ""),
        (None, ""),
    ]
    results = []
    for input_val, expected in cases:
        actual = raw(input_val)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_clean():
    """Clean tier: lowercase, strip, remove trailing punct."""
    cases = [
        ("BAPIENX INC", "bapienx inc"),
        ("  Nexacore Solutions LLC,  ", "nexacore solutions llc"),
        ("Vanteon Systems, Inc.", "vanteon systems inc"),
        ("Caldris Technologies, Inc", "caldris technologies inc"),
        ("  multiple   spaces  here  ", "multiple spaces here"),
        ("trailing comma,", "trailing comma"),
        ("trailing period.", "trailing period"),
        ("trailing semicolon;", "trailing semicolon"),
        ("Pelomar Consulting, LLC", "pelomar consulting llc"),
        ("Bapienx, Inc.", "bapienx inc"),
        ("alpha-beta", "alpha beta"),
        ("", ""),
        (None, ""),
    ]
    results = []
    for input_val, expected in cases:
        actual = clean(input_val)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_normalized():
    """Normalized tier: clean + aliases + stopwords."""
    aliases = {
        "blvd": "boulevard",
        "st": "street",
        "ave": "avenue",
        "dr": "drive",
        "n": "north",
        "ste": "suite",
        "fl": "floor",
        "pkwy": "parkway",
    }
    stopwords = ["inc", "llc", "ltd", "corp", "the"]

    cases = [
        ("500 Technology Dr Ste 200", "500 technology drive suite 200"),
        ("194 6TH AVE FL 7", "194 6th avenue floor 7"),
        ("1200 Commerce Blvd", "1200 commerce boulevard"),
        ("3300 N Central Expy", "3300 north central expy"),  # expy not in aliases
        ("BAPIENX INC", "bapienx"),  # stopword removed
        ("The Nexacore Group LLC", "nexacore group"),  # two stopwords
    ]
    results = []
    for input_val, expected in cases:
        actual = normalized(input_val, aliases, stopwords)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_unicode_classify():
    """Unicode codepoint classification."""
    cases = [
        (ord("A"), "ascii_alnum"),
        (ord("z"), "ascii_alnum"),
        (ord("5"), "ascii_alnum"),
        (ord(" "), "ascii_punct_space"),
        (ord(","), "ascii_punct_space"),
        (ord("\u00e9"), "latin"),          # e with accent (Latin Extended)
        (ord("\u00f1"), "latin"),          # n with tilde
        (ord("\u0410"), "cyrillic"),       # Cyrillic A
        (ord("\u4e00"), "cjk"),            # CJK unified ideograph
        (ord("\u3042"), "cjk"),            # Hiragana a
        (ord("\uac00"), "cjk"),            # Hangul syllable
        (ord("\u0391"), "greek"),          # Greek Alpha
        (0xFF21, "latin"),                 # Fullwidth A (in latin range)
        (0x1F600, "unknown"),             # Emoji (not in any range)
    ]
    results = []
    for cp, expected in cases:
        actual = classify_codepoint(cp)
        passed = actual == expected
        results.append({
            "codepoint": f"U+{cp:04X}",
            "char": chr(cp) if cp < 0x10000 else f"U+{cp:X}",
            "expected": expected,
            "actual": actual,
            "passed": passed,
        })
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_profile_string():
    """Profile string by unicode buckets."""
    cases = [
        ("Hello World", {"ascii_alnum": 10, "ascii_punct_space": 1}),
        ("Caf\u00e9", {"ascii_alnum": 3, "latin": 1}),
        ("\u041f\u0440\u0438\u0432\u0435\u0442", {"cyrillic": 6}),  # Privet in Russian
        ("\u4f60\u597d", {"cjk": 2}),  # Nihao in Chinese
        ("Mix\u00e9d\u0410\u4e00", {"ascii_alnum": 4, "latin": 1, "cyrillic": 1, "cjk": 1}),  # Mixed scripts
    ]
    results = []
    for input_val, expected_buckets in cases:
        profile = profile_string(input_val)
        # Check expected buckets are present with correct counts
        passed = True
        for bucket, count in expected_buckets.items():
            if profile.get(bucket, 0) != count:
                passed = False
                break
        results.append({
            "input": input_val,
            "expected_buckets": expected_buckets,
            "actual_profile": {k: v for k, v in profile.items() if not k.startswith("_") and v > 0},
            "unknown_pct": profile["_unknown_pct"],
            "passed": passed,
        })
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_unicode_normalize():
    """Unicode normalization (NFKC)."""
    cases = [
        ("\uff21\uff22\uff23", "ABC"),               # Fullwidth -> ASCII
        ("\uff11\uff12\uff13", "123"),               # Fullwidth digits
        ("Caf\u00e9", "Caf\u00e9"),                  # Standard accent preserved
        ("normal text", "normal text"),               # No change
    ]
    results = []
    for input_val, expected in cases:
        actual = normalize_unicode(input_val)
        passed = actual == expected
        results.append({"input": repr(input_val), "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_normalized_edge_cases():
    """Edge cases for normalized tier."""
    aliases = {"st": "street", "blvd": "boulevard"}
    stopwords = ["inc", "llc"]
    cases = [
        # None aliases/stopwords should not crash
        ("test value", None, None, "test value"),
        # Empty/whitespace
        ("   ", {}, [], ""),
        # Alias word-boundary: 'st' should NOT mangle 'boston' or 'first'
        ("123 boston st", aliases, [], "123 boston street"),
        ("first st", aliases, [], "first street"),
        # Stopwords with frozenset
        ("Bapienx Inc", {}, frozenset({"inc"}), "bapienx"),
    ]
    results = []
    for input_val, als, sw, expected in cases:
        actual = normalized(input_val, als, sw)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_profile_column():
    """Test profile_column with plain list (no Polars dependency)."""
    from normalize import profile_column
    values = [
        "Hello World",
        "Caf\u00e9",
        "\u041f\u0440\u0438\u0432\u0435\u0442",  # Cyrillic
        None,
        "",
        "Normal text 123",
    ]
    result = profile_column(values)
    checks = [
        ("total_cells", result["total_cells"] == 4, result["total_cells"]),  # None and empty skipped
        ("has_latin", "latin" in result["bucket_totals"], result["bucket_totals"].keys()),
        ("has_cyrillic", "cyrillic" in result["bucket_totals"], result["bucket_totals"].keys()),
        ("mixed_script", result["mixed_script_cells"] == 0, result["mixed_script_cells"]),
    ]
    results = []
    for name, passed, actual in checks:
        results.append({"check": name, "passed": passed, "actual": str(actual)})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_clean_unicode_input():
    """Clean tier with accented/unicode input."""
    cases = [
        ("Caf\u00e9 R\u00c9SUM\u00c9,", "caf\u00e9 r\u00e9sum\u00e9"),
        ("\u00d1o\u00f1o", "\u00f1o\u00f1o"),  # Spanish n with tilde
    ]
    results = []
    for input_val, expected in cases:
        actual = clean(input_val)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_profile_column_all_none():
    """profile_column with all None/empty values."""
    from normalize import profile_column
    values = [None, "", "   ", None]
    result = profile_column(values)
    checks = [
        ("total_cells_zero", result["total_cells"] == 0, result["total_cells"]),
        ("no_flags", len(result["flagged_indices"]) == 0, result["flagged_indices"]),
        ("unknown_pct_zero", result["cells_with_unknown_pct"] == 0.0, result["cells_with_unknown_pct"]),
    ]
    results = []
    for name, passed, actual in checks:
        results.append({"check": name, "passed": passed, "actual": str(actual)})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_normalized_empty_aliases():
    """normalized() with empty dict aliases and empty list stopwords."""
    cases = [
        ("test value", {}, [], "test value"),
        ("BAPIENX INC", {}, [], "bapienx inc"),
    ]
    results = []
    for input_val, als, sw, expected in cases:
        actual = normalized(input_val, als, sw)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_invalid_tier():
    """apply_tier with invalid tier name should raise ValueError."""
    results = []
    try:
        apply_tier("test", "bogus")
        results.append({"check": "raises_ValueError", "passed": False, "actual": "no exception"})
    except ValueError:
        results.append({"check": "raises_ValueError", "passed": True, "actual": "ValueError raised"})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_apply_tier_normalized():
    """apply_tier with normalized tier + aliases/stopwords."""
    from normalize import compile_aliases
    aliases = {"blvd": "boulevard", "st": "street"}
    stopwords = ["inc", "llc"]
    cases = [
        # Dict aliases
        ("500 Technology Blvd Inc", "normalized", aliases, stopwords, "skip", "500 technology boulevard"),
        # Pre-compiled aliases
        ("500 Technology Blvd Inc", "normalized", compile_aliases(aliases), stopwords, "skip", "500 technology boulevard"),
    ]
    results = []
    for input_val, tier, als, sw, um, expected in cases:
        actual = apply_tier(input_val, tier, aliases=als, stopwords=sw, unicode_mode=um)
        passed = actual == expected
        results.append({"input": input_val, "expected": expected, "actual": actual, "passed": passed})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_profile_column_mixed_script():
    """profile_column should detect mixed-script cells."""
    from normalize import profile_column
    values = [
        "Normal English text",
        "Mix\u00e9d\u0410\u4e00",  # Latin + Cyrillic + CJK in one cell
        "Pure \u041f\u0440\u0438\u0432\u0435\u0442",  # Cyrillic only (with ASCII space)
    ]
    result = profile_column(values)
    checks = [
        ("total_cells", result["total_cells"] == 3, result["total_cells"]),
        ("mixed_script_detected", result["mixed_script_cells"] >= 1, result["mixed_script_cells"]),
        ("mixed_cell_flagged", 1 in result["flagged_indices"], result["flagged_indices"]),
    ]
    results = []
    for name, passed, actual in checks:
        results.append({"check": name, "passed": passed, "actual": str(actual)})
    for r in results:
        assert r["passed"], f"Failed: {r}"


def test_apply_tier():
    """apply_tier convenience function."""
    cases = [
        ("  TEST  ", "raw", "skip", "  TEST  "),
        ("  TEST  ", "clean", "skip", "test"),
        ("\uff34\uff25\uff33\uff34", "clean", "normalize", "test"),  # Fullwidth + clean
        ("\uff34\uff25\uff33\uff34", "clean", "skip", "\uff54\uff45\uff53\uff54"),  # Fullwidth without normalize (still lowercased by clean)
    ]
    results = []
    for input_val, tier, unicode_mode, expected in cases:
        actual = apply_tier(input_val, tier, unicode_mode=unicode_mode)
        passed = actual == expected
        results.append({
            "input": repr(input_val),
            "tier": tier,
            "unicode_mode": unicode_mode,
            "expected": expected,
            "actual": actual,
            "passed": passed,
        })
    for r in results:
        assert r["passed"], f"Failed: {r}"


def run_all():
    """Run all tests and write results."""
    all_results = {
        "test_raw": test_raw(),
        "test_clean": test_clean(),
        "test_clean_unicode_input": test_clean_unicode_input(),
        "test_normalized": test_normalized(),
        "test_normalized_edge_cases": test_normalized_edge_cases(),
        "test_unicode_classify": test_unicode_classify(),
        "test_profile_string": test_profile_string(),
        "test_profile_column": test_profile_column(),
        "test_unicode_normalize": test_unicode_normalize(),
        "test_apply_tier": test_apply_tier(),
        "test_apply_tier_normalized": test_apply_tier_normalized(),
        "test_invalid_tier": test_invalid_tier(),
        "test_profile_column_mixed_script": test_profile_column_mixed_script(),
        "test_profile_column_all_none": test_profile_column_all_none(),
        "test_normalized_empty_aliases": test_normalized_empty_aliases(),
    }

    # Summary
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

    output = {"summary": summary, "results": all_results}

    out_path = Path(__file__).parent / "results" / "normalize_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    # Print summary only
    print(f"Tests: {passed}/{total} passed ({summary['pass_rate']})")
    if failed_details:
        print(f"FAILURES:")
        for fd in failed_details:
            print(f"  {fd['test']}: expected={fd.get('expected')}, actual={fd.get('actual')}")
    return summary


if __name__ == "__main__":
    summary = run_all()
    sys.exit(0 if summary["failed"] == 0 else 1)
