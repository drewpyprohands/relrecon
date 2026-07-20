"""Tests for output.decision_record + output.compare_columns (Issue #93)."""

import copy
import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matching import run_pipeline
from recipe import (
    RecipeValidationError,
    known_derived_columns,
    load_recipe,
    validate_fields,
    validate_recipe,
)
from report import apply_output_computations

SRC = Path(__file__).parent.parent / "src"
DATA_DIR = Path(__file__).parent.parent / "data"
RECIPES = Path(__file__).parent / "recipes"
EXPECTED = Path(__file__).parent / "expected"

RECIPE = str(RECIPES / "decision_record_test.yaml")
UNLISTED_RECIPE = str(RECIPES / "decision_record_unlisted_test.yaml")
NONNUMERIC_RECIPE = str(RECIPES / "decision_record_nonnumeric_test.yaml")


def _load_main():
    spec = importlib.util.spec_from_file_location("relrecon_main", SRC / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(recipe: dict, tmp_path, out_name="data.csv"):
    """Run the pipeline + _write_output for a recipe dict; return the out dir."""
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    main._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(tmp_path / out_name),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file="decision_record_test.yaml",
        timing=result.get("timing"),
        source_df=result["populations"].get("pop1"),
        source_key="vnd_id",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Byte-exact artifacts
# ---------------------------------------------------------------------------

def test_matched_csv_byte_exact(tmp_path):
    """Coalesce first-hit, whitespace fall-through, all-null row; all 6 compares."""
    _run(load_recipe(RECIPE), tmp_path)
    assert (tmp_path / "data.csv").read_text() == (
        EXPECTED / "decision_record_matched.csv"
    ).read_text()


def test_merged_csv_byte_exact(tmp_path):
    """Unmatched merged rows fall through to the source-side candidate."""
    _run(load_recipe(RECIPE), tmp_path)
    assert (tmp_path / "data_merged.csv").read_text() == (
        EXPECTED / "decision_record_merged.csv"
    ).read_text()


def test_merged_unmatched_row_falls_through(tmp_path):
    """The unmatched block resolves via src_l1_id -- derived_l1_id is absent there."""
    _run(load_recipe(RECIPE), tmp_path)
    rows = (tmp_path / "data_merged.csv").read_text().splitlines()
    unmatched = [r for r in rows if r.endswith(",true")]
    assert unmatched == ["V004,unmatched,S1004,src_l1_id,,,true"]


def test_all_candidates_null_row_is_empty(tmp_path):
    """V003 has no derived_l1_id and no src_l1_id: both columns empty, no sentinel."""
    _run(load_recipe(RECIPE), tmp_path)
    df = pl.read_csv(tmp_path / "data.csv")
    row = df.filter(pl.col("vnd_id") == "V003").to_dicts()[0]
    assert row["final_parent_id"] is None
    assert row["final_parent_src"] is None


def test_whitespace_candidate_falls_through(tmp_path):
    """V002's inherited derived_l1_id is whitespace-only -> treated as null."""
    _run(load_recipe(RECIPE), tmp_path)
    df = pl.read_csv(tmp_path / "data.csv")
    row = df.filter(pl.col("vnd_id") == "V002").to_dicts()[0]
    assert row["final_parent_id"] == "S1002"
    assert row["final_parent_src"] == "src_l1_id"


def test_empty_string_candidate_is_null():
    """A literal empty string (not just a null) is a fall-through, not a value."""
    df = pl.DataFrame({"a": ["", "x"], "b": ["fallback", "fallback"]})
    out = apply_output_computations(df, {
        "decision_record": {"candidates": ["a", "b"]},
        "columns": {"matched": [
            {"field": "final_parent_id", "header": "final_parent_id"},
            {"field": "final_parent_src", "header": "final_parent_src"},
        ]},
    })
    assert out["final_parent_id"].to_list() == ["fallback", "x"]
    assert out["final_parent_src"].to_list() == ["b", "a"]


# ---------------------------------------------------------------------------
# compare_columns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("left,right,expected", [
    (10.0, 5.0, "higher"),
    (5.0, 10.0, "lower"),
    (5.0, 5.0, "same"),
    (None, 5.0, None),
    (5.0, None, None),
    (None, None, None),
])
def test_compare_cases(left, right, expected):
    """higher/lower/same are left-relative; any null operand yields an empty cell."""
    df = pl.DataFrame({"l": [left], "r": [right]}, schema={"l": pl.Float64, "r": pl.Float64})
    out = apply_output_computations(df, {
        "compare_columns": [{"left": "l", "right": "r", "output": "cmp"}],
        "columns": {"matched": [{"field": "cmp", "header": "cmp"}]},
    })
    assert out["cmp"].to_list() == [expected]


def test_nonnumeric_aborts_naming_column_and_value():
    """A present non-numeric value is a config error, never a silent null."""
    df = pl.DataFrame({"l": ["12", "abc"], "r": ["1", "2"]})
    with pytest.raises(RecipeValidationError) as exc:
        apply_output_computations(df, {
            "compare_columns": [{"left": "l", "right": "r", "output": "cmp"}],
            "columns": {"matched": [{"field": "cmp", "header": "cmp"}]},
        })
    assert '"l"' in str(exc.value)
    assert '"abc"' in str(exc.value)


