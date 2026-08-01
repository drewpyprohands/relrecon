"""
Test: inherit prefers _dst column when both source and dest have the same field.

When Pop1 and Pop3 both have a column (e.g., engmtn_id), after join:
- engmtn_id = Pop1's (source side)
- engmtn_id_dst = Pop3's (destination side)

Inherit should rename the _dst version, leaving the source column intact.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl

from matching import run_matching_step


def test_inherit_prefers_dst_when_both_exist():
    """Inherit renames dest column, not source, when both sides have the same name."""
    source = pl.DataFrame({
        "vendor_id": ["V7001"],
        "l3_fmly_nm": ["Acme Corp"],
        "engmtn_id": ["SRC_001"],
        "hq_addr1": [""], "hq_addr2": [""],
    })
    dest = pl.DataFrame({
        "l3_fmly_nm": ["Acme Corp"],
        "engmtn_id": ["DST_999"],
        "l1_fmly_nm": ["Acme Parent"],
        "tpty_l1_id": ["L1_001"],
    })

    step = {
        "name": "Match Pop1 to Pop3",
        "source": "pop1",
        "destination": "pop3",
        "match_fields": [{"source": "l3_fmly_nm", "destination": "l3_fmly_nm",
                          "method": "exact", "tiers": ["raw"]}],
        "inherit": [
            {"source": "engmtn_id", "as": "dest_engmtn_id"},
        ],
    }

    matched = run_matching_step(source, dest, step, dedup_field="vendor_id")

    assert matched.height == 1

    # Source engmtn_id should still exist with Pop1's value
    assert "engmtn_id" in matched.columns, f"Source engmtn_id missing. Columns: {matched.columns}"
    assert matched["engmtn_id"][0] == "SRC_001"

    # Inherited column should have Pop3's (destination) value
    assert "dest_engmtn_id" in matched.columns, f"dest_engmtn_id missing. Columns: {matched.columns}"
    assert matched["dest_engmtn_id"][0] == "DST_999"
