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
LEX_RECIPE = str(RECIPES / "decision_record_lexicographic_test.yaml")


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
    assert unmatched == ["V004,unmatched,S1004,src_l1_id,,,,,true"]


def test_all_candidates_null_row_is_empty(tmp_path):
    """V003 has no derived_l1_id and no src_l1_id: both columns empty, no sentinel."""
    _run(load_recipe(RECIPE), tmp_path)
    df = pl.read_csv(tmp_path / "data.csv")
    row = df.filter(pl.col("vnd_id") == "V003").to_dicts()[0]
    assert row["final_l1_id"] is None
    assert row["final_l1_id_src"] is None


def test_whitespace_candidate_falls_through(tmp_path):
    """V002's inherited derived_l1_id is whitespace-only -> treated as null."""
    _run(load_recipe(RECIPE), tmp_path)
    df = pl.read_csv(tmp_path / "data.csv")
    row = df.filter(pl.col("vnd_id") == "V002").to_dicts()[0]
    assert row["final_l1_id"] == "S1002"
    assert row["final_l1_id_src"] == "src_l1_id"


def test_empty_string_candidate_is_null():
    """A literal empty string (not just a null) is a fall-through, not a value."""
    df = pl.DataFrame({"a": ["", "x"], "b": ["fallback", "fallback"]})
    assert _decide(df, ["a", "b"]) == (["fallback", "x"], ["b", "a"])


# ---------------------------------------------------------------------------
# compare_columns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("left,right,expected", [
    ("10", "5", "higher"),
    ("5", "10", "lower"),
    ("5", "5", "same"),
    (None, "5", None),
    ("5", None, None),
    (None, None, None),
])
def test_compare_cases(left, right, expected):
    """higher/lower/same are left-relative; any null operand yields an empty cell."""
    df = pl.DataFrame({"l": [left], "r": [right]}, schema={"l": pl.String, "r": pl.String})
    assert _cmp(df, left="l", right="r") == [expected]


def _cmp(df, **entry):
    """Compute one compare_columns entry and return its column as a list."""
    entry.setdefault("output", "cmp")
    out = apply_output_computations(df, {
        "compare_columns": [entry],
        "columns": {"matched": [{"field": entry["output"], "header": entry["output"]}]},
    })
    return out[entry["output"]].to_list()


def test_nonnumeric_never_aborts():
    """An unparseable value is data, not a config error. No abort path exists."""
    df = pl.DataFrame({"l": ["12", "abc"], "r": ["1", "2"]})
    assert _cmp(df, left="l", right="r", strip_prefix="none") == ["higher", "higher"]


@pytest.mark.parametrize("left,right,expected", [
    ("abc", "abd", "lower"),      # both unparseable -> lexicographic
    ("abd", "abc", "higher"),
    ("abc", "abc", "same"),
    ("abc", "12", "higher"),      # one side parses -> still lexicographic
])
def test_lexicographic_fallback(left, right, expected):
    """Unless both sides parse as integers, the compare is lexicographic."""
    df = pl.DataFrame({"l": [left], "r": [right]})
    assert _cmp(df, left="l", right="r", strip_prefix="none") == [expected]


def test_numeric_beats_lexicographic_when_both_parse():
    """9 vs 100 is 'lower' numerically though 'higher' as text.

    Parseability, not strip_prefix, picks the branch: bare digits parse under
    every strip setting, so both spellings agree here.
    """
    df = pl.DataFrame({"l": ["9"], "r": ["100"]})
    assert _cmp(df, left="l", right="r") == ["lower"]
    assert _cmp(df, left="l", right="r", strip_prefix="none") == ["lower"]


@pytest.mark.parametrize("left,right,expected", [
    ("9.5", "10.5", "lower"),     # numeric, not text ("9" > "1" would be higher)
    ("2.50", "2.5", "same"),      # equal as floats, unequal as text
    ("-1.5", "0", "lower"),
])
def test_decimals_compare_numerically(left, right, expected):
    """Float64 parsing: decimal values take the numeric branch."""
    df = pl.DataFrame({"l": [left], "r": [right]})
    assert _cmp(df, left="l", right="r") == [expected]


def test_decimal_vs_unparseable_is_lexicographic():
    """Only one side parses, so the text branch decides."""
    df = pl.DataFrame({"l": ["9.5"], "r": ["12-34"]})
    assert _cmp(df, left="l", right="r") == ["higher"]


