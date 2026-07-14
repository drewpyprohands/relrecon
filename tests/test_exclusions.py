"""Tests for the global exclusions file (Issue #65)."""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from matching import run_pipeline
from recipe import apply_exclusions, load_exclusions
from summary import generate_summary


def _write_data(tmp_path):
    pl.DataFrame({
        "vendor_id": ["V7001", "V7002", "V7003"],
        "name": ["Acme Corp", "Beta LLC", "Gamma Inc"],
    }).write_csv(str(tmp_path / "src.csv"))
    # destA can match all three; destB can only match Acme
    pl.DataFrame({
        "name": ["Acme Corp", "Beta LLC", "Gamma Inc"],
        "pid": ["A1", "B1", "G1"],
    }).write_csv(str(tmp_path / "destA.csv"))
    pl.DataFrame({
        "name": ["Acme Corp"], "pid": ["A2"],
    }).write_csv(str(tmp_path / "destB.csv"))


def _recipe(exclusions=None, inline_exclude=None):
    step_a = {
        "name": "MatchA", "source": "pop", "destination": "destA",
        "match_fields": [{"source": "name", "destination": "name",
                          "method": "exact", "tiers": ["raw"]}],
        "inherit": [{"source": "pid", "as": "derived_id"}],
    }
    if inline_exclude is not None:
        step_a["exclude"] = {"values": inline_exclude}
    recipe = {
        "name": "Exclusion Test",
        "sources": {
            "destA": {"file": "destA.csv"},
            "destB": {"file": "destB.csv"},
            "src": {"file": "src.csv"},
        },
        "populations": {"pop": {"source": "src", "record_key": "vendor_id"}},
        "steps": [
            step_a,
            {"name": "MatchB", "source": "pop", "destination": "destB",
             "match_fields": [{"source": "name", "destination": "name",
                               "method": "exact", "tiers": ["raw"]}],
             "inherit": [{"source": "pid", "as": "derived_id"}]},
        ],
        "output": {"format": "xlsx", "match_mode": "best_match"},
    }
    if exclusions is not None:
        recipe["exclusions"] = exclusions
    return recipe


# ---------------------------------------------------------------------------
# load_exclusions / apply_exclusions
# ---------------------------------------------------------------------------

def test_load_exclusions_parses_rows(tmp_path):
    (tmp_path / "excl.csv").write_text(
        "step,vnd_id,note\n"
        "MatchA,V7001,manual review\n"
        "MatchA,V7002,\n"
        "MatchB,V7003,dupe\n"
        "\n"  # blank row skipped
        ",V7004,no step\n"  # missing step skipped
    )
    got = load_exclusions("excl.csv", base_dir=str(tmp_path))
    assert got == {"MatchA": ["V7001", "V7002"], "MatchB": ["V7003"]}


def test_load_exclusions_missing_step_column_raises(tmp_path):
    (tmp_path / "bad.csv").write_text("foo,vnd_id\nx,y\n")
    with pytest.raises(ValueError, match="step"):
        load_exclusions("bad.csv", base_dir=str(tmp_path))


def test_load_exclusions_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_exclusions("nope.csv", base_dir=str(tmp_path))


def test_load_exclusions_handles_utf8_bom(tmp_path):
    """Excel 'CSV UTF-8' exports carry a BOM; header must still parse."""
    (tmp_path / "bom.csv").write_text(
        "step,vnd_id\nMatchA,V7001\n", encoding="utf-8-sig"
    )
    got = load_exclusions("bom.csv", base_dir=str(tmp_path))
    assert got == {"MatchA": ["V7001"]}


def test_load_exclusions_custom_id_column(tmp_path):
    """id_column lets the CSV name its id column anything."""
    (tmp_path / "excl.csv").write_text(
        "step,supplier_id,note\nMatchA,S1,x\nMatchB,S2,\n"
    )
    cfg = {"file": "excl.csv", "id_column": "supplier_id"}
    got = load_exclusions(cfg, base_dir=str(tmp_path))
    assert got == {"MatchA": ["S1"], "MatchB": ["S2"]}


def test_load_exclusions_autodetects_sole_id_column(tmp_path):
    """With no vnd_id and no config, the single non-note column is used."""
    (tmp_path / "excl.csv").write_text("step,supplier_id,note\nMatchA,S1,x\n")
    got = load_exclusions("excl.csv", base_dir=str(tmp_path))
    assert got == {"MatchA": ["S1"]}


def test_load_exclusions_ambiguous_id_column_raises(tmp_path):
    (tmp_path / "excl.csv").write_text("step,supplier_id,region\nMatchA,S1,EU\n")
    with pytest.raises(ValueError, match="id_column"):
        load_exclusions("excl.csv", base_dir=str(tmp_path))


