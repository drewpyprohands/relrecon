"""
Tests for Issue #50: generic step filters (replaces date_gate).

Validates that:
- Legacy date_gate is converted to filters internally
- Generic filter ops (eq, contains, max_age_years) work on steps
- applies_to controls source vs destination filtering
- Both date_gate and filters can coexist
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl
from matching import (
    _normalize_step_filters,
    _apply_step_filter,
    run_matching_step,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# Test: date_gate normalization
# ---------------------------------------------------------------------------

def test_normalize_date_gate_to_filters():
    """Legacy date_gate should be converted to a filters entry."""
    step = {
        "name": "Test",
        "date_gate": {
            "field": "Updated",
            "max_age_years": 2,
            "applies_to": "destination",
        },
        "match_fields": [{"source": "a", "destination": "b", "method": "exact"}],
    }
    result = _normalize_step_filters(step)

    assert "date_gate" not in result
    assert len(result["filters"]) == 1
    f = result["filters"][0]
    assert f["field"] == "Updated"
    assert f["op"] == "max_age_years"
    assert f["value"] == 2
    assert f["applies_to"] == "destination"


def test_normalize_preserves_existing_filters():
    """date_gate should be appended to existing filters, not replace them."""
    step = {
        "name": "Test",
        "filters": [
            {"field": "status", "op": "eq", "value": "active", "applies_to": "destination"},
        ],
        "date_gate": {
            "field": "Updated",
            "max_age_years": 2,
            "applies_to": "destination",
        },
        "match_fields": [{"source": "a", "destination": "b", "method": "exact"}],
    }
    result = _normalize_step_filters(step)

    assert len(result["filters"]) == 2
    assert result["filters"][0]["op"] == "eq"
    assert result["filters"][1]["op"] == "max_age_years"


def test_normalize_no_date_gate_passthrough():
    """Without date_gate, step_config is returned as-is."""
    step = {"name": "Test", "match_fields": []}
    result = _normalize_step_filters(step)
    assert result is step  # same object, not copied


# ---------------------------------------------------------------------------
# Test: generic filter ops on DataFrames
# ---------------------------------------------------------------------------

def test_apply_filter_eq():
    """Filter op 'eq' keeps matching rows."""
    df = pl.DataFrame({"status": ["active", "inactive", "active"]})
    result = _apply_step_filter(df, {"field": "status", "op": "eq", "value": "active"})
    assert result.height == 2


def test_apply_filter_neq():
    """Filter op 'neq' removes matching rows."""
    df = pl.DataFrame({"status": ["active", "inactive", "active"]})
    result = _apply_step_filter(df, {"field": "status", "op": "neq", "value": "inactive"})
    assert result.height == 2


def test_apply_filter_contains():
    """Filter op 'contains' matches substrings."""
    df = pl.DataFrame({"name": ["Acme Corp", "Beta LLC", "Acme Holdings"]})
    result = _apply_step_filter(df, {"field": "name", "op": "contains", "value": "Acme"})
    assert result.height == 2


def test_apply_filter_is_not_null_with_and_join():
    """is_not_null works with default AND join alongside other filters."""
    df = pl.DataFrame({
        "name": ["Acme", None, "Beta", None, "Gamma"],
        "status": ["active", "active", "inactive", "active", "active"],
    })
    from recipe import build_filter_expr
    expr = build_filter_expr([
        {"field": "name", "op": "is_not_null"},
        {"field": "status", "op": "eq", "value": "active"},
    ])
    result = df.filter(expr)
    assert result.height == 2
    assert result["name"].to_list() == ["Acme", "Gamma"]


def test_apply_filter_is_not_null():
    """Filter op 'is_not_null' keeps rows where field is not null."""
    df = pl.DataFrame({"vendor_id": ["V1", None, "V3", None, "V5"]})
    result = _apply_step_filter(df, {"field": "vendor_id", "op": "is_not_null"})
    assert result.height == 3
    assert result["vendor_id"].to_list() == ["V1", "V3", "V5"]


def test_apply_filter_is_null():
    """Filter op 'is_null' keeps rows where field is null."""
    df = pl.DataFrame({"vendor_id": ["V1", None, "V3", None, "V5"]})
    result = _apply_step_filter(df, {"field": "vendor_id", "op": "is_null"})
    assert result.height == 2
    assert result["vendor_id"].to_list() == [None, None]


def test_apply_filter_max_age_years():
    """Filter op 'max_age_years' delegates to apply_date_gate."""
    df = pl.DataFrame({"Updated": ["01/01/2026", "01/01/2020", "06/01/2025"]})
    result = _apply_step_filter(df, {"field": "Updated", "op": "max_age_years", "value": 2})
    # Only recent dates should survive
    assert result.height >= 1
    assert result.height < 3  # the 2020 record should be filtered


# ---------------------------------------------------------------------------
# Test: applies_to in step pipeline
# ---------------------------------------------------------------------------

def test_filters_applies_to_destination(tmp_path):
    """Filter with applies_to=destination only filters the dest DataFrame."""
    source = pl.DataFrame({
        "vendor_id": ["V7001"],
        "l3_fmly_nm": ["Acme Corp"],
        "hq_addr1": [""], "hq_addr2": [""],
    })
    dest = pl.DataFrame({
        "Vendor Name": ["Acme Corp", "Acme Corp"],
        "Supplier Name": ["Acme Holdings", "Acme Old"],
        "Supplier ID": ["S001", "S002"],
        "Address1": ["", ""], "Address2": ["", ""],
        "status": ["active", "inactive"],
    })

    step = {
        "name": "Test",
        "source": "pop",
        "destination": "dst",
        "match_fields": [{"source": "l3_fmly_nm", "destination": "Vendor Name",
                          "method": "exact", "tiers": ["raw"]}],
        "filters": [
            {"field": "status", "op": "eq", "value": "active", "applies_to": "destination"},
        ],
        "inherit": [
            {"source": "Supplier Name", "as": "derived_l1_name"},
            {"source": "Supplier ID", "as": "derived_l1_id"},
        ],
    }

    matched = run_matching_step(source, dest, step, dedup_field="vendor_id")
    assert matched.height == 1
    assert matched["derived_l1_name"][0] == "Acme Holdings"


def test_filters_applies_to_source(tmp_path):
    """Filter with applies_to=source only filters the source DataFrame."""
    source = pl.DataFrame({
        "vendor_id": ["V7001", "V7002"],
        "l3_fmly_nm": ["Acme Corp", "Acme Corp"],
        "status": ["active", "inactive"],
        "hq_addr1": ["", ""], "hq_addr2": ["", ""],
    })
    dest = pl.DataFrame({
        "Vendor Name": ["Acme Corp"],
        "Supplier Name": ["Acme Holdings"],
        "Supplier ID": ["S001"],
        "Address1": [""], "Address2": [""],
    })

    step = {
        "name": "Test",
        "source": "pop",
        "destination": "dst",
        "match_fields": [{"source": "l3_fmly_nm", "destination": "Vendor Name",
                          "method": "exact", "tiers": ["raw"]}],
        "filters": [
            {"field": "status", "op": "eq", "value": "active", "applies_to": "source"},
        ],
        "inherit": [
            {"source": "Supplier Name", "as": "derived_l1_name"},
            {"source": "Supplier ID", "as": "derived_l1_id"},
        ],
    }

    matched = run_matching_step(source, dest, step, dedup_field="vendor_id")
    assert matched.height == 1
    assert matched["vendor_id"][0] == "V7001"


# ---------------------------------------------------------------------------
# Test: backward compat — date_gate still works in full pipeline
# ---------------------------------------------------------------------------

def test_pipeline_date_gate_backward_compat(tmp_path):
    """Pipeline with date_gate (no filters key) should still work."""
    source = pl.DataFrame({
        "vendor_id": ["V7001", "V7002"],
        "l3_fmly_nm": ["Acme Corp", "Beta LLC"],
        "hq_addr1": ["", ""], "hq_addr2": ["", ""],
    })
    dest = pl.DataFrame({
        "Vendor Name": ["Acme Corp", "Beta LLC"],
        "Supplier Name": ["Acme Holdings", "Beta Parent"],
        "Supplier ID": ["S001", "S002"],
        "Address1": ["", ""], "Address2": ["", ""],
        "Updated": ["01/01/2026", "01/01/2018"],  # Beta is too old
    })

    source.write_csv(str(tmp_path / "src.csv"))
    dest.write_csv(str(tmp_path / "dst.csv"))

    recipe = {
        "name": "Test Date Gate Compat",
        "sources": {
            "dst": {"file": "dst.csv", "type": "trusted_reference"},
            "src": {"file": "src.csv", "type": "multi_population"},
        },
        "populations": {
            "pop": {"source": "src", "record_key": "vendor_id",
                    "filter": [{"field": "vendor_id", "op": "starts_with", "value": "V7"}]},
        },
        "steps": [{
            "name": "Match",
            "source": "pop",
            "destination": "dst",
            "match_fields": [{"source": "l3_fmly_nm", "destination": "Vendor Name",
                              "method": "exact", "tiers": ["raw"]}],
            "date_gate": {"field": "Updated", "max_age_years": 2, "applies_to": "destination"},
            "inherit": [{"source": "Supplier Name", "as": "derived_l1_name"},
                        {"source": "Supplier ID", "as": "derived_l1_id"}],
        }],
        "output": {"format": "xlsx", "match_mode": "best_match"},
    }

    result = run_pipeline(recipe, base_dir=str(tmp_path))
    # Acme matches (2026 is recent), Beta filtered out (2018 too old)
    assert result["stats"]["matched_count"] == 1
    assert result["stats"]["unmatched_count"] == 1
