"""Tests for multi-phase pipeline (issue #164)."""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from matching import run_pipeline


@pytest.fixture
def entities_csv(tmp_path):
    """Reference entities with LEI and parent info."""
    df = pl.DataFrame({
        "LEI": ["LEI001", "LEI002", "LEI003", "LEI004", "LEI005"],
        "entity_name": [
            "ACME CORPORATION",
            "ACME HOLDINGS LTD",
            "GLOBEX INC",
            "INITECH LLC",
            "UMBRELLA CORP",
        ],
        "country": ["US", "GB", "US", "US", "JP"],
    })
    path = tmp_path / "entities.csv"
    df.write_csv(path)
    return path


@pytest.fixture
def relationships_csv(tmp_path):
    """Parent relationships: child_lei -> parent_lei."""
    df = pl.DataFrame({
        "child_lei": ["LEI001", "LEI004"],
        "parent_lei": ["LEI002", "LEI005"],
        "rel_type": ["IS_DIRECTLY_CONSOLIDATED_BY", "IS_DIRECTLY_CONSOLIDATED_BY"],
    })
    path = tmp_path / "relationships.csv"
    df.write_csv(path)
    return path


@pytest.fixture
def input_csv(tmp_path):
    """Input companies to look up."""
    df = pl.DataFrame({
        "id": ["T1", "T2", "T3"],
        "name": ["Acme Corporation", "Globex Inc", "FakeCompany XYZ"],
    })
    path = tmp_path / "input.csv"
    df.write_csv(path)
    return path


