"""Tests for JSON Schema-based recipe validation (Issue #61)."""

import copy
import pytest
from pathlib import Path

from src.recipe import validate_recipe

# Minimal valid recipe
_BASE = {
    "name": "test",
    "sources": {"src": {"file": "test.csv", "type": "trusted_reference"}},
    "populations": {
        "pop1": {"source": "src", "record_key": "id"},
    },
    "steps": [
        {
            "name": "step1",
            "source": "pop1",
            "destination": "src",
            "match_fields": [
                {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]}
            ],
        }
    ],
    "output": {"format": "xlsx", "summary": ["md", "xlsx"]},
}


def _make(**overrides):
    r = copy.deepcopy(_BASE)
    r.update(overrides)
    return r


# ---------------------------------------------------------------------------
# Clean recipes produce no warnings
# ---------------------------------------------------------------------------

class TestCleanRecipes:
    def test_minimal_valid(self):
        assert validate_recipe(_make()) == []

    def test_all_optional_fields(self):
        """Recipe with every optional field should still be clean."""
        r = _make(
            description="test desc",
            normalization={"stopwords": "x.json", "aliases": "y.json", "unicode": {"ranges": "z.json", "mode": "skip"}},
        )
        r["steps"][0]["address_support"] = {
            "source": ["a1"], "destination": ["a2"], "parser": "auto", "threshold": 60,
        }
        r["steps"][0]["date_gate"] = {"field": "dt", "max_age_years": 2, "applies_to": "destination"}
        r["steps"][0]["filters"] = [
            {"field": "x", "op": "eq", "value": "1", "applies_to": "destination"}
        ]
        r["steps"][0]["inherit"] = [{"source": "col1", "as": "new_col"}]
        r["output"]["match_mode"] = "best_match"
        r["output"]["path"] = "out.xlsx"
        r["output"]["columns"] = {
            "matched": [{"field": "name", "header": "Name"}],
            "analysis": [{"field": "name", "header": "Name"}],
        }
        assert validate_recipe(r) == []

    def test_l1_recipe(self):
        """The real L1 recipe should validate clean."""
        import yaml
        recipe_path = Path(__file__).parent.parent / "config" / "recipes" / "l1_reconciliation.yaml"
        if not recipe_path.exists():
            pytest.skip("L1 recipe not found")
        with open(recipe_path) as f:
            recipe = yaml.safe_load(f)
        warnings = validate_recipe(recipe)
        # Filter out known expected warnings
        schema_warnings = [
            w for w in warnings
            if "record_key" not in w and "tabs" not in w
        ]
        assert schema_warnings == [], f"L1 recipe has unexpected schema warnings: {schema_warnings}"


# ---------------------------------------------------------------------------
# Unrecognized keys caught as warnings (additionalProperties: false)
# ---------------------------------------------------------------------------