@pytest.mark.parametrize("row,revenue_cmp,revenue_cmp_inv", [
    ("V005", "lower", "higher"),   # 9.5 vs 10.5 -> numeric
    ("V006", "same", "same"),      # 2.50 vs 2.5 -> equal as floats
    ("V007", "higher", "lower"),   # 9.5 vs "12-34" -> lexicographic
])
def test_decimal_fixture_rows(tmp_path, row, revenue_cmp, revenue_cmp_inv):
    """The decimal rows of the compare fixture, from the written artifact."""
    _run(load_recipe(RECIPE), tmp_path)
    df = pl.read_csv(tmp_path / "data.csv")
    got = df.filter(pl.col("vnd_id") == row).to_dicts()[0]
    assert got["revenue_cmp"] == revenue_cmp
    assert got["revenue_cmp_inv"] == revenue_cmp_inv


def test_select_min_uses_float_parsing():
    """decision_record min/max parses floats too, not just integers."""
    df = pl.DataFrame({"a": ["10.5"], "b": ["9.5"]})
    assert _decide(df, ["a", "b"], select="min") == (["9.5"], ["b"])


def test_alpha_strip_default_vs_none():
    """Default alpha strip parses the digits; none compares the raw strings."""
    df = pl.DataFrame({"l": ["S1001"], "r": ["P3100"]})
    assert _cmp(df, left="l", right="r") == ["lower"]
    assert _cmp(df, left="l", right="r", strip_prefix="none") == ["higher"]


def test_alpha_strip_makes_prefix_only_difference_same():
    """Documented consequence: AB123 vs CD123 reads 'same' under alpha strip."""
    df = pl.DataFrame({"l": ["AB123"], "r": ["CD123"]})
    assert _cmp(df, left="l", right="r") == ["same"]
    assert _cmp(df, left="l", right="r", strip_prefix="none") == ["lower"]


def test_anchored_regex_strip_prefix():
    """A non-alpha strip_prefix is an anchored regex, as in tie_breaker."""
    df = pl.DataFrame({"l": ["VND-9"], "r": ["VND-100"]})
    assert _cmp(df, left="l", right="r", strip_prefix="VND-") == ["lower"]


def test_lexicographic_recipe_runs_and_writes(tmp_path):
    """End-to-end: a text column in a compare pair completes and writes."""
    recipe = load_recipe(LEX_RECIPE)
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    main._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(tmp_path / "data.csv"),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file="decision_record_lexicographic_test.yaml",
        timing=result.get("timing"),
        source_df=result["populations"].get("pop1"),
        source_key="vnd_id",
    )
    rows = (tmp_path / "data.csv").read_text().splitlines()
    assert rows[0] == "vnd_id,text_cmp"
    # Names sort above the revenue digits, so every populated row is 'higher'.
    assert [r.split(",")[1] for r in rows[1:]] == ["higher"] * 6


# ---------------------------------------------------------------------------
# select: first | min | max
# ---------------------------------------------------------------------------

def _decide(df, candidates, **cfg):
    """Compute one decision_record and return (value list, src list)."""
    cfg.setdefault("write_to", "decided")
    cfg["candidates"] = candidates
    out = apply_output_computations(df, {
        "decision_record": cfg,
        "columns": {"matched": [
            {"field": "decided", "header": "decided"},
            {"field": "decided_src", "header": "decided_src"},
        ]},
    })
    return out["decided"].to_list(), out["decided_src"].to_list()


def test_select_first_is_the_default():
    """Absent select behaves as list-order coalesce."""
    df = pl.DataFrame({"a": ["700"], "b": ["100"]})
    assert _decide(df, ["a", "b"]) == (["700"], ["a"])


@pytest.mark.parametrize("select,value,src", [
    ("min", "100", "b"),
    ("max", "700", "a"),
])
def test_select_min_max(select, value, src):
    df = pl.DataFrame({"a": ["700"], "b": ["100"]})
    assert _decide(df, ["a", "b"], select=select) == ([value], [src])


def test_select_min_tie_falls_back_to_list_order():
    """Equal values resolve to the earlier candidate, so _src reads 'a'."""
    df = pl.DataFrame({"a": ["100"], "b": ["100"]})
    assert _decide(df, ["a", "b"], select="min") == (["100"], ["a"])


def test_select_min_skips_unpopulated_candidates():
    """A null candidate never wins, however it would have sorted."""
    df = pl.DataFrame({"a": [None], "b": ["500"]}, schema={"a": pl.String, "b": pl.String})
    assert _decide(df, ["a", "b"], select="min") == (["500"], ["b"])


def test_select_min_all_unparseable_falls_back_to_list_order():
    """Nothing parses -> no value ordering exists -> earliest populated wins."""
    df = pl.DataFrame({"a": ["xx"], "b": ["yy"]})
    assert _decide(df, ["a", "b"], select="min") == (["xx"], ["a"])


def test_select_min_unparseable_cannot_win():
    """A candidate that will not parse loses to one that does."""
    df = pl.DataFrame({"a": ["xx"], "b": ["500"]})
    assert _decide(df, ["a", "b"], select="min") == (["500"], ["b"])


