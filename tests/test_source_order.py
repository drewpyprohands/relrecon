"""Tests for deterministic output row order (Issue #86)."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from matching import run_pipeline
from recipe import load_recipe
from report import sort_by_source_order

SRC = Path(__file__).parent.parent / "src"
DATA_DIR = Path(__file__).parent.parent / "data"
RECIPE = str(Path(__file__).parent / "recipes" / "source_order_test.yaml")

SOURCE_ORDER = ["V001", "V002", "V003", "V004"]
MATCHED_HEADER = "vnd_id,l3_fmly_nm,match_step,derived_l1_id"


def _load_main():
    spec = importlib.util.spec_from_file_location("relrecon_main", SRC / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_path, recipe=None):
    """Run the pipeline + _write_output the way main() does; return the out dir."""
    recipe = recipe or load_recipe(RECIPE)
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    source_df, source_key = main._resolve_source_order(recipe, result)
    main._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(tmp_path / "data.csv"),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file="source_order_test.yaml",
        timing=result.get("timing"),
        source_df=source_df,
        source_key=source_key,
    )
    return tmp_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ids(path: Path) -> list:
    return pl.read_csv(path)["vnd_id"].to_list()


# ---------------------------------------------------------------------------
# Fixture guard: the engine really does emit these rows out of source order
# ---------------------------------------------------------------------------

def test_fixture_completion_order_differs_from_source_order():
    """Negative control -- the fixture's match order cannot equal source order.

    Asserted structurally, not by observing the engine's emitted row order:
    that order is itself unstable run-to-run, which is the defect under test.
    The last source row matches in step 1 and the earlier ones in step 2, so
    any step-ordered resolution puts V003 ahead of V001/V002.
    """
    result = run_pipeline(load_recipe(RECIPE), base_dir=str(DATA_DIR))
    steps = dict(zip(
        result["matched"]["vnd_id"].to_list(),
        result["matched"]["match_step"].to_list(),
        strict=True,
    ))
    assert steps == {
        "V001": "Exact L3", "V002": "Exact L3", "V003": "Alias L3",
    }, "fixture no longer matches out of source order"


# ---------------------------------------------------------------------------
# Source-order output
# ---------------------------------------------------------------------------

def test_matched_csv_in_source_order(tmp_path):
    _run(tmp_path)
    assert _ids(tmp_path / "data.csv") == ["V001", "V002", "V003"]


def test_matched_parquet_in_source_order(tmp_path):
    _run(tmp_path)
    assert pl.read_parquet(tmp_path / "data.parquet")["vnd_id"].to_list() == [
        "V001", "V002", "V003",
    ]


def test_unmatched_companion_in_source_order(tmp_path):
    _run(tmp_path)
    assert _ids(tmp_path / "data_unmatched.csv") == ["V004"]


def test_merged_matched_block_in_source_order_then_unmatched(tmp_path):
    """Matched block sorted by source order, still ahead of the unmatched block."""
    _run(tmp_path)
    merged = pl.read_csv(tmp_path / "data_merged.csv")
    assert merged["vnd_id"].to_list() == SOURCE_ORDER
    flags = merged["is_unmatched"].to_list()
    assert flags == [False, False, False, True]


def test_no_new_column_in_any_artifact(tmp_path):
    """Ordering helpers never reach an artifact."""
    _run(tmp_path)
    expected = MATCHED_HEADER.split(",")
    assert pl.read_csv(tmp_path / "data.csv").columns == expected
    assert pl.read_parquet(tmp_path / "data.parquet").columns == expected
    assert pl.read_csv(tmp_path / "data_merged.csv").columns == expected + ["is_unmatched"]
    assert pl.read_csv(tmp_path / "data_unmatched.csv").columns == ["vnd_id", "l3_fmly_nm"]


# ---------------------------------------------------------------------------
# Determinism: two runs of the same recipe are byte-identical
# ---------------------------------------------------------------------------

def test_repeat_runs_byte_identical(tmp_path):
    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    for name in ("data.csv", "data_merged.csv", "data_unmatched.csv",
                 "data.parquet", "data_merged.parquet"):
        assert _sha256(a / name) == _sha256(b / name), f"{name} differs between runs"


# ---------------------------------------------------------------------------
# sort_by_source_order unit behaviour
# ---------------------------------------------------------------------------

def test_sort_helper_puts_unknown_keys_last():
    source = pl.DataFrame({"k": ["a", "b", "c"]})
    df = pl.DataFrame({"k": ["zz", "c", "a"], "v": [1, 2, 3]})
    assert sort_by_source_order(df, source, "k")["k"].to_list() == ["a", "c", "zz"]


def test_sort_helper_is_noop_without_source():
    df = pl.DataFrame({"k": ["b", "a"]})
    assert sort_by_source_order(df, None, "k")["k"].to_list() == ["b", "a"]


def test_sort_helper_is_noop_when_key_absent():
    source = pl.DataFrame({"k": ["a", "b"]})
    df = pl.DataFrame({"other": ["b", "a"]})
    assert sort_by_source_order(df, source, "k")["other"].to_list() == ["b", "a"]


def test_sort_helper_survives_colliding_helper_column_names():
    """A source column literally named _source_order must not break the sort."""
    source = pl.DataFrame({"k": ["a", "b"], "_source_order": [9, 9]})
    df = pl.DataFrame({"k": ["b", "a"], "_source_order": [9, 9],
                       "_source_order_key": ["x", "y"]})
    out = sort_by_source_order(df, source, "k")
    assert out["k"].to_list() == ["a", "b"]
    assert out.columns == ["k", "_source_order", "_source_order_key"]
