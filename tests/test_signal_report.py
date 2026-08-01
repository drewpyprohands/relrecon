"""Tests for signal analysis report formatter and CLI integration."""

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from signal_report import format_report


DATA_DIR = Path(__file__).parent / "data"


def _mock_results():
    """Build a minimal analyze_dataset() result for testing."""
    return {
        "data_quality": {
            "test_col": {
                "total_rows": 100,
                "null_count": 5,
                "null_pct": 5.0,
                "non_null": 95,
                "unique_count": 80,
                "unique_pct": 80.0,
                "duplicate_count": 20,
                "lengths": {"min": 3, "max": 50, "mean": 15.2},
            }
        },
        "columns": {
            "test_col": {
                "detected_type": "name",
                "top_tokens_raw": [("Inc", 20), ("LLC", 15), ("Corp", 5)],
                "top_tokens_clean": [("inc", 22), ("llc", 15), ("corp", 5)],
                "suggested_stopwords": [
                    {"token": "inc", "frequency": 0.32, "count": 32, "known": True},
                    {"token": "llc", "frequency": 0.25, "count": 25, "known": True},
                ],
                "suggested_aliases": [
                    {
                        "canonical": "inc",
                        "variants": [
                            {"raw": "Inc", "count": 20},
                            {"raw": "Inc.", "count": 2},
                        ],
                        "total_count": 22,
                    }
                ],
                "unicode_profile": {
                    "total_cells": 100,
                    "cells_with_unknown": 0,
                    "cells_with_unknown_pct": 0.0,
                    "mixed_script_cells": 0,
                    "flagged_indices": [],
                    "bucket_totals": {"ascii_alnum": 500},
                },
            }
        },
        "aggregated_stopwords": {"name": ["corp", "inc", "llc"]},
        "aggregated_aliases": {},
    }


def test_format_report_has_header():
    report = format_report(_mock_results(), file_path="data/test.csv", columns=["test_col"])
    assert "# Signal Analysis Report" in report
    assert "data/test.csv" in report
    assert "test_col" in report


def test_format_report_data_quality():
    report = format_report(_mock_results())
    assert "## Data Quality" in report
    assert "5.0%" in report
    assert "80.0%" in report


def test_format_report_top_tokens():
    report = format_report(_mock_results())
    assert "Top tokens (raw)" in report
    assert "| Inc | 20 |" in report
    assert "Top tokens (clean)" in report
    assert "| inc | 22 |" in report


def test_format_report_stopwords():
    report = format_report(_mock_results())
    assert "Suggested stopwords" in report
    assert "| inc | 32% | yes |" in report


def test_format_report_aliases():
    report = format_report(_mock_results())
    assert "Alias groups" in report
    assert "Inc (20)" in report


def test_format_report_aggregated():
    report = format_report(_mock_results())
    assert "Aggregated Suggestions" in report
    assert "corp, inc, llc" in report


def test_format_report_unicode_flags_hidden_when_clean():
    """Unicode section should not appear when there are no flags."""
    report = format_report(_mock_results())
    assert "Unicode flags" not in report


def test_format_report_unicode_flags_shown():
    results = _mock_results()
    results["columns"]["test_col"]["unicode_profile"]["cells_with_unknown"] = 3
    results["columns"]["test_col"]["unicode_profile"]["cells_with_unknown_pct"] = 3.0
    report = format_report(results)
    assert "Unicode flags" in report
    assert "3 cells" in report


def test_cli_analyze_runs():
    """Integration: --analyze flag runs without error."""
    result = subprocess.run(
        [sys.executable, "-m", "src", "--analyze", str(DATA_DIR / "core_parent_export.csv"),
         "--columns", "Vendor Name"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 0
    assert "Signal Analysis Report" in result.stdout
    assert "Vendor Name" in result.stdout


def test_cli_analyze_auto_select():
    """Integration: auto-selects name/address columns with --columns auto."""
    result = subprocess.run(
        [sys.executable, "-m", "src", "--analyze", str(DATA_DIR / "core_parent_export.csv"),
         "--columns", "auto"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 0
    assert "Auto-selected columns" in result.stdout


def test_cli_analyze_bad_file():
    result = subprocess.run(
        [sys.executable, "-m", "src", "--analyze", str(DATA_DIR / "nonexistent.csv")],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 1


def test_cli_analyze_bad_column():
    result = subprocess.run(
        [sys.executable, "-m", "src", "--analyze", str(DATA_DIR / "core_parent_export.csv"),
         "--columns", "nonexistent_col"],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 1
    assert "not found" in result.stderr