def test_load_exclusions_bad_id_column_raises(tmp_path):
    (tmp_path / "excl.csv").write_text("step,supplier_id\nMatchA,S1\n")
    with pytest.raises(ValueError, match="no 'nope' column"):
        load_exclusions({"file": "excl.csv", "id_column": "nope"},
                        base_dir=str(tmp_path))


def test_apply_exclusions_merges_with_inline():
    recipe = _recipe(inline_exclude=["V7009"])
    unknown = apply_exclusions(recipe, {"MatchA": ["V7001"]})
    assert unknown == []
    assert recipe["steps"][0]["exclude"]["values"] == ["V7009", "V7001"]


def test_apply_exclusions_reports_unknown_step():
    recipe = _recipe()
    unknown = apply_exclusions(recipe, {"NoSuchStep": ["V7001"]})
    assert unknown == ["NoSuchStep"]


# ---------------------------------------------------------------------------
# Pipeline behavior
# ---------------------------------------------------------------------------

def test_exclusion_applied_at_named_step_only_and_cascades(tmp_path):
    _write_data(tmp_path)
    (tmp_path / "excl.csv").write_text("step,vnd_id\nMatchA,V7001\n")

    result = run_pipeline(_recipe(exclusions="excl.csv"), base_dir=str(tmp_path))
    matched = result["matched"].sort("vendor_id")
    by_id = dict(zip(matched["vendor_id"].to_list(),
                     matched["match_step"].to_list(), strict=True))

    # V7001 excluded from MatchA, cascades to MatchB
    assert by_id["V7001"] == "MatchB"
    # Others unaffected -- matched at MatchA
    assert by_id["V7002"] == "MatchA"
    assert by_id["V7003"] == "MatchA"
    assert matched.height == 3


def test_pipeline_custom_id_column(tmp_path):
    """Object form + custom id column routes correctly against record_key."""
    _write_data(tmp_path)
    (tmp_path / "excl.csv").write_text("step,supplier_ref\nMatchA,V7001\n")
    recipe = _recipe(exclusions={"file": "excl.csv", "id_column": "supplier_ref"})

    result = run_pipeline(recipe, base_dir=str(tmp_path))
    by_id = dict(zip(result["matched"]["vendor_id"].to_list(),
                     result["matched"]["match_step"].to_list(), strict=True))
    assert by_id["V7001"] == "MatchB"
    assert result["stats"]["exclusions"]["MatchA"]["count"] == 1


def test_summary_counts_correct(tmp_path):
    _write_data(tmp_path)
    (tmp_path / "excl.csv").write_text("step,vnd_id\nMatchA,V7001\n")

    result = run_pipeline(_recipe(exclusions="excl.csv"), base_dir=str(tmp_path))
    excl = result["stats"]["exclusions"]
    assert excl == {"MatchA": {"count": 1, "values": ["V7001"], "field": "vendor_id"}}

    md = generate_summary(_recipe(exclusions="excl.csv"), result["stats"],
                          result["matched"], mermaid="disabled")
    assert "Exclusions (from exclusions file)" in md
    assert "| MatchA | 1 | V7001 |" in md


def test_global_matches_inline_exclude(tmp_path):
    """Global file yields identical results to hand-editing per-step exclude."""
    _write_data(tmp_path)
    (tmp_path / "excl.csv").write_text("step,vnd_id\nMatchA,V7001\n")

    global_res = run_pipeline(_recipe(exclusions="excl.csv"), base_dir=str(tmp_path))
    inline_res = run_pipeline(_recipe(inline_exclude=["V7001"]), base_dir=str(tmp_path))

    cols = ["vendor_id", "match_step", "derived_id"]
    assert (global_res["matched"].sort("vendor_id").select(cols).to_dicts()
            == inline_res["matched"].sort("vendor_id").select(cols).to_dicts())


def test_existing_per_step_exclude_still_works(tmp_path):
    """A recipe with only inline exclude (no exclusions file) is unchanged."""
    _write_data(tmp_path)
    result = run_pipeline(_recipe(inline_exclude=["V7001"]), base_dir=str(tmp_path))
    by_id = dict(zip(result["matched"]["vendor_id"].to_list(),
                     result["matched"]["match_step"].to_list(), strict=True))
    assert by_id["V7001"] == "MatchB"


def test_no_exclusions_file_is_noop(tmp_path):
    _write_data(tmp_path)
    result = run_pipeline(_recipe(), base_dir=str(tmp_path))
    assert result["stats"]["exclusions"] == {}
    assert result["matched"].height == 3
