"""Runtime guard: source column literally named is_unmatched (Issue #91)."""

import importlib.util
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matching import run_pipeline
from recipe import load_recipe

SRC = Path(__file__).parent.parent / "src"
DATA_DIR = Path(__file__).parent.parent / "data"
RECIPE = str(Path(__file__).parent / "recipes" / "is_unmatched_collision_test.yaml")

SOURCE_VALUES = ["keep_me", "keep_me_too", "keep_me_three"]


def _assert_passthrough(tmp_path):
    """Column reaches both artifacts with its source values, unflagged.

    Matched row order is not fixed without an output source_key, so values
    are compared as a set.
    """
    matched = pl.read_csv(tmp_path / "data.csv")
    unmatched = pl.read_csv(tmp_path / "data_unmatched.csv")
    assert sorted(matched["is_unmatched"].to_list()) == sorted(SOURCE_VALUES[:2])
    assert unmatched["is_unmatched"].to_list() == SOURCE_VALUES[2:]
    assert matched["is_unmatched"].dtype == pl.String  # not overwritten by a bool
    assert not (tmp_path / "data_merged.csv").exists()


def _load_main():
    spec = importlib.util.spec_from_file_location("relrecon_main", SRC / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(recipe: dict, tmp_path, out_name="data.csv"):
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    main._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(tmp_path / out_name),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file="is_unmatched_collision_test.yaml",
        timing=result.get("timing"),
    )
    return tmp_path


def test_fixture_carries_colliding_column():
    """Control: the column really does reach both output frames."""
    result = run_pipeline(load_recipe(RECIPE), base_dir=str(DATA_DIR))
    assert "is_unmatched" in result["matched"].columns
    assert "is_unmatched" in result["unmatched"].columns


# ---------------------------------------------------------------------------
# Error case: merged configured
# ---------------------------------------------------------------------------

def test_merged_aborts_on_colliding_column(tmp_path):
    with pytest.raises(ValueError, match='"is_unmatched" collides'):
        _run(load_recipe(RECIPE), tmp_path)


def test_merged_writes_zero_artifacts(tmp_path):
    """The abort happens before any artifact lands, separate ones included."""
    recipe = load_recipe(RECIPE)
    recipe["output"]["matched_unmatched"] = ["merged", "separate"]
    recipe["output"]["format"] = ["csv", "parquet"]
    recipe["output"]["summary"] = ["md", "xlsx"]
    with pytest.raises(ValueError, match="is_unmatched"):
        _run(recipe, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_error_names_column_and_frames(tmp_path):
    with pytest.raises(ValueError) as exc:
        _run(load_recipe(RECIPE), tmp_path)
    msg = str(exc.value)
    assert "is_unmatched" in msg
    assert "matched and unmatched" in msg
    assert "matched_unmatched" in msg


def test_merged_without_collision_still_works(tmp_path):
    """Pass case: same recipe on the non-colliding source -- merged emitted."""
    recipe = load_recipe(RECIPE)
    recipe["sources"]["src"]["file"] = "mu_source.csv"
    _run(recipe, tmp_path)
    merged = pl.read_csv(tmp_path / "data_merged.csv")
    assert merged.height == 3
    assert merged["is_unmatched"].to_list() == [False, False, True]


def test_guard_only_fires_for_matched_frame(tmp_path):
    """A collision in either frame alone is enough to abort."""
    from report import check_merged_reserved_collision

    clean = pl.DataFrame({"vnd_id": ["V001"]})
    dirty = pl.DataFrame({"vnd_id": ["V001"], "is_unmatched": ["x"]})
    check_merged_reserved_collision(clean, clean)   # no raise
    check_merged_reserved_collision(clean, None)    # no raise
    with pytest.raises(ValueError, match="matched frame"):
        check_merged_reserved_collision(dirty, clean)
    with pytest.raises(ValueError, match="unmatched frame"):
        check_merged_reserved_collision(clean, dirty)


# ---------------------------------------------------------------------------
# Pass cases: merged not configured
# ---------------------------------------------------------------------------

def test_legacy_no_matched_unmatched_passes_through(tmp_path):
    """No matched_unmatched key -> unchanged behavior, column untouched."""
    recipe = load_recipe(RECIPE)
    del recipe["output"]["matched_unmatched"]
    recipe["output"]["emit_unmatched"] = True  # legacy companion artifact
    _run(recipe, tmp_path)

    _assert_passthrough(tmp_path)


def test_separate_passes_through(tmp_path):
    """matched_unmatched: separate -> succeeds, column passes through."""
    recipe = load_recipe(RECIPE)
    recipe["output"]["matched_unmatched"] = "separate"
    _run(recipe, tmp_path)
    _assert_passthrough(tmp_path)
