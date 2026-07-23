"""Regression coverage for retaining same-step ``all_matches`` candidates."""

import sys
from itertools import chain, permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import polars as pl

import matching
from matching import match_names_exact, match_names_fuzzy, run_matching_step, run_pipeline


def _recipe(tmp_path, source_rows, destination_rows, match_field):
    """Create a minimal single-phase recipe backed by temporary CSV inputs."""
    source_path = tmp_path / "source.csv"
    destination_path = tmp_path / "destination.csv"
    pl.DataFrame(source_rows).write_csv(source_path)
    pl.DataFrame(destination_rows).write_csv(destination_path)
    return {
        "sources": {
            "source_data": {"file": source_path.name},
            "destination_data": {"file": destination_path.name},
        },
        "populations": {
            "source": {"source": "source_data", "record_key": "source_id", "filter": []},
            "destination": {"source": "destination_data", "filter": []},
        },
        "steps": [{
            "name": "candidate step",
            "source": "source",
            "destination": "destination",
            "match_fields": [match_field],
        }],
        "output": {"match_mode": "all_matches"},
    }


def test_all_matches_retains_duplicate_exact_destinations(tmp_path):
    """Exact all_matches must retain every destination in the first tier."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["source-1"], "name": ["Acme"]},
        {"destination_id": ["dest-1", "dest-2"], "name": ["Acme", "Acme"]},
        {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]},
    )

    result = run_pipeline(recipe, base_dir=str(tmp_path))

    assert result["matched"].height == 2
    assert result["matched"]["destination_id"].to_list() == ["dest-1", "dest-2"]


def test_all_matches_retains_every_fuzzy_destination_above_threshold(tmp_path):
    """Fuzzy all_matches must retain every above-threshold destination."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["source-1"], "name": ["Acme Incorporated"]},
        {
            "destination_id": ["dest-1", "dest-2"],
            "name": ["Acme Incorporated LLC", "Acme Incorporated Ltd"],
        },
        {
            "source": "name",
            "destination": "name",
            "method": "fuzzy",
            "tiers": ["raw"],
            "threshold": 80,
        },
    )

    result = run_pipeline(recipe, base_dir=str(tmp_path))

    assert result["matched"].height == 2
    assert result["matched"]["destination_id"].to_list() == ["dest-1", "dest-2"]


def test_all_matches_preserves_first_tier_precedence(tmp_path):
    """Candidates found only in later tiers cannot supplement an earlier tier."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["source-1"], "name": ["Acme, Inc."]},
        {
            "destination_id": ["raw", "clean-only"],
            "name": ["Acme, Inc.", "acme inc"],
        },
        {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw", "clean"]},
    )

    result = run_pipeline(recipe, base_dir=str(tmp_path))["matched"]

    assert result["destination_id"].to_list() == ["raw"]
    assert result["match_tier"].to_list() == ["raw"]


def test_all_ordered_tier_combinations_preserve_first_tier():
    """Every ordered non-empty raw/clean/normalized tier set is supported."""
    tier_sets = list(chain.from_iterable(permutations(["raw", "clean", "normalized"], size)
                                        for size in range(1, 4)))
    source = pl.DataFrame({"source_id": ["source-1"], "name": ["Acme, Inc."]})
    destination = pl.DataFrame({"destination_id": ["a", "b"], "name": ["Acme, Inc.", "Acme, Inc."]})

    for tiers in tier_sets:
        for matcher in [match_names_exact, match_names_fuzzy]:
            kwargs = {"threshold": 100} if matcher is match_names_fuzzy else {}
            result = matcher(
                source, destination, "name", "name", tiers=list(tiers),
                dedup_field="source_id", match_mode="all_matches", **kwargs,
            )
            assert result.height == 2
            assert result["match_tier"].to_list() == [tiers[0], tiers[0]]


def test_all_matches_supports_every_fuzzy_scorer():
    """All supported fuzzy scorers retain every above-threshold candidate."""
    source = pl.DataFrame({"source_id": ["source-1"], "name": ["Acme Incorporated"]})
    destination = pl.DataFrame({
        "destination_id": ["a", "b"],
        "name": ["Acme Incorporated LLC", "Acme Incorporated Ltd"],
    })

    for scorer in ["token_sort_ratio", "token_set_ratio", "ratio", "partial_ratio", "WRatio"]:
        all_matches = match_names_fuzzy(
            source, destination, "name", "name", tiers=["raw"], threshold=80,
            scorer=scorer, dedup_field="source_id", match_mode="all_matches",
        )
        best_match = match_names_fuzzy(
            source, destination, "name", "name", tiers=["raw"], threshold=80,
            scorer=scorer, dedup_field="source_id", match_mode="best_match",
        )
        assert all_matches["destination_id"].to_list() == ["a", "b"]
        assert best_match.height == 1


def test_all_matches_fuzzy_retains_candidates_across_chunks(monkeypatch):
    """Fuzzy all_matches collects threshold hits from every bounded chunk."""
    monkeypatch.setattr(matching, "_CDIST_CHUNK_SIZE", 1)
    source = pl.DataFrame({"source_id": ["one", "two", "three"], "name": ["Acme", "Acme", "Acme"]})
    destination = pl.DataFrame({"destination_id": ["a", "b"], "name": ["Acme", "Acme"]})

    result = match_names_fuzzy(
        source, destination, "name", "name", tiers=["raw"], threshold=100,
        dedup_field="source_id", match_mode="all_matches",
    )

    assert result.select("source_id", "destination_id").rows() == [
        ("one", "a"), ("one", "b"), ("two", "a"),
        ("two", "b"), ("three", "a"), ("three", "b"),
    ]


def test_all_matches_ordering_is_deterministic_for_exact_and_equal_fuzzy_scores():
    """Candidate order is stable even when exact or fuzzy scores are equal."""
    source = pl.DataFrame({"source_id": ["one", "two"], "name": ["Acme", "Acme"]})
    destination = pl.DataFrame({"destination_id": ["b", "a"], "name": ["Acme", "Acme"]})

    exact = match_names_exact(
        source, destination, "name", "name", tiers=["raw"],
        dedup_field="source_id", match_mode="all_matches",
    )
    fuzzy = match_names_fuzzy(
        source, destination, "name", "name", tiers=["raw"], threshold=100,
        dedup_field="source_id", match_mode="all_matches",
    )

    expected = [("one", "b"), ("one", "a"), ("two", "b"), ("two", "a")]
    assert exact.select("source_id", "destination_id").rows() == expected
    assert fuzzy.select("source_id", "destination_id").rows() == expected


def test_all_matches_applies_address_gates_to_each_candidate(tmp_path):
    """Address threshold and required-street gates filter candidates individually."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["source-1"], "name": ["Acme"], "address": ["1 Main Street"]},
        {
            "destination_id": ["passes", "fails"],
            "name": ["Acme", "Acme"],
            "address": ["1 Main Street", "99 Other Road"],
        },
        {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]},
    )
    recipe["steps"][0]["address_support"] = {
        "source": ["address"], "destination": ["address"], "threshold": 90,
        "require_street_match": True, "parser": "default",
    }

    result = run_pipeline(recipe, base_dir=str(tmp_path))["matched"]

    assert result["destination_id"].to_list() == ["passes"]