class TestUnrecognizedKeys:
    def test_root_typo(self):
        r = _make(debugg=True)
        warnings = validate_recipe(r)
        assert any("debugg" in w for w in warnings)

    def test_root_typo_soruces(self):
        r = _make()
        r["soruces"] = {"x": {"file": "x.csv"}}
        warnings = validate_recipe(r)
        assert any("soruces" in w for w in warnings)

    def test_source_unknown_key(self):
        r = _make()
        r["sources"]["src"]["encoding"] = "utf-8"
        warnings = validate_recipe(r)
        assert any("encoding" in w for w in warnings)

    def test_population_unknown_key(self):
        r = _make()
        r["populations"]["pop1"]["dedup"] = True
        warnings = validate_recipe(r)
        assert any("dedup" in w for w in warnings)

    def test_step_unknown_key(self):
        r = _make()
        r["steps"][0]["weighting"] = 0.5
        warnings = validate_recipe(r)
        assert any("weighting" in w for w in warnings)

    def test_match_field_unknown_key(self):
        r = _make()
        r["steps"][0]["match_fields"][0]["case_insensitive"] = True
        warnings = validate_recipe(r)
        assert any("case_insensitive" in w for w in warnings)

    def test_address_support_unknown_key(self):
        r = _make()
        r["steps"][0]["address_support"] = {
            "source": ["a"], "destination": ["b"], "fuzzy": True,
        }
        warnings = validate_recipe(r)
        assert any("fuzzy" in w for w in warnings)

    def test_date_gate_unknown_key(self):
        r = _make()
        r["steps"][0]["date_gate"] = {"field": "dt", "max_age_years": 1, "applies_to": "destination", "format": "%Y"}
        warnings = validate_recipe(r)
        assert any("format" in w for w in warnings)

    def test_inherit_unknown_key(self):
        r = _make()
        r["steps"][0]["inherit"] = [{"source": "x", "as": "y", "default": "N/A"}]
        warnings = validate_recipe(r)
        assert any("default" in w for w in warnings)

    def test_output_unknown_key(self):
        r = _make()
        r["output"]["sheet_name"] = "Results"
        warnings = validate_recipe(r)
        assert any("sheet_name" in w for w in warnings)

    def test_normalization_unknown_key(self):
        r = _make(normalization={"stopwords": "x.json", "case_fold": True})
        warnings = validate_recipe(r)
        assert any("case_fold" in w for w in warnings)


# ---------------------------------------------------------------------------
# YAML indent bug detection (the original motivation for #61)
# ---------------------------------------------------------------------------

class TestIndentBugs:
    def test_filter_on_step_instead_of_population(self):
        """'filter' (singular) is a population key, not a step key."""
        r = _make()
        r["steps"][0]["filter"] = [{"field": "x", "op": "eq", "value": "1"}]
        warnings = validate_recipe(r)
        assert any("filter" in w and "steps" in w for w in warnings)

    def test_record_key_on_step(self):
        """record_key belongs on populations, not steps."""
        r = _make()
        r["steps"][0]["record_key"] = "vendor_id"
        warnings = validate_recipe(r)
        assert any("record_key" in w for w in warnings)

    def test_action_on_step(self):
        """action belongs on populations, not steps."""
        r = _make()
        r["steps"][0]["action"] = "exclude"
        warnings = validate_recipe(r)
        assert any("action" in w for w in warnings)


# ---------------------------------------------------------------------------
# Structural errors raise ValueError
# ---------------------------------------------------------------------------

class TestStructuralErrors:
    def test_missing_name(self):
        r = _make()
        del r["name"]
        with pytest.raises(ValueError):
            validate_recipe(r)

    def test_missing_sources(self):
        r = _make()
        del r["sources"]
        with pytest.raises(ValueError):
            validate_recipe(r)

    def test_missing_step_name(self):
        r = _make()
        del r["steps"][0]["name"]
        with pytest.raises(ValueError):
            validate_recipe(r)

    def test_invalid_method_enum(self):
        r = _make()
        r["steps"][0]["match_fields"][0]["method"] = "approximate"
        with pytest.raises(ValueError, match="validation failed"):
            validate_recipe(r)

    def test_invalid_output_format(self):
        r = _make()
        r["output"]["format"] = "pdf"
        with pytest.raises(ValueError, match="validation failed"):
            validate_recipe(r)


# ---------------------------------------------------------------------------
# Semantic warnings (record_key)
# ---------------------------------------------------------------------------

class TestSemanticWarnings:
    def test_missing_record_key_warns(self):
        r = _make()
        del r["populations"]["pop1"]["record_key"]
        warnings = validate_recipe(r)
        assert any("record_key" in w for w in warnings)

    def test_record_key_present_no_warning(self):
        r = _make()
        warnings = validate_recipe(r)
        assert not any("record_key" in w for w in warnings)


# ---------------------------------------------------------------------------
# New schema features (filters, record_key in schema)
# ---------------------------------------------------------------------------

