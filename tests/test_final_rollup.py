"""Tests for the terminal final_rollup pass (Issue #67).

final_rollup is a single-phase, non-destructive terminal aggregation: per
bucket it rolls a group to the tie-broken min of a target column across a set
of steps, writing an additive column plus a <write_to>_changed audit flag.
Distinct from per-step tie_breaker (resolves ties within one step).
"""

import copy
import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from matching import run_pipeline  # noqa: E402
from recipe import (  # noqa: E402
    RecipeValidationError,
    load_recipe,
    validate_recipe,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RECIPE_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "recipes")


def _load(name):
    return load_recipe(os.path.join(RECIPE_DIR, f"{name}.yaml"))


def _run(recipe):
    return run_pipeline(recipe, base_dir=DATA_DIR)


def _by_id(matched, key="vendor_id"):
    """Return {id: row-dict} for easy per-record assertions."""
    return {r[key]: r for r in matched.iter_rows(named=True)}


# ---------------------------------------------------------------------------
# Cross-step / dest-identity bucket (group_key: derived_supplier_nm, all steps)
# ---------------------------------------------------------------------------

class TestDestIdentityBucket:
    def test_cross_step_family_rolls_to_lowest(self):
        """Members matched via different steps get the family min."""
        rows = _by_id(_run(_load("crossstep_rollup"))["matched"])
        # Company XYZ Holdings: V001 via L3 (S1013), V002/V003 via L1 (S0500)
        for vid in ("V001", "V002", "V003"):
            assert rows[vid]["rolled_supplier_id"] == "S0500"

    def test_orphan_reaches_family_min(self):
        """Zeta -> S0100 including the orphan V012 (matches no supplier_nm)."""
        rows = _by_id(_run(_load("crossstep_rollup"))["matched"])
        for vid in ("V010", "V011", "V012"):
            assert rows[vid]["rolled_supplier_id"] == "S0100"

    def test_rollup_changed_flag(self):
        """changed true iff write_to differs from the row's own target."""
        rows = _by_id(_run(_load("crossstep_rollup"))["matched"])
        changed = {v for v, r in rows.items() if r["rolled_supplier_id_changed"]}
        # Only rows whose rolled value differs from their own derived id.
        assert changed == {"V001", "V010", "V012"}
        for r in rows.values():
            differs = r["rolled_supplier_id"] != r["derived_supplier_id"]
            assert r["rolled_supplier_id_changed"] == differs


# ---------------------------------------------------------------------------
# Tier-name bucket (group_key: derived_dest_name, L3 steps only)
# ---------------------------------------------------------------------------

class TestTierNameBucket:
    def test_groups_within_l3_name_only(self):
        """{V010,V012} -> S0200 (Zeta East); V011 keeps S0100 (Zeta West)."""
        rows = _by_id(_run(_load("crossstep_rollup"))["matched"])
        assert rows["V010"]["rolled_supplier_id_l3"] == "S0200"
        assert rows["V012"]["rolled_supplier_id_l3"] == "S0200"
        assert rows["V011"]["rolled_supplier_id_l3"] == "S0100"

    def test_l1_rows_unaffected(self):
        """Bucket isolation: L1-matched rows keep their own target."""
        rows = _by_id(_run(_load("crossstep_rollup"))["matched"])
        for vid in ("V002", "V003", "V006", "V030", "V031"):
            assert rows[vid]["match_step"].endswith("L1")
            assert rows[vid]["rolled_supplier_id_l3"] == rows[vid]["derived_supplier_id"]
            assert rows[vid]["rolled_supplier_id_l3_changed"] is False

    def test_l3_bucket_never_sources_from_l1(self):
        """Every L3 rolled value equals the min among L3 rows in that group."""
        matched = _run(_load("crossstep_rollup"))["matched"]
        l3 = matched.filter(pl.col("match_step").str.ends_with("L3"))
        # Group L3 rows by dest name; each member's l3 rollup must equal the
        # min derived id within that L3-only group.
        for (_name,), grp in l3.group_by(["derived_dest_name"]):
            ids = sorted(grp["derived_supplier_id"].to_list())
            expected = ids[0]  # asc, alpha-stripped equals lexical here (S0xxx)
            for rolled in grp["rolled_supplier_id_l3"].to_list():
                assert rolled == expected


# ---------------------------------------------------------------------------
# Anchor: the group's minimum-holder retains its OWN id
# ---------------------------------------------------------------------------

