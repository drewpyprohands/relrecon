"""
Tests for Issue dedup uses vendor_id instead of match field.

Verifies that records with the same l3_fmly_nm but different vendor_ids
are preserved through exact matching, fuzzy matching, and the full pipeline.
matched + unmatched must equal total_source.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl
import pytest

from matching import match_names_exact, match_names_fuzzy, run_pipeline
from recipe import RecipeValidationError

# ---------------------------------------------------------------------------
# Fixtures: two source records with same name, different vendor_ids
# ---------------------------------------------------------------------------

def _source_df():
    """Two Pop1 records: same l3_fmly_nm, different vendor_ids."""
    return pl.DataFrame({
        "vendor_id": ["V7001", "V7002"],
        "l3_fmly_nm": ["Acme Corp", "Acme Corp"],
        "hq_addr1": ["100 Main St", "200 Oak Ave"],
        "hq_addr2": ["Suite 1", "Floor 3"],
    })


def _dest_df():
    """Destination with a matching name."""
    return pl.DataFrame({
        "Vendor Name": ["Acme Corp"],
        "Supplier Name": ["Acme Holdings"],
        "Supplier ID": ["S001"],
        "Address1": ["100 Main St"],
        "Address2": ["Suite 1"],
        "Updated": ["01/01/2025"],
    })


# ---------------------------------------------------------------------------
# Test: exact match preserves both records
# ---------------------------------------------------------------------------

def test_exact_match_preserves_duplicate_names():
    """Two records with same name should both match when dedup_field=vendor_id."""
    src = _source_df()
    dst = _dest_df()

    matched = match_names_exact(
        src, dst,
        "l3_fmly_nm", "Vendor Name",
        tiers=["raw"],
        dedup_field="vendor_id",
    )

    assert matched.height == 2, (
        f"Expected 2 matched records (both vendor_ids), got {matched.height}"
    )
    assert set(matched["vendor_id"].to_list()) == {"V7001", "V7002"}


def test_exact_match_old_behavior_collapses():
    """Without dedup_field, dedup on match field collapses duplicates (old bug)."""
    src = _source_df()
    dst = _dest_df()

    # Default dedup_field=None falls back to src_field (l3_fmly_nm)
    matched = match_names_exact(
        src, dst,
        "l3_fmly_nm", "Vendor Name",
        tiers=["raw"],
    )

    # Old behavior: collapses to 1. This test documents the default behavior.
    assert matched.height == 1


# ---------------------------------------------------------------------------
# Test: fuzzy match preserves both records
# ---------------------------------------------------------------------------

def test_fuzzy_match_preserves_duplicate_names():
    """Fuzzy matching with dedup_field=vendor_id keeps both records."""
    src = _source_df()
    dst = pl.DataFrame({
        "Vendor Name": ["Acme Corporation"],  # slight difference for fuzzy
        "Supplier Name": ["Acme Holdings"],
        "Supplier ID": ["S001"],
        "Address1": ["100 Main St"],
        "Address2": ["Suite 1"],
        "Updated": ["01/01/2025"],
    })

    matched = match_names_fuzzy(
        src, dst,
        "l3_fmly_nm", "Vendor Name",
        tiers=["raw"],
        threshold=70,
        scorer="token_sort_ratio",
        dedup_field="vendor_id",
    )

    assert matched.height == 2, (
        f"Expected 2 fuzzy matched records, got {matched.height}"
    )
    assert set(matched["vendor_id"].to_list()) == {"V7001", "V7002"}


# ---------------------------------------------------------------------------
# Test: pipeline invariant — matched + unmatched == total_source
# ---------------------------------------------------------------------------

def test_pipeline_matched_plus_unmatched_equals_total(tmp_path):
    """The core invariant: no records should be lost in the pipeline."""
    # Create source data with duplicate names
    source_data = pl.DataFrame({
        "vendor_id": ["V7001", "V7002", "V7003", "V7004"],
        "l3_fmly_nm": ["Acme Corp", "Acme Corp", "Beta LLC", "Gamma Inc"],
        "hq_addr1": ["100 Main St", "200 Oak Ave", "300 Elm St", "400 Pine Rd"],
        "hq_addr2": ["Suite 1", "Floor 3", "", ""],
        "l1_fmly_nm": ["bad", "bad", "bad", "bad"],
        "tpty_l1_id": ["bad", "bad", "bad", "bad"],
        "cntrct_cmpl_dt": ["01/01/2020", "01/01/2020", "01/01/2020", "01/01/2020"],
        "data_entry_type": ["Migrated", "Migrated", "Migrated", "Migrated"],
        "rq_intk_user": ["Data Migration", "Data Migration", "Data Migration", "Data Migration"],
        "tpty_assm_nm": ["", "", "", ""],
    })

    dest_data = pl.DataFrame({
        "Vendor Name": ["Acme Corp", "Beta LLC"],
        "Supplier Name": ["Acme Holdings", "Beta Parent"],
        "Supplier ID": ["S001", "S002"],
        "Address1": ["100 Main St", "300 Elm St"],
        "Address2": ["Suite 1", ""],
        "Updated": ["01/01/2025", "01/01/2025"],
    })

    # Write CSVs
    source_data.write_csv(str(tmp_path / "tp_multi_pop_dataset.csv"))
    dest_data.write_csv(str(tmp_path / "core_parent_export.csv"))

    # Minimal recipe
    recipe = {
        "name": "Test Dedup",
        "sources": {
            "core_parent": {"file": "core_parent_export.csv", "type": "trusted_reference"},
            "multi_pop": {"file": "tp_multi_pop_dataset.csv", "type": "multi_population"},
        },
        "populations": {
            "pop1": {
                "source": "multi_pop",
                "record_key": "vendor_id",
                "filter": [{"field": "vendor_id", "op": "starts_with", "value": "V7"}],
            },
        },
        "steps": [
            {
                "name": "Match to core_parent",
                "source": "pop1",
                "destination": "core_parent",
                "match_fields": [
                    {
                        "source": "l3_fmly_nm",
                        "destination": "Vendor Name",
                        "method": "exact",
                        "tiers": ["raw"],
                    }
                ],
                "inherit": [
                    {"source": "Supplier Name", "as": "derived_l1_name"},
                    {"source": "Supplier ID", "as": "derived_l1_id"},
                ],
            }
        ],
        "output": {"format": "xlsx", "match_mode": "best_match"},
    }

    result = run_pipeline(recipe, base_dir=str(tmp_path))

    total = result["stats"]["total_source"]
    matched = result["stats"]["matched_count"]
    unmatched = result["stats"]["unmatched_count"]

    assert total == 4, f"Expected 4 total source records, got {total}"
    assert matched + unmatched == total, (
        f"Invariant violated: matched ({matched}) + unmatched ({unmatched}) "
        f"!= total ({total})"
    )
    # Both Acme Corp records should match (V7001 + V7002), plus Beta LLC (V7003)
    assert matched == 3, f"Expected 3 matched, got {matched}"
    assert unmatched == 1, f"Expected 1 unmatched (Gamma Inc), got {unmatched}"


# ---------------------------------------------------------------------------
# Test: invalid record_key raises error
# ---------------------------------------------------------------------------

def test_pipeline_invalid_record_key_raises(tmp_path):
    """A record_key that doesn't exist in the source data should fail."""
    source_data = pl.DataFrame({
        "vendor_id": ["V7001"],
        "l3_fmly_nm": ["Acme Corp"],
        "hq_addr1": [""], "hq_addr2": [""],
    })
    dest_data = pl.DataFrame({
        "Vendor Name": ["Acme Corp"],
        "Supplier Name": ["Acme Holdings"],
        "Supplier ID": ["S001"],
        "Address1": [""], "Address2": [""],
        "Updated": ["01/01/2025"],
    })
    source_data.write_csv(str(tmp_path / "src.csv"))
    dest_data.write_csv(str(tmp_path / "dst.csv"))

    recipe = {
        "name": "Test Bad Key",
        "sources": {
            "dst": {"file": "dst.csv", "type": "trusted_reference"},
            "src": {"file": "src.csv", "type": "multi_population"},
        },
        "populations": {
            "pop": {"source": "src", "record_key": "nonexistent_field", "filter": [{"field": "vendor_id", "op": "starts_with", "value": "V7"}]},
        },
        "steps": [{
            "name": "Match",
            "source": "pop",
            "destination": "dst",
            "match_fields": [{"source": "l3_fmly_nm", "destination": "Vendor Name", "method": "exact", "tiers": ["raw"]}],
            "inherit": [{"source": "Supplier Name", "as": "derived_l1_name"}, {"source": "Supplier ID", "as": "derived_l1_id"}],
        }],
        "output": {"format": "xlsx", "match_mode": "best_match"},
    }

    with pytest.raises(RecipeValidationError, match="record_key"):
        run_pipeline(recipe, base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# Test: missing record_key warns but still works (legacy fallback)
# ---------------------------------------------------------------------------

def test_pipeline_missing_record_key_falls_back(tmp_path):
    """Without record_key, pipeline falls back to match field (with warning)."""
    source_data = pl.DataFrame({
        "vendor_id": ["V7001", "V7002"],
        "l3_fmly_nm": ["Acme Corp", "Beta LLC"],
        "hq_addr1": ["", ""], "hq_addr2": ["", ""],
    })
    dest_data = pl.DataFrame({
        "Vendor Name": ["Acme Corp", "Beta LLC"],
        "Supplier Name": ["Acme Holdings", "Beta Parent"],
        "Supplier ID": ["S001", "S002"],
        "Address1": ["", ""], "Address2": ["", ""],
        "Updated": ["01/01/2025", "01/01/2025"],
    })
    source_data.write_csv(str(tmp_path / "src.csv"))
    dest_data.write_csv(str(tmp_path / "dst.csv"))

    recipe = {
        "name": "Test No Key",
        "sources": {
            "dst": {"file": "dst.csv", "type": "trusted_reference"},
            "src": {"file": "src.csv", "type": "multi_population"},
        },
        "populations": {
            "pop": {"source": "src", "filter": [{"field": "vendor_id", "op": "starts_with", "value": "V7"}]},
        },
        "steps": [{
            "name": "Match",
            "source": "pop",
            "destination": "dst",
            "match_fields": [{"source": "l3_fmly_nm", "destination": "Vendor Name", "method": "exact", "tiers": ["raw"]}],
            "inherit": [{"source": "Supplier Name", "as": "derived_l1_name"}, {"source": "Supplier ID", "as": "derived_l1_id"}],
        }],
        # No record_key
        "output": {"format": "xlsx", "match_mode": "best_match"},
    }

    # Should work without error (legacy fallback)
    result = run_pipeline(recipe, base_dir=str(tmp_path))
    assert result["stats"]["matched_count"] == 2
