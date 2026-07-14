"""Tests for the emit_unmatched CSV/parquet companion export (Issue #66).

Covers write_unmatched_export (report.py) and apply_column_mapping's
analysis key: an unmatched companion file written next to the matched
raw export, columns resolved via output.columns.analysis (recipe-driven),
DW-importable (UTF-8 + header row), row count == unmatched count.
"""

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl

from matching import run_pipeline
from recipe import load_recipe, validate_recipe
from report import apply_column_mapping, write_unmatched_export

DATA_DIR = Path(__file__).parent.parent / "data"
RECIPE_PATH = Path(__file__).parent.parent / "config" / "recipes" / "l1_reconciliation.yaml"

# An output config whose analysis mapping references a field absent from the
# raw frame (reason_code) to prove reason columns are backfilled.
_OUTPUT_CFG = {
    "format": "csv",
    "columns": {
        "matched": [{"field": "vendor_id", "header": "Vendor ID"}],
        "analysis": [
            {"field": "vendor_id", "header": "Vendor ID"},
            {"field": "l3_fmly_nm", "header": "L3 Name"},
            {"field": "reason_code", "header": "Reason Code"},
        ],
    },
}


def _unmatched_frame():
    return pl.DataFrame(
        {"vendor_id": ["V1", "V2"], "l3_fmly_nm": ["Acme", "Globex"]}
    )


def _tmp(suffix):
    f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# write_unmatched_export
# ---------------------------------------------------------------------------

def test_writes_csv_with_analysis_columns():
    path = _tmp(".csv")
    written = write_unmatched_export(_unmatched_frame(), _OUTPUT_CFG, path, "csv")
    assert written == path
    out = pl.read_csv(path)
    # Columns are the analysis headers, in order
    assert out.columns == ["Vendor ID", "L3 Name", "Reason Code"]
    # Row count equals the unmatched count (== Analysis tab count)
    assert out.height == 2
    Path(path).unlink()


def test_reason_code_backfilled_when_absent():
    path = _tmp(".csv")
    write_unmatched_export(_unmatched_frame(), _OUTPUT_CFG, path, "csv")
    out = pl.read_csv(path)
    assert out["Reason Code"].to_list() == ["no_name_match", "no_name_match"]
    Path(path).unlink()


def test_writes_parquet():
    path = _tmp(".parquet")
    written = write_unmatched_export(_unmatched_frame(), _OUTPUT_CFG, path, "parquet")
    assert written == path
    out = pl.read_parquet(path)
    assert out.columns == ["Vendor ID", "L3 Name", "Reason Code"]
    assert out.height == 2
    Path(path).unlink()


def test_dw_importable_utf8_header_row():
    path = _tmp(".csv")
    write_unmatched_export(_unmatched_frame(), _OUTPUT_CFG, path, "csv")
    with open(path, encoding="utf-8") as f:
        header = f.readline().strip()
    assert header == "Vendor ID,L3 Name,Reason Code"
    Path(path).unlink()


def test_noop_on_empty_and_none():
    path = _tmp(".csv")
    Path(path).unlink()  # ensure absent
    assert write_unmatched_export(None, _OUTPUT_CFG, path, "csv") is None
    empty = _unmatched_frame().clear()
    assert write_unmatched_export(empty, _OUTPUT_CFG, path, "csv") is None
    assert not Path(path).exists()


def test_apply_column_mapping_analysis_key():
    df = _unmatched_frame().with_columns(pl.lit("no_name_match").alias("reason_code"))
    mapped = apply_column_mapping(df, _OUTPUT_CFG, key="analysis")
    assert mapped.columns == ["Vendor ID", "L3 Name", "Reason Code"]
    # Default key still resolves matched mapping
    assert apply_column_mapping(df, _OUTPUT_CFG).columns == ["Vendor ID"]


# ---------------------------------------------------------------------------
# End-to-end: companion row count matches the report's Analysis data
# ---------------------------------------------------------------------------

def test_rowcount_equals_pipeline_unmatched():
    recipe = load_recipe(str(RECIPE_PATH))
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    unmatched = result["unmatched"]
    path = _tmp(".csv")
    write_unmatched_export(unmatched, recipe["output"], path, "csv")
    out = pl.read_csv(path)
    assert out.height == unmatched.height
    Path(path).unlink()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_accepts_emit_unmatched():
    recipe = load_recipe(str(RECIPE_PATH))
    recipe = copy.deepcopy(recipe)
    recipe["output"]["emit_unmatched"] = True
    # No additionalProperties warning for emit_unmatched
    warnings = validate_recipe(recipe)
    assert not any("emit_unmatched" in w for w in warnings)