class TestAnchor:
    def test_minimum_holder_retains_own_id(self):
        rows = _by_id(_run(_load("anchor_rollup"))["matched"])
        # G1: A=S300, B=S100, C=S200 -> all S100; B holds the min, keeps it.
        assert rows["A"]["rolled_supplier_id"] == "S100"
        assert rows["B"]["rolled_supplier_id"] == "S100"
        assert rows["C"]["rolled_supplier_id"] == "S100"
        assert rows["B"]["rolled_supplier_id_changed"] is False
        assert rows["A"]["rolled_supplier_id_changed"] is True
        # G2 is a singleton.
        assert rows["D"]["rolled_supplier_id"] == "S500"
        assert rows["D"]["rolled_supplier_id_changed"] is False


# ---------------------------------------------------------------------------
# group_key_tier
# ---------------------------------------------------------------------------

class TestGroupKeyTier:
    def test_raw_keeps_variants_separate(self):
        """raw: 'Company Xyz. Holdings' does not group with 'Company XYZ Holdings'."""
        r = _load("crossstep_rollup")
        r["output"]["final_rollup"][0]["group_key_tier"] = "raw"
        rows = _by_id(_run(r)["matched"])
        assert rows["V006"]["rolled_supplier_id"] == "S0300"
        assert rows["V001"]["rolled_supplier_id"] == "S0500"

    def test_clean_collapses_variants(self):
        """clean: the punctuation/case variant groups with the base name."""
        r = _load("crossstep_rollup")
        r["output"]["final_rollup"][0]["group_key_tier"] = "clean"
        rows = _by_id(_run(r)["matched"])
        for vid in ("V001", "V002", "V003", "V006"):
            assert rows[vid]["rolled_supplier_id"] == "S0300"


# ---------------------------------------------------------------------------
# Non-destructive / no-op guarantee
# ---------------------------------------------------------------------------

class TestNonDestructive:
    def test_target_column_unchanged(self):
        matched = _run(_load("crossstep_rollup"))["matched"]
        # derived_supplier_id (the target) is never mutated by the rollup.
        rows = _by_id(matched)
        assert rows["V001"]["derived_supplier_id"] == "S1013"
        assert rows["V010"]["derived_supplier_id"] == "S0200"

    def test_no_op_without_final_rollup(self):
        """Removing final_rollup yields no rolled_* columns and same matches."""
        r = _load("crossstep_rollup")
        with_rollup = _run(copy.deepcopy(r))["matched"]
        del r["output"]["final_rollup"]
        # Drop rolled column refs so field validation passes.
        r["output"]["columns"]["matched"] = [
            c for c in r["output"]["columns"]["matched"]
            if not c["field"].startswith("rolled_")
        ]
        without = _run(r)["matched"]
        assert not any(c.startswith("rolled_") for c in without.columns)
        # Shared columns are identical (rollup is purely additive).
        shared = [c for c in without.columns if not c.startswith("rolled_")]
        left = with_rollup.select(shared).sort("vendor_id")
        right = without.select(shared).sort("vendor_id")
        assert left.equals(right)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def _base(self):
        return _load("crossstep_rollup")

    def test_multi_phase_rejected(self):
        r = {
            "name": "mp",
            "sources": {"src": {"file": "test.csv"}},
            "phases": [
                {
                    "name": "P1",
                    "populations": {"p": {"source": "src", "filter": []}},
                    "steps": [
                        {
                            "name": "s1",
                            "source": "p",
                            "destination": "p",
                            "match_fields": [
                                {"source": "n", "destination": "n",
                                 "method": "exact", "tiers": ["raw"]}
                            ],
                        }
                    ],
                    "output": {
                        "format": "csv",
                        "summary": "none",
                        "final_rollup": [
                            {"steps": ["s1"], "group_key": "grp",
                             "target": "supplier_id"}
                        ],
                    },
                }
            ],
        }
        with pytest.raises(ValueError, match="multi-phase"):
            validate_recipe(r)

    def test_unknown_step_name_rejected(self):
        r = self._base()
        r["output"]["final_rollup"][0]["steps"] = ["No Such Step"]
        with pytest.raises(ValueError, match="names no existing step"):
            validate_recipe(r)

    def test_duplicate_write_to_rejected(self):
        r = self._base()
        # Two buckets both default-writing to the same column.
        r["output"]["final_rollup"][1]["write_to"] = "rolled_supplier_id"
        with pytest.raises(ValueError, match="unique write_to"):
            validate_recipe(r)

    def test_missing_group_key_column_rejected(self):
        r = self._base()
        r["output"]["final_rollup"][0]["group_key"] = "nonexistent_col"
        with pytest.raises(RecipeValidationError):
            _run(r)

    def test_missing_target_column_rejected(self):
        r = self._base()
        r["output"]["final_rollup"][0]["target"] = "nonexistent_col"
        with pytest.raises(RecipeValidationError):
            _run(r)

    def test_bad_group_key_tier_enum_rejected(self):
        r = self._base()
        r["output"]["final_rollup"][0]["group_key_tier"] = "bogus"
        with pytest.raises(ValueError):
            validate_recipe(r)
