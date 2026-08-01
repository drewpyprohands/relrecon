"""Tests for address threshold enforcement"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import copy
import polars as pl
from matching import run_pipeline
from recipe import load_recipe

RECIPE_PATH = Path(__file__).resolve().parent / "config" / "recipes" / "l1_reconciliation.yaml"
DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_with_threshold(threshold=None):
    r = copy.deepcopy(load_recipe(str(RECIPE_PATH)))
    for step in r["steps"]:
        if "address_support" in step:
            if threshold is not None:
                step["address_support"]["threshold"] = threshold
            else:
                step["address_support"].pop("threshold", None)
    return run_pipeline(r, base_dir=str(DATA_DIR))


def test_no_threshold_keeps_all():
    """Without threshold, all matches kept regardless of score.

    Count is 33 after fix (dedup on vendor_id preserves records
    with duplicate l3_fmly_nm but different vendor_ids) + alias test data.
    """
    res = _load_with_threshold(threshold=None)
    assert res["stats"]["matched_count"] == 33


def test_low_threshold_keeps_all():
    """Low threshold keeps everything."""
    res = _load_with_threshold(threshold=10)
    assert res["stats"]["matched_count"] == 33


def test_high_threshold_filters():
    """High threshold drops low-scoring matches."""
    res_low = _load_with_threshold(threshold=60)
    res_high = _load_with_threshold(threshold=95)
    assert res_high["stats"]["matched_count"] < res_low["stats"]["matched_count"]


def test_filtered_records_go_to_unmatched():
    """Records filtered by address threshold appear in unmatched."""
    res_off = _load_with_threshold(threshold=None)
    res_on = _load_with_threshold(threshold=100)
    dropped = res_off["stats"]["matched_count"] - res_on["stats"]["matched_count"]
    gained = res_on["stats"]["unmatched_count"] - res_off["stats"]["unmatched_count"]
    assert gained >= dropped
