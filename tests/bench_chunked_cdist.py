"""Benchmark: chunked cdist correctness and memory scaling.

Usage: python tests/bench_chunked_cdist.py
"""

import os
import sys
import time
import tracemalloc

import numpy as np
import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from matching import _CDIST_CHUNK_SIZE, match_names_fuzzy


def _generate_data(n_source: int, n_dest: int, overlap_pct: float = 0.3):
    """Generate synthetic source and destination DataFrames.

    overlap_pct controls what fraction of source names appear in dest
    (with slight variation for fuzzy matching).
    """
    rng = np.random.default_rng(42)

    # Base names for destination
    prefixes = ["ACME", "GLOBEX", "INITECH", "UMBRELLA", "WAYSTAR",
                "STARK", "WAYNE", "OSCORP", "LEXCORP", "CYBERDYNE",
                "SOYLENT", "TYRELL", "WEYLAND", "APERTURE", "MASSIVE"]
    suffixes = ["CORP", "INC", "LLC", "LTD", "PTE", "AG", "GMBH",
                "SA", "NV", "PLC", "CO", "GROUP", "INTL", "GLOBAL"]
    middles = ["INDUSTRIES", "TECHNOLOGIES", "SOLUTIONS", "SYSTEMS",
               "HOLDINGS", "PARTNERS", "VENTURES", "DYNAMICS", "NETWORK"]

    def _random_name(i):
        p = prefixes[i % len(prefixes)]
        m = middles[i % len(middles)]
        s = suffixes[i % len(suffixes)]
        return f"{p} {m} {s} {i}"

    dest_names = [_random_name(i) for i in range(n_dest)]

    # Source: some overlap (exact or near-match), rest random
    n_overlap = int(n_source * overlap_pct)
    overlap_idxs = rng.choice(n_dest, size=min(n_overlap, n_dest), replace=False)

    source_names = []
    for i in range(n_source):
        if i < n_overlap:
            # Near-match: take dest name, maybe add typo
            base = dest_names[overlap_idxs[i % len(overlap_idxs)]]
            if rng.random() > 0.5:
                # Add slight variation
                base = base.replace("CORP", "CORPORATION")
            source_names.append(base)
        else:
            source_names.append(f"RANDOM VENDOR {i} ENTERPRISES")

    source_df = pl.DataFrame({
        "vendor_id": [f"V{i:06d}" for i in range(n_source)],
        "vendor_name": source_names,
    })
    dest_df = pl.DataFrame({
        "entity_id": [f"E{i:06d}" for i in range(n_dest)],
        "entity_name": dest_names,
    })
    return source_df, dest_df


def bench_correctness():
    """Verify chunked results match full-matrix results at small scale."""
    import matching

    print("=" * 60)
    print("CORRECTNESS: chunked vs full matrix (small scale)")
    print("=" * 60)

    source_df, dest_df = _generate_data(50, 200)

    # Run with chunk size = 1 (extreme chunking)
    matching._CDIST_CHUNK_SIZE = 1
    result_chunked_1 = match_names_fuzzy(
        source_df, dest_df, "vendor_name", "entity_name",
        tiers=["raw"], threshold=70, scorer="token_sort_ratio",
    )

    # Run with chunk size = 10
    matching._CDIST_CHUNK_SIZE = 10
    result_chunked_10 = match_names_fuzzy(
        source_df, dest_df, "vendor_name", "entity_name",
        tiers=["raw"], threshold=70, scorer="token_sort_ratio",
    )

    # Run with chunk size larger than source (no chunking)
    matching._CDIST_CHUNK_SIZE = 10000
    result_full = match_names_fuzzy(
        source_df, dest_df, "vendor_name", "entity_name",
        tiers=["raw"], threshold=70, scorer="token_sort_ratio",
    )

    # Reset
    matching._CDIST_CHUNK_SIZE = _CDIST_CHUNK_SIZE

    # Compare
    def _sort_df(df):
        if df.height == 0:
            return df
        return df.sort("vendor_id")

    r1 = _sort_df(result_chunked_1)
    r10 = _sort_df(result_chunked_10)
    rf = _sort_df(result_full)

    assert r1.height == rf.height, f"chunk=1 rows {r1.height} != full {rf.height}"
    assert r10.height == rf.height, f"chunk=10 rows {r10.height} != full {rf.height}"

    # Compare scores (float tolerance)
    scores_1 = r1["name_score"].to_numpy()
    scores_10 = r10["name_score"].to_numpy()
    scores_f = rf["name_score"].to_numpy()
    assert np.allclose(scores_1, scores_f, atol=0.01), "chunk=1 scores differ"
    assert np.allclose(scores_10, scores_f, atol=0.01), "chunk=10 scores differ"

    # Compare matched vendor_ids
    assert r1["vendor_id"].to_list() == rf["vendor_id"].to_list(), "chunk=1 vendor_ids differ"
    assert r10["vendor_id"].to_list() == rf["vendor_id"].to_list(), "chunk=10 vendor_ids differ"

    print(f"  Matched rows: {rf.height}/{source_df.height}")
    print(f"  chunk=1:     PASS (identical)")
    print(f"  chunk=10:    PASS (identical)")
    print(f"  chunk=full:  PASS (baseline)")
    print()