def test_select_min_uses_strip_prefix():
    """Alpha strip is applied before parsing, so S9 (9) beats P100 (100).

    Under strip_prefix: none neither value parses, so min/max has nothing to
    order by and the decision falls back to list order.
    """
    df = pl.DataFrame({"a": ["S9"], "b": ["P100"]})
    assert _decide(df, ["a", "b"], select="min") == (["S9"], ["a"])
    assert _decide(df, ["a", "b"], select="min", strip_prefix="none") == (["S9"], ["a"])


def test_select_min_all_null_row_is_empty():
    df = pl.DataFrame({"a": [None], "b": ["  "]}, schema={"a": pl.String, "b": pl.String})
    assert _decide(df, ["a", "b"], select="min") == ([None], [None])


def test_decided_value_is_the_original_not_the_stripped_form():
    """Stripping is a parsing device; the emitted value keeps its prefix."""
    df = pl.DataFrame({"a": ["S700"], "b": ["P100"]})
    assert _decide(df, ["a", "b"], select="min") == (["P100"], ["b"])


# ---------------------------------------------------------------------------
# Permutation fixture (owner's recipe + expected-winners table)
# ---------------------------------------------------------------------------

PERM_RECIPE = str(
    Path(__file__).parent.parent / "config" / "recipes" / "multipop_comparison_rollup.yaml"
)

# (row, source, dest, rolled, case, decided, _src) from Issue #93 comment 2.
PERM_EXPECTED = [
    ("vnd5001", "700", "100", "100", 5, "100", "rolled_l1_id"),
    ("vnd5002", "100", "700", "100", 1, "100", "rolled_l1_id"),
    ("vnd6001", "900", "500", "100", 4, "100", "rolled_l1_id"),
    ("vnd7001", "800", "30", "800", 6, "30", "derived_l1_id"),
    ("vnd7002", "30", "800", "30", 1, "30", "rolled_l1_id"),
]


def _run_permutation(tmp_path):
    recipe = load_recipe(PERM_RECIPE)
    result = run_pipeline(recipe, base_dir=str(DATA_DIR))
    main = _load_main()
    main._write_output(
        output_cfg=recipe["output"],
        matched_df=result["matched"],
        unmatched_df=result.get("unmatched"),
        output_path=str(tmp_path / "data.csv"),
        stats=result.get("stats", {}),
        recipe=recipe,
        recipe_file="multipop_comparison_rollup.yaml",
        timing=result.get("timing"),
        source_df=result["populations"].get("mg_pop"),
        source_key="vnd_id",
    )
    return pl.read_csv(tmp_path / "data_merged.csv", infer_schema_length=0)


@pytest.mark.parametrize("row,source,dest,rolled,case,decided,src", PERM_EXPECTED)
def test_select_min_permutation_winners(tmp_path, row, source, dest, rolled, case, decided, src):
    """Cases 1/4/5/6 over {rolled, dest, source}; 2 and 3 cannot occur."""
    df = _run_permutation(tmp_path)
    got = df.filter(pl.col("Source L3 ID") == row).to_dicts()[0]
    assert got["Source Parent ID"] == source
    assert got["Destination L1 ID"] == dest
    assert got["Rolled L1 ID"] == rolled
    assert got["Decided Parent ID"] == decided, f"case {case}"
    assert got["Decided Parent Src"] == src, f"case {case}"


def test_permutation_merged_byte_exact(tmp_path):
    _run_permutation(tmp_path)
    assert (tmp_path / "data_merged.csv").read_text() == (
        EXPECTED / "multipop_comparison_merged.csv"
    ).read_text()


def test_permutation_impossible_cases_absent(tmp_path):
    """rolled > source cannot occur under rollup order: asc (cases 2 and 3)."""
    df = _run_permutation(tmp_path).filter(pl.col("is_unmatched") == "false")
    offenders = [
        r for r in df.to_dicts()
        if int(r["Rolled L1 ID"]) > int(r["Source Parent ID"])
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Visibility / registration
# ---------------------------------------------------------------------------

def test_columns_absent_when_not_listed(tmp_path):
    """Configured but unlisted computed columns never reach an artifact."""
    _run(load_recipe(UNLISTED_RECIPE), tmp_path)
    for name in ("data.csv", "data_merged.csv", "data_unmatched.csv"):
        header = (tmp_path / name).read_text().splitlines()[0]
        for col in ("final_l1_id", "final_l1_id_src", "revenue_cmp"):
            assert col not in header, f"{col} leaked into {name}"


def test_computed_columns_register_in_known_derived():
    """Both features' outputs are referenceable as derived columns."""
    known = known_derived_columns(load_recipe(RECIPE))
    assert {"final_l1_id", "final_l1_id_src", "revenue_cmp",
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
    recipe["output"]["compare_columns"][0]["output"] = "final_l1_id"
    _reject(recipe, "a decision_record output column")


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
    """A source dataset providing final_l1_id shadows the reserved output."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    errors, _ = _field_errors(recipe, extra_source_col="final_l1_id")
    assert any("collides with an existing source column" in e for e in errors)


@pytest.mark.parametrize("name", ["final_l1_id", "final_l1_id_src"])
def test_inherit_as_colliding_with_reserved_name_rejected(name):
    """A derived column may not claim a reserved decision_record output name."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["steps"][0]["inherit"][0]["as"] = name
    _reject(recipe, "A derived column cannot take a generated column's name")


def test_inherit_as_colliding_with_compare_output_rejected():
    """Same rule against a configured compare output name."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["steps"][0]["inherit"][0]["as"] = "revenue_cmp"
    _reject(recipe, "a compare_columns output column")


def test_rollup_write_to_colliding_with_reserved_name_rejected():
    """final_rollup is a derived-column path too."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["final_rollup"] = [{
        "group_key": "l3_fmly_nm", "target": "src_l1_id",
        "write_to": "final_l1_id",
    }]
    _reject(recipe, "A derived column cannot take a generated column's name")


