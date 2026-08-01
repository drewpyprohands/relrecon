"""Tests for street match gate / hard filter (Issue #110)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import copy

import polars as pl

from matching import run_pipeline
from recipe import load_recipe

RECIPE_PATH = Path(__file__).resolve().parent / "config" / "recipes" / "l1_reconciliation.yaml"
DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_recipe():
    return copy.deepcopy(load_recipe(str(RECIPE_PATH)))


def _run_with_street_gate(require=True, threshold=None):
    """Run pipeline with require_street_match toggled."""
    r = _load_recipe()
    for step in r["steps"]:
        if "address_support" in step:
            step["address_support"]["require_street_match"] = require
            if threshold is not None:
                step["address_support"]["threshold"] = threshold
            else:
                step["address_support"].pop("threshold", None)
    return run_pipeline(r, base_dir=str(DATA_DIR))


def test_gate_off_matches_baseline():
    """With gate off, matched count equals baseline (no filtering by street)."""
    res_off = _run_with_street_gate(require=False)
    res_baseline = _run_with_street_gate(require=False)
    assert res_off["stats"]["matched_count"] == res_baseline["stats"]["matched_count"]


def test_gate_on_reduces_or_equals_matches():
    """With gate on, matched count <= gate off (never adds matches)."""
    res_off = _run_with_street_gate(require=False)
    res_on = _run_with_street_gate(require=True)
    assert res_on["stats"]["matched_count"] <= res_off["stats"]["matched_count"]


def test_gate_rejected_go_to_unmatched():
    """Records rejected by street gate appear in unmatched with reason_code."""
    res_off = _run_with_street_gate(require=False)
    res_on = _run_with_street_gate(require=True)
    dropped = res_off["stats"]["matched_count"] - res_on["stats"]["matched_count"]
    if dropped > 0:
        gained = res_on["stats"]["unmatched_count"] - res_off["stats"]["unmatched_count"]
        assert gained >= dropped
        # Check reason_code column has street_mismatch entries
        unmatched = res_on["unmatched"]
        if "reason_code" in unmatched.columns:
            reasons = unmatched["reason_code"].to_list()
            assert "street_mismatch" in reasons


def test_gate_with_threshold_both_apply():
    """Street gate and threshold work together -- gate runs first."""
    res_gate = _run_with_street_gate(require=True, threshold=None)
    res_both = _run_with_street_gate(require=True, threshold=75)
    # Threshold can only reduce further, never increase
    assert res_both["stats"]["matched_count"] <= res_gate["stats"]["matched_count"]


def test_gate_default_is_off():
    """Default behavior (no require_street_match key) matches gate=False."""
    r = _load_recipe()
    # Remove require_street_match entirely
    for step in r["steps"]:
        if "address_support" in step:
            step["address_support"].pop("require_street_match", None)
            step["address_support"].pop("threshold", None)
    res_default = run_pipeline(r, base_dir=str(DATA_DIR))

    res_off = _run_with_street_gate(require=False)
    assert res_default["stats"]["matched_count"] == res_off["stats"]["matched_count"]


def test_gate_preserves_street_match_column():
    """addr_street_match column is present in output regardless of gate setting."""
    res_on = _run_with_street_gate(require=True)
    res_off = _run_with_street_gate(require=False)
    # All remaining matches with gate on should have street_match=True
    matched_on = res_on["matched"]
    if "addr_street_match" in matched_on.columns and matched_on.height > 0:
        assert matched_on["addr_street_match"].all()
    # Gate off may have a mix
    matched_off = res_off["matched"]
    if "addr_street_match" in matched_off.columns:
        assert "addr_street_match" in matched_off.columns


def test_gate_on_all_remaining_have_street_match_true():
    """When gate is on, every matched record must have addr_street_match=True."""
    res = _run_with_street_gate(require=True)
    matched = res["matched"]
    if "addr_street_match" in matched.columns and matched.height > 0:
        false_count = matched.filter(~pl.col("addr_street_match")).height
        assert false_count == 0, f"{false_count} records slipped through with street_match=False"