class TestMultiPhaseBasic:
    def test_two_phase_pipeline(self, entities_csv, relationships_csv, input_csv, tmp_path):
        """Phase 1 matches names, phase 2 resolves parents."""
        recipe = {
            "name": "Multi-phase test",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "rels": {"file": str(relationships_csv)},
                "input": {"file": str(input_csv)},
            },
            "phases": [
                {
                    "name": "Match to entities",
                    "populations": {
                        "lookup": {
                            "source": "input",
                            "record_key": "id",
                            "filter": [],
                        },
                    },
                    "steps": [
                        {
                            "name": "Exact match",
                            "source": "lookup",
                            "destination": "entities",
                            "match_fields": [{
                                "source": "name",
                                "destination": "entity_name",
                                "method": "exact",
                                "tiers": ["clean"],
                            }],
                            "inherit": [
                                {"source": "LEI", "as": "matched_lei"},
                                {"source": "entity_name", "as": "matched_name"},
                            ],
                        },
                    ],
                },
                {
                    "name": "Resolve parents",
                    "populations": {
                        "matched_entities": {
                            "source": "_previous_matched",
                            "record_key": "id",
                            "filter": [],
                        },
                    },
                    "steps": [
                        {
                            "name": "Find parent",
                            "source": "matched_entities",
                            "destination": "rels",
                            "match_fields": [{
                                "source": "matched_lei",
                                "destination": "child_lei",
                                "method": "exact",
                                "tiers": ["raw"],
                            }],
                            "inherit": [
                                {"source": "parent_lei", "as": "parent_lei"},
                            ],
                        },
                    ],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))

        # Phase 1: 2 of 3 matched (FakeCompany won't match)
        # Phase 2: of those 2, only ACME has a parent (LEI001 -> LEI002)
        assert result["stats"]["total_source"] == 3
        assert result["stats"]["unmatched_count"] == 1  # FakeCompany

        matched = result["matched"]
        # Phase 2 output: ACME matched fully (has parent), BetaCorp is a
        # partial match (matched Phase 1 but no parent in Phase 2)
        assert matched.height == 2
        # First record is the full match (went through all phases)
        full_matches = matched.filter(pl.col("parent_lei").is_not_null())
        assert full_matches.height == 1
        assert full_matches["parent_lei"][0] == "LEI002"
        # Partial match has null parent columns
        partial = matched.filter(pl.col("parent_lei").is_null())
        assert partial.height == 1

        # Check phase stats
        assert "phases" in result
        assert len(result["phases"]) == 2
        assert result["phases"][0]["name"] == "Match to entities"
        assert result["phases"][0]["matched_count"] == 2
        assert result["phases"][1]["name"] == "Resolve parents"
        assert result["phases"][1]["matched_count"] == 1

    def test_single_phase_backward_compat(self, entities_csv, input_csv, tmp_path):
        """Classic recipe without phases still works."""
        recipe = {
            "name": "Classic test",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "input": {"file": str(input_csv)},
            },
            "populations": {
                "lookup": {
                    "source": "input",
                    "record_key": "id",
                    "filter": [],
                },
            },
            "steps": [
                {
                    "name": "Exact match",
                    "source": "lookup",
                    "destination": "entities",
                    "match_fields": [{
                        "source": "name",
                        "destination": "entity_name",
                        "method": "exact",
                        "tiers": ["clean"],
                    }],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))
        assert result["stats"]["matched_count"] == 2
        assert result["stats"]["unmatched_count"] == 1
        assert "phases" not in result

    def test_three_phase_pipeline(self, entities_csv, relationships_csv, input_csv, tmp_path):
        """Three phases: match -> resolve parent -> resolve parent name."""
        recipe = {
            "name": "Three-phase test",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "rels": {"file": str(relationships_csv)},
                "input": {"file": str(input_csv)},
            },
            "phases": [
                {
                    "name": "Match names",
                    "populations": {
                        "lookup": {
                            "source": "input",
                            "record_key": "id",
                            "filter": [],
                        },
                    },
                    "steps": [{
                        "name": "Exact",
                        "source": "lookup",
                        "destination": "entities",
                        "match_fields": [{
                            "source": "name",
                            "destination": "entity_name",
                            "method": "exact",
                            "tiers": ["clean"],
                        }],
                        "inherit": [
                            {"source": "LEI", "as": "matched_lei"},
                        ],
                    }],
                },
                {
                    "name": "Get parent LEI",
                    "populations": {
                        "phase2": {
                            "source": "_previous_matched",
                            "record_key": "id",
                            "filter": [],
                        },
                    },
                    "steps": [{
                        "name": "Find parent",
                        "source": "phase2",
                        "destination": "rels",
                        "match_fields": [{
                            "source": "matched_lei",
                            "destination": "child_lei",
                            "method": "exact",
                            "tiers": ["raw"],
                        }],
                        "inherit": [
                            {"source": "parent_lei", "as": "parent_lei"},
                        ],
                    }],
                },
                {
                    "name": "Resolve parent name",
                    "populations": {
                        "phase3": {
                            "source": "_previous_matched",
                            "record_key": "id",
                            "filter": [],
                        },
                    },
                    "steps": [{
                        "name": "Get name",
                        "source": "phase3",
                        "destination": "entities",
                        "match_fields": [{
                            "source": "parent_lei",
                            "destination": "LEI",
                            "method": "exact",
                            "tiers": ["raw"],
                        }],
                        "inherit": [
                            {"source": "entity_name", "as": "parent_name"},
                            {"source": "country", "as": "parent_country"},
                        ],
                    }],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))

        assert len(result["phases"]) == 3
        matched = result["matched"]
        # ACME has full parent chain; BetaCorp is a partial match (Phase 1 only)
        assert matched.height == 2

        # Full chain match
        full = matched.filter(pl.col("parent_name").is_not_null())
        assert full.height == 1
        row = full.row(0, named=True)
        assert row["parent_name"] == "ACME HOLDINGS LTD"
        assert row["parent_country"] == "GB"
        assert row["parent_lei"] == "LEI002"

        # Partial match (matched entity but no parent)
        partial = matched.filter(pl.col("parent_name").is_null())
        assert partial.height == 1