@pytest.mark.parametrize("role", ["left", "right"])
@pytest.mark.parametrize("col", ["final_l1_id", "final_l1_id_src"])
def test_compare_operand_naming_reserved_column_rejected(role, col):
    """Operands are read before any generated column exists."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0][role] = col
    _reject(recipe, "Cross-feature references are not supported")


@pytest.mark.parametrize("role", ["left", "right"])
def test_compare_operand_naming_compare_output_rejected(role):
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0][role] = "revenue_cmp_inv"
    _reject(recipe, "Cross-feature references are not supported")


def test_compare_operand_naming_own_output_rejected():
    """An entry reading its own output back is the degenerate cross-reference."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["compare_columns"][0]["left"] = "revenue_cmp"
    _reject(recipe, "Cross-feature references are not supported")


def test_missing_write_to_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    del recipe["output"]["decision_record"]["write_to"]
    _reject(recipe, "write_to is required")


@pytest.mark.parametrize("name,needle", [
    ("derived_l1_id", "a derived (inherit) column"),
    ("match_step", "a pipeline metadata column"),
    ("is_unmatched", "the reserved merged-view flag column"),
    ("revenue_cmp", "a compare_columns output"),
])
def test_write_to_collision_rejected(name, needle):
    """write_to is policed against the same name space as compare outputs."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["write_to"] = name
    _reject(recipe, needle)


def test_write_to_src_collision_rejected():
    """The _src companion is claimed too, not just the base name."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["steps"][0]["inherit"].append(
        {"source": "dst_l1_id", "as": "decided_src"}
    )
    recipe["output"]["decision_record"]["write_to"] = "decided"
    _reject(recipe, "a derived (inherit) column")


def test_write_to_colliding_with_source_column_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["write_to"] = "src_l1_id"
    errors, _ = _field_errors(recipe)
    assert any("collides with an existing source column" in e for e in errors)


def test_invalid_select_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["select"] = "median"
    with pytest.raises(ValueError) as exc:
        validate_recipe(recipe)
    assert "select" in str(exc.value)


def test_derived_and_operand_control_validates():
    """Passing control: real derived names and real source operands validate."""
    recipe = copy.deepcopy(load_recipe(RECIPE))
    assert recipe["steps"][0]["inherit"][0]["as"] == "derived_l1_id"
    assert recipe["output"]["compare_columns"][0]["left"] == "src_revenue"
    assert recipe["output"]["compare_columns"][0]["right"] == "dst_revenue"
    validate_recipe(recipe)


def test_candidate_naming_compare_output_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["revenue_cmp", "src_l1_id"]
    _reject(recipe, "Cross-feature references are not supported")


def test_candidate_naming_reserved_column_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["final_l1_id", "src_l1_id"]
    _reject(recipe, "Cross-feature references are not supported")


def test_unknown_candidate_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["decision_record"]["candidates"] = ["nope", "src_l1_id"]
    errors, _ = _field_errors(recipe)
    assert any('candidate "nope" not found' in e for e in errors)


def test_analysis_reference_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["columns"]["analysis"].append(
        {"field": "final_l1_id", "header": "final_l1_id"}
    )
    _reject(recipe, "matched-view only")


def test_analysis_reference_to_compare_output_rejected():
    recipe = copy.deepcopy(load_recipe(RECIPE))
    recipe["output"]["columns"]["analysis"].append(
        {"field": "revenue_cmp", "header": "revenue_cmp"}
    )
    _reject(recipe, "matched-view only")


@pytest.mark.parametrize("key,value", [
    ("decision_record", {"candidates": ["a", "b"], "write_to": "d"}),
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