def test_nonnumeric_run_writes_zero_artifacts(tmp_path):
    """The abort happens before any artifact reaches disk."""
    recipe = load_recipe(NONNUMERIC_RECIPE)
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    with pytest.raises(RecipeValidationError):
        main._write_output(
            output_cfg=recipe["output"],
            matched_df=result["matched"],
            unmatched_df=result.get("unmatched"),
            output_path=str(tmp_path / "data.csv"),
            stats=result.get("stats", {}),
            recipe=recipe,
            recipe_file="decision_record_nonnumeric_test.yaml",
            timing=result.get("timing"),
        )
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == []


# ---------------------------------------------------------------------------
# Visibility / registration
# ---------------------------------------------------------------------------

def test_columns_absent_when_not_listed(tmp_path):
    """Configured but unlisted computed columns never reach an artifact."""
    _run(load_recipe(UNLISTED_RECIPE), tmp_path)
    for name in ("data.csv", "data_merged.csv", "data_unmatched.csv"):
        header = (tmp_path / name).read_text().splitlines()[0]
        for col in ("final_parent_id", "final_parent_src", "revenue_cmp"):
            assert col not in header, f"{col} leaked into {name}"


def test_computed_columns_register_in_known_derived():
    """Both features' outputs are referenceable as derived columns."""
    known = known_derived_columns(load_recipe(RECIPE))
    assert {"final_parent_id", "final_parent_src", "revenue_cmp",
            "revenue_cmp_inv"} <= known


def test_no_computed_columns_when_unconfigured():
    """No decision_record/compare_columns keys -> the frame is untouched."""
    df = pl.DataFrame({"a": ["x"]})
    assert apply_output_computations(df, {}).columns == ["a"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _reject(recipe: dict, needle: str):
    with pytest.raises(ValueError) as exc:
        validate_recipe(recipe)
    assert needle in str(exc.value)


def test_control_recipe_validates():
    """Passing control: the fixture recipe raises nothing."""
    validate_recipe(load_recipe(RECIPE))


def test_candidates_below_two_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["derived_l1_id"]
    _reject(recipe, "at least 2 entries")


def test_compare_output_colliding_with_reserved_name_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["output"] = "final_parent_id"
    _reject(recipe, "reserved decision_record output column")


def test_compare_output_colliding_with_is_unmatched_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["output"] = "is_unmatched"
    _reject(recipe, "reserved merged-view flag column")


def test_compare_output_colliding_with_match_step_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["output"] = "match_step"
    _reject(recipe, "a pipeline metadata column")


def test_compare_output_colliding_with_derived_column_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["output"] = "derived_l1_id"
    _reject(recipe, "a derived (inherit) column")


def test_duplicate_compare_output_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][1]["output"] = "revenue_cmp"
    _reject(recipe, "both emit column")


def test_compare_output_colliding_with_source_column_rejected():
    """Source collisions need loaded data -- validate_fields, not validate_recipe."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["output"] = "src_revenue"
    errors, _ = _field_errors(recipe)
    assert any("collides with an existing source column" in e for e in errors)


def test_reserved_name_as_source_column_rejected():
    """A source dataset providing final_parent_id shadows the reserved output."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    errors, _ = _field_errors(recipe, extra_source_col="final_parent_id")
    assert any("reserved output column" in e for e in errors)


def test_candidate_naming_compare_output_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["revenue_cmp", "src_l1_id"]
    _reject(recipe, "Cross-feature references are not supported")


def test_candidate_naming_reserved_column_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["final_parent_id", "src_l1_id"]
    _reject(recipe, "cannot be a candidate")


def test_unknown_candidate_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["nope", "src_l1_id"]
    errors, _ = _field_errors(recipe)
    assert any('candidate "nope" not found' in e for e in errors)


def test_analysis_reference_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["columns"]["analysis"].append(
        {"field": "final_parent_id", "header": "final_parent_id"}
    )
    _reject(recipe, "matched-view only")


def test_analysis_reference_to_compare_output_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["columns"]["analysis"].append(
        {"field": "revenue_cmp", "header": "revenue_cmp"}
    )
    _reject(recipe, "matched-view only")


@pytest.mark.parametrize("key,value", [
    ("decision_record", {"candidates": ["a", "b"]}),
    ("compare_columns", [{"left": "a", "right": "b", "output": "c"}]),
])
def test_multi_phase_rejected(key, value):
    """Both features are global-output-only, like matched_unmatched."""
    recipe = {
        "name": "mp",
        "sources": {"src": {"file": "dr_source.csv"}},
        "phases": [{
            "name": "P1",
            "populations": {"pop1": {"source": "src", "record_key": "vnd_id"}},
            "steps": [{
                "name": "S1", "source": "pop1", "destination": "pop1",
                "match_fields": [{"source": "l3_fmly_nm", "destination": "l3_fmly_nm",
                                  "method": "exact", "tiers": ["raw"]}],
            }],
            "output": {"format": "csv", key: value},
        }],
    }
    _reject(recipe, f"output.{key} is not supported in multi-phase recipes")


# ---------------------------------------------------------------------------
# validate_fields harness
# ---------------------------------------------------------------------------

def _field_errors(recipe: dict, extra_source_col: str | None = None):
    """Run validate_fields against the fixture sources."""
    from recipe import load_source

    sources = {
        name: load_source(cfg, str(DATA_DIR))
        for name, cfg in recipe["sources"].items()
    }
    if extra_source_col:
        sources["src"] = sources["src"].with_columns(
            pl.lit("x").alias(extra_source_col)
        )
    populations = {"pop1": {"config": recipe["populations"]["pop1"],
                            "df": sources["src"], "source": "src"}}
    return validate_fields(recipe, sources, populations)