def bench_memory_scaling():
    """Measure peak memory at increasing source sizes."""
    import matching

    print("=" * 60)
    print("MEMORY: peak RSS at increasing source sizes")
    print("=" * 60)

    dest_size = 50000  # Fixed destination size
    source_sizes = [100, 500, 1000, 2000, 5000]
    matching._CDIST_CHUNK_SIZE = 500  # Fixed chunk size

    _, dest_df = _generate_data(1, dest_size)

    for n_src in source_sizes:
        source_df, _ = _generate_data(n_src, dest_size)

        tracemalloc.start()
        t0 = time.perf_counter()

        result = match_names_fuzzy(
            source_df, dest_df, "vendor_name", "entity_name",
            tiers=["raw"], threshold=70, scorer="token_sort_ratio",
        )

        elapsed = time.perf_counter() - t0
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak / 1024 / 1024
        # Theoretical full matrix size
        theory_mb = (n_src * dest_size * 4) / 1024 / 1024
        # Chunk matrix size
        chunk_mb = (min(500, n_src) * dest_size * 4) / 1024 / 1024

        print(f"  {n_src:>5d} x {dest_size:>6d}: "
              f"{elapsed:5.1f}s | "
              f"peak {peak_mb:7.1f} MB | "
              f"theory {theory_mb:7.1f} MB | "
              f"chunk ceiling {chunk_mb:6.1f} MB | "
              f"matched {result.height}")

    matching._CDIST_CHUNK_SIZE = _CDIST_CHUNK_SIZE
    print()


def bench_timing_scaling():
    """Verify timing scales roughly linearly with source size."""
    import matching

    print("=" * 60)
    print("TIMING: scaling with source size (fixed dest=10000)")
    print("=" * 60)

    dest_size = 10000
    source_sizes = [100, 200, 500, 1000, 2000]
    matching._CDIST_CHUNK_SIZE = 500

    _, dest_df = _generate_data(1, dest_size)

    timings = []
    for n_src in source_sizes:
        source_df, _ = _generate_data(n_src, dest_size)

        t0 = time.perf_counter()
        result = match_names_fuzzy(
            source_df, dest_df, "vendor_name", "entity_name",
            tiers=["raw"], threshold=70, scorer="token_sort_ratio",
        )
        elapsed = time.perf_counter() - t0
        timings.append((n_src, elapsed))
        print(f"  {n_src:>5d} x {dest_size}: {elapsed:5.2f}s (matched {result.height})")

    # Check roughly linear: 10x source should be ~10x time (within 3x tolerance)
    if len(timings) >= 2:
        ratio_src = timings[-1][0] / timings[0][0]
        ratio_time = timings[-1][1] / max(timings[0][1], 0.001)
        print(f"\n  Source grew {ratio_src:.0f}x, time grew {ratio_time:.1f}x "
              f"({'~linear' if ratio_time < ratio_src * 3 else 'SUPERLINEAR'})")

    matching._CDIST_CHUNK_SIZE = _CDIST_CHUNK_SIZE
    print()


def bench_gleif_scale():
    """Simulate GLEIF-scale matching: 12k source x 100k dest (scaled down)."""
    import matching

    print("=" * 60)
    print("GLEIF SIMULATION: 12k source x 100k dest")
    print("=" * 60)

    matching._CDIST_CHUNK_SIZE = 1000
    source_df, dest_df = _generate_data(12000, 100000, overlap_pct=0.1)

    tracemalloc.start()
    t0 = time.perf_counter()

    result = match_names_fuzzy(
        source_df, dest_df, "vendor_name", "entity_name",
        tiers=["raw", "clean"], threshold=70, scorer="token_sort_ratio",
    )

    elapsed = time.perf_counter() - t0
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mb = peak / 1024 / 1024
    theory_mb = (12000 * 100000 * 4) / 1024 / 1024  # Full matrix
    chunk_mb = (1000 * 100000 * 4) / 1024 / 1024    # Per-chunk

    print(f"  Size: 12,000 x 100,000")
    print(f"  Time: {elapsed:.1f}s")
    print(f"  Peak memory: {peak_mb:.1f} MB")
    print(f"  Full matrix would be: {theory_mb:.0f} MB ({theory_mb/1024:.1f} GB)")
    print(f"  Chunk ceiling: {chunk_mb:.0f} MB")
    print(f"  Memory savings: {theory_mb/max(peak_mb, 1):.1f}x")
    print(f"  Matched: {result.height}/{source_df.height}")
    print(f"  Tiers: raw + clean")

    matching._CDIST_CHUNK_SIZE = _CDIST_CHUNK_SIZE
    print()


def bench_existing_recipes():
    """Run actual test recipes to verify nothing is broken."""
    print("=" * 60)
    print("E2E: existing test recipes")
    print("=" * 60)

    import subprocess
    recipes = [
        ("gleif_parent_lookup_test", "config/recipes/gleif_parent_lookup_test.yaml"),
        ("same_pop_example", "config/recipes/same_pop_example.yaml"),
    ]

    base = os.path.join(os.path.dirname(__file__), "..")
    for name, recipe in recipes:
        t0 = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "src", "--recipe", recipe],
            capture_output=True, text=True, cwd=base,
        )
        elapsed = time.perf_counter() - t0
        status = "PASS" if result.returncode == 0 else "FAIL"
        # Extract matched count from output
        for line in result.stdout.splitlines():
            if "Matched:" in line:
                print(f"  {name}: {status} ({elapsed:.1f}s) -- {line.strip()}")
                break
        else:
            print(f"  {name}: {status} ({elapsed:.1f}s)")
        if result.returncode != 0:
            print(f"    STDERR: {result.stderr[:200]}")
    print()


if __name__ == "__main__":
    print()
    bench_correctness()
    bench_memory_scaling()
    bench_timing_scaling()
    bench_gleif_scale()
    bench_existing_recipes()
    print("All benchmarks complete.")