class TestNewSchemaFeatures:
    def test_step_filters_valid(self):
        r = _make()
        r["steps"][0]["filters"] = [
            {"field": "status", "op": "eq", "value": "active", "applies_to": "destination"},
            {"field": "date", "op": "max_age_years", "value": 2, "applies_to": "source"},
        ]
        warnings = validate_recipe(r)
        # No schema warnings (record_key warning is fine)
        schema_warnings = [w for w in warnings if "record_key" not in w]
        assert schema_warnings == []

    def test_population_record_key_valid(self):
        r = _make()
        r["populations"]["pop1"]["record_key"] = "vendor_id"
        warnings = validate_recipe(r)
        assert not any("record_key" in w for w in warnings)

    def test_exclude_on_step_source_raises(self):
        r = _make()
        r["populations"]["pop1"]["action"] = "exclude"
        import pytest
        with pytest.raises(ValueError, match="action: exclude"):
            validate_recipe(r)

    def test_duplicate_step_names_raises(self):
        r = _make()
        r["steps"].append(copy.deepcopy(r["steps"][0]))  # same name "step1"
        with pytest.raises(ValueError, match="Duplicate step name"):
            validate_recipe(r)

    def test_unique_step_names_ok(self):
        r = _make()
        step2 = copy.deepcopy(r["steps"][0])
        step2["name"] = "step2"
        r["steps"].append(step2)
        warnings = validate_recipe(r)
        assert not any("Duplicate" in w for w in warnings)

    def test_multiple_errors_reported_together(self):
        """All critical errors should be in one ValueError, not one at a time."""
        r = _make()
        # Create 3 duplicate step names (2 errors: steps 0&1 and 0&2)
        step2 = copy.deepcopy(r["steps"][0])  # duplicate "step1"
        step3 = copy.deepcopy(r["steps"][0])  # another duplicate "step1"
        step3["name"] = "step1"  # same name
        r["steps"].extend([step2, step3])
        # Also add a bad exclude-on-source
        r["populations"]["pop1"]["action"] = "exclude"
        with pytest.raises(ValueError, match="3 errors") as exc_info:
            validate_recipe(r)
        msg = str(exc_info.value)
        assert msg.count("Duplicate step name") == 2
        assert "action: exclude" in msg

    def test_single_error_no_count_prefix(self):
        """A single error should not say '1 errors'."""
        r = _make()
        r["steps"].append(copy.deepcopy(r["steps"][0]))  # one duplicate
        with pytest.raises(ValueError, match="Duplicate step name"):
            validate_recipe(r)
        # Should NOT contain "errors)" phrasing for single error
        try:
            validate_recipe(r)
        except ValueError as e:
            assert "errors)" not in str(e)


# ---------------------------------------------------------------------------
# step_defaults expansion
# ---------------------------------------------------------------------------