def test_all_matches_preserves_filters_date_gate_inheritance_and_exclusions(tmp_path):
    """Candidate retention composes with filters, legacy date_gate, and inheritance."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["excluded", "included"], "name": ["Acme", "Acme"], "allowed": [False, True]},
        {
            "destination_id": ["old", "one", "two"],
            "name": ["Acme", "Acme", "Acme"],
            "active": [True, True, True],
            "updated": ["2000-01-01", "2099-01-01", "2099-01-01"],
            "parent": ["old-parent", "parent-1", "parent-2"],
        },
        {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]},
    )
    recipe["steps"][0].update({
        "filters": [{"field": "allowed", "op": "eq", "value": True, "applies_to": "source"}],
        "date_gate": {"field": "updated", "max_age_years": 2, "applies_to": "destination"},
        "exclude": {"field": "source_id", "values": ["excluded"]},
        "inherit": [{"source": "parent", "as": "parent_id"}],
    })

    result = run_pipeline(recipe, base_dir=str(tmp_path))["matched"]

    assert result.select("source_id", "destination_id", "parent_id").rows() == [
        ("included", "one", "parent-1"), ("included", "two", "parent-2"),
    ]


def test_tie_breakers_do_not_discard_all_matches_candidates(tmp_path):
    """Tie-breakers retain best_match selection while all_matches keeps both."""
    recipe = _recipe(
        tmp_path,
        {"source_id": ["source-1"], "name": ["Acme"]},
        {"destination_id": ["later", "first"], "name": ["Acme", "Acme"], "rank": ["B2", "B1"]},
        {"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]},
    )
    recipe["output"]["tie_breaker"] = {"column": "rank", "order": "asc"}

    all_result = run_pipeline(recipe, base_dir=str(tmp_path))["matched"]
    recipe["output"]["match_mode"] = "best_match"
    best_result = run_pipeline(recipe, base_dir=str(tmp_path))["matched"]

    assert all_result["destination_id"].to_list() == ["first", "later"]
    assert best_result["destination_id"].to_list() == ["first"]


def test_all_matches_excludes_same_population_self_matches():
    """Same-population all_matches retains cross-candidates but never self-matches."""
    population = pl.DataFrame({"source_id": ["one", "two"], "name": ["Acme", "Acme"]})
    step = {
        "name": "self step", "_same_pop": True,
        "match_fields": [{"source": "name", "destination": "name", "method": "exact", "tiers": ["raw"]}],
    }

    result = run_matching_step(
        population, population, step, dedup_field="source_id", match_mode="all_matches",
    )

    assert result.select("source_id", "source_id_dst").rows() == [("one", "two"), ("two", "one")]

    step["match_fields"][0].update({"method": "fuzzy", "threshold": 0})
    fuzzy_result = run_matching_step(
        population, population, step, dedup_field="source_id", match_mode="all_matches",
    )

    assert fuzzy_result.select("source_id", "source_id_dst").rows() == [("one", "two"), ("two", "one")]


def test_best_match_behavior_is_unchanged():
    """The default and explicit best_match paths retain one best candidate."""
    source = pl.DataFrame({"source_id": ["source-1"], "name": ["Acme"]})
    destination = pl.DataFrame({"destination_id": ["a", "b"], "name": ["Acme", "Acme"]})

    for matcher in [match_names_exact, match_names_fuzzy]:
        kwargs = {"threshold": 100} if matcher is match_names_fuzzy else {}
        default = matcher(source, destination, "name", "name", tiers=["raw"], dedup_field="source_id", **kwargs)
        explicit = matcher(
            source, destination, "name", "name", tiers=["raw"], dedup_field="source_id",
            match_mode="best_match", **kwargs,
        )
        assert default.rows() == explicit.rows()
        assert default.height == 1