class TestMultiPhaseEdgeCases:
    def test_empty_previous_matched(self, entities_csv, relationships_csv, tmp_path):
        """Phase 2 gets empty _previous_matched when phase 1 matches nothing."""
        input_path = tmp_path / "no_match.csv"
        pl.DataFrame({"id": ["X1"], "name": ["ZZZZZ NONEXISTENT"]}).write_csv(input_path)

        recipe = {
            "name": "No match test",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "rels": {"file": str(relationships_csv)},
                "input": {"file": str(input_path)},
            },
            "phases": [
                {
                    "name": "Phase 1",
                    "populations": {
                        "lookup": {"source": "input", "record_key": "id", "filter": []},
                    },
                    "steps": [{
                        "name": "Exact",
                        "source": "lookup",
                        "destination": "entities",
                        "match_fields": [{
                            "source": "name",
                            "destination": "entity_name",
                            "method": "exact",
                            "tiers": ["clean"],
                        }],
                        "inherit": [{"source": "LEI", "as": "lei"}],
                    }],
                },
                {
                    "name": "Phase 2",
                    "populations": {
                        "p2": {"source": "_previous_matched", "record_key": "id", "filter": []},
                    },
                    "steps": [{
                        "name": "Resolve",
                        "source": "p2",
                        "destination": "rels",
                        "match_fields": [{
                            "source": "lei",
                            "destination": "child_lei",
                            "method": "exact",
                            "tiers": ["raw"],
                        }],
                    }],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))
        assert result["stats"]["matched_count"] == 0
        assert result["stats"]["unmatched_count"] == 1

    def test_phase_with_multiple_steps(self, entities_csv, input_csv, tmp_path):
        """A single phase can have exact + fuzzy steps."""
        recipe = {
            "name": "Multi-step phase",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "input": {"file": str(input_csv)},
            },
            "phases": [
                {
                    "name": "Match all ways",
                    "populations": {
                        "lookup": {"source": "input", "record_key": "id", "filter": []},
                    },
                    "steps": [
                        {
                            "name": "Exact",
                            "source": "lookup",
                            "destination": "entities",
                            "match_fields": [{
                                "source": "name",
                                "destination": "entity_name",
                                "method": "exact",
                                "tiers": ["clean"],
                            }],
                        },
                        {
                            "name": "Fuzzy",
                            "source": "lookup",
                            "destination": "entities",
                            "match_fields": [{
                                "source": "name",
                                "destination": "entity_name",
                                "method": "fuzzy",
                                "threshold": 70,
                                "tiers": ["clean"],
                            }],
                        },
                    ],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))
        assert result["stats"]["matched_count"] == 2
        assert len(result["phases"]) == 1


class TestMultiPhaseStepDefaults:
    def test_step_defaults_applied_to_phases(self, entities_csv, input_csv, tmp_path):
        """step_defaults should merge into phase steps."""
        recipe = {
            "name": "Defaults test",
            "sources": {
                "entities": {"file": str(entities_csv)},
                "input": {"file": str(input_csv)},
            },
            "step_defaults": {
                "inherit": [
                    {"source": "LEI", "as": "matched_lei"},
                ],
            },
            "phases": [
                {
                    "name": "Match",
                    "populations": {
                        "lookup": {"source": "input", "record_key": "id", "filter": []},
                    },
                    "steps": [
                        {
                            "name": "Exact",
                            "source": "lookup",
                            "destination": "entities",
                            "match_fields": [{
                                "source": "name",
                                "destination": "entity_name",
                                "method": "exact",
                                "tiers": ["clean"],
                            }],
                            # No inherit here -- should come from step_defaults
                        },
                    ],
                },
            ],
            "output": {"format": "xlsx", "match_mode": "best_match"},
        }

        result = run_pipeline(recipe, str(tmp_path))
        matched = result["matched"]
        assert matched.height == 2
        # Verify inherit from step_defaults was applied
        assert "matched_lei" in matched.columns