class TestStepDefaults:
    def test_defaults_merged_into_steps(self):
        """step_defaults values appear in steps after expansion."""
        from src.recipe import _apply_step_defaults
        r = _make()
        r["step_defaults"] = {
            "address_support": {
                "source": ["a1"], "destination": ["a2"],
                "parser": "auto", "threshold": 75,
            }
        }
        expanded = _apply_step_defaults(copy.deepcopy(r))
        assert expanded["steps"][0]["address_support"]["threshold"] == 75
        assert expanded["steps"][0]["address_support"]["parser"] == "auto"
        # step_defaults removed after expansion
        assert "step_defaults" not in expanded

    def test_step_values_override_defaults(self):
        """Step-level values win over defaults."""
        from src.recipe import _apply_step_defaults
        r = _make()
        r["step_defaults"] = {
            "address_support": {
                "source": ["a1"], "destination": ["a2"],
                "parser": "auto", "threshold": 75,
            }
        }
        r["steps"][0]["address_support"] = {
            "source": ["x1"], "destination": ["x2"],
            "parser": "default", "threshold": 60,
        }
        expanded = _apply_step_defaults(copy.deepcopy(r))
        assert expanded["steps"][0]["address_support"]["threshold"] == 60
        assert expanded["steps"][0]["address_support"]["parser"] == "default"

    def test_deep_merge_nested_dicts(self):
        """Deep merge handles nested dicts (e.g. weights inside address_support)."""
        from src.recipe import _apply_step_defaults
        r = _make()
        r["step_defaults"] = {
            "address_support": {
                "source": ["a1"], "destination": ["a2"],
                "weights": {"street_name": 0.6, "city": 0.2},
            }
        }
        r["steps"][0]["address_support"] = {
            "source": ["a1"], "destination": ["a2"],
            "weights": {"street_name": 0.8},  # override street, keep city
        }
        expanded = _apply_step_defaults(copy.deepcopy(r))
        weights = expanded["steps"][0]["address_support"]["weights"]
        assert weights["street_name"] == 0.8  # overridden
        assert weights["city"] == 0.2  # inherited from defaults

    def test_no_defaults_is_noop(self):
        """Recipe without step_defaults is unchanged."""
        from src.recipe import _apply_step_defaults
        r = _make()
        original = copy.deepcopy(r)
        expanded = _apply_step_defaults(r)
        assert expanded["steps"] == original["steps"]

    def test_schema_allows_step_defaults(self):
        """step_defaults should not produce schema warnings."""
        r = _make()
        r["step_defaults"] = {
            "address_support": {
                "source": ["a1"], "destination": ["a2"],
            }
        }
        # validate_recipe sees step_defaults before expansion -- should not warn
        warnings = validate_recipe(r)
        assert not any("step_defaults" in w for w in warnings)

    def test_exclude_on_step_dest_warns(self):
        r = _make()
        r["populations"]["dest_pop"] = {"source": "src", "filter": []}
        r["steps"][0]["destination"] = "dest_pop"
        r["populations"]["dest_pop"]["action"] = "exclude"
        warnings = validate_recipe(r)
        assert any("action: exclude" in w and "destination" in w for w in warnings)


# ---------------------------------------------------------------------------
# Output placement rules (ADR-003)
# ---------------------------------------------------------------------------

def _make_multi():
    """Minimal valid multi-phase recipe."""
    return {
        "name": "Test Multi",
        "sources": {"src": {"file": "test.csv"}},
        "phases": [
            {
                "name": "Phase 1",
                "populations": {
                    "pop1": {"source": "src", "filter": []},
                    "pop2": {"source": "src", "filter": []},
                },
                "steps": [
                    {
                        "name": "step1",
                        "source": "pop1",
                        "destination": "pop2",
                        "match_fields": [
                            {
                                "source": "name",
                                "destination": "name",
                                "method": "exact",
                                "tiers": ["raw"],
                            }
                        ],
                    }
                ],
                "output": {"format": "csv", "summary": "none"},
            }
        ],
    }


class TestOutputPlacement:
    """ADR-003: output placement validation."""

    def test_multi_phase_with_top_level_output_errors(self):
        r = _make_multi()
        r["output"] = {"format": "csv"}
        with pytest.raises(ValueError, match="top-level"):
            validate_recipe(r)

    def test_multi_phase_no_output_on_any_phase_errors(self):
        r = _make_multi()
        del r["phases"][0]["output"]
        with pytest.raises(ValueError, match="no output"):
            validate_recipe(r)

    def test_single_phase_no_output_errors(self):
        r = _make()
        del r["output"]
        with pytest.raises(ValueError):
            validate_recipe(r)

    def test_multi_phase_valid_per_phase_output(self):
        r = _make_multi()
        warnings = validate_recipe(r)
        # No output-related warnings
        assert not any("output" in w.lower() for w in warnings)

    def test_single_phase_valid_top_level_output(self):
        r = _make()
        warnings = validate_recipe(r)
        assert not any("output" in w.lower() for w in warnings)

    def test_summary_field_accepted(self):
        r = _make()
        r["output"]["summary"] = "md"
        warnings = validate_recipe(r)
        assert not any("summary" in w for w in warnings)

    def test_summary_array_accepted(self):
        r = _make()
        r["output"]["summary"] = ["md", "xlsx"]
        warnings = validate_recipe(r)
        assert not any("summary" in w for w in warnings)
