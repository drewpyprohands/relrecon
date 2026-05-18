# Multi-Phase Recipes

Multi-phase recipes chain multiple matching passes into a single pipeline. Each phase produces matched output that the next phase can consume as input, and each phase can produce its own output files independently. This enables relational lookups like "find entity, then find parent, then resolve parent name."

## When to use multi-phase

Use multi-phase when your matching problem requires **sequential joins** -- where the output of one match is the input to the next. Common patterns:

- Entity resolution → parent lookup → parent name resolution (GLEIF)
- Vendor → contract → billing entity chains
- Any "match A to B, then match result to C" workflow

For simple source-to-destination matching, use a standard single-phase recipe.

## Recipe structure

A multi-phase recipe replaces the top-level `populations` and `steps` keys with a `phases` list:

```yaml
name: My Multi-Phase Pipeline

sources:
  vendors:
    file: vendors.csv
  reference_db:
    loader: http
    url: https://example.com/data.csv
    cache_ttl: "7d"

phases:
  - name: "Phase 1: Match vendors to reference"
    populations:
      vendor_pop:
        source: vendors
        record_key: vendor_id
      ref_pop:
        source: reference_db
        record_key: ref_id
    steps:
      - name: Exact name match
        source: vendor_pop
        destination: ref_pop
        match_fields:
          - source: vendor_name
            destination: ref_name
            method: exact
            tiers: [raw, clean, normalized]

    output:
      format: csv
      summary: md

  - name: "Phase 2: Resolve parent from matched results"
    populations:
      matched_from_phase1:
        source: _previous_matched    # <-- output from Phase 1
        record_key: ref_id
      parent_db:
        source: parent_reference
        record_key: parent_id
    steps:
      - name: Match ref to parent
        source: matched_from_phase1
        destination: parent_db
        match_fields:
          - source: ref_id
            destination: child_ref_id
            method: exact
            tiers: [raw]
    output:
      format: csv
      summary: [md, xlsx]
```

Key differences from single-phase:
- `populations` and `steps` live inside each phase, not at the top level
- `sources` remains top-level (shared across all phases)
- `output` is per-phase (not top-level) -- each phase defines its own output independently
- At least one phase must have an `output` block; phases without one still run but produce no files

## `_previous_matched`

The virtual source `_previous_matched` gives a phase access to the matched rows from the prior phase. It contains all columns from the matched output -- both source and destination fields, including any `_dst` suffixed columns from the join.

Rules:
- Only available in Phase 2 and later (Phase 1 has no prior output)
- Contains the matched DataFrame from the immediately preceding phase
- Unmatched rows from earlier phases are **not** lost -- they appear in the final output with null columns for the phases they missed (partial-match recovery)

## Per-phase output (ADR-003)

Each phase can independently produce data files and summaries. The `output` block lives inside each phase:

```yaml
phases:
  - name: "Phase 1: Match vendors"
    # ... populations, steps ...
    output:
      format: csv              # csv or xlsx -- raw data, no formatting
      summary: md              # none, md, xlsx, or [md, xlsx]

  - name: "Phase 2: Resolve parents"
    # ... populations, steps ...
    output:
      format: xlsx
      summary: none            # raw xlsx data only

  - name: "Phase 3: Final enrichment"
    # ... populations, steps ...
    output:
      format: csv
      summary: [md, xlsx]      # both markdown and formatted Excel report
      columns:
        matched:
          - field: vendor_id
            header: Vendor ID
          - field: parent_legal_name
            header: Parent Name
```

**Output rules:**
- `format`: `csv` or `xlsx` -- always raw data (single "Data" tab for xlsx)
- `summary`: controls report generation alongside the data file
  - `md` -- markdown summary (`_summary.md`)
  - `xlsx` -- formatted Excel report with Summary + Matched + Analysis tabs (`_report.xlsx`)
  - `[md, xlsx]` -- both
  - `none` -- no summary
- `summary` must be explicit. Omitting it produces no summary (and a warning if format is xlsx)
- Single-phase recipes use top-level `output` (backward compatible)
- Multi-phase recipes must NOT have top-level `output` -- use per-phase instead

See `docs/adr/003-per-phase-output-model.md` for the full design rationale.

## Column collision handling (`_dst` / `_dst2` / `_dst3`)

When two DataFrames are joined, overlapping column names get a `_dst` suffix on the destination side. In multi-phase pipelines, subsequent joins against the same source increment the suffix: `_dst`, `_dst2`, `_dst3`, etc.

```
Phase 1: vendor_name + Entity.LegalName → Entity.LegalName_dst
Phase 3: joins against GLEIF again → Entity.LegalName_dst2
```

The recommended approach is to use `inherit` to rename destination columns to meaningful names, avoiding `_dst` references in your output config entirely:

```yaml
steps:
  - name: Resolve parent name
    source: matched_with_parent
    destination: gleif_entities
    match_fields:
      - source: Relationship.EndNode.NodeID
        destination: LEI
        method: exact
        tiers: [raw]
    inherit:
      - source: Entity.LegalName
        as: parent_legal_name       # clean name, no _dst needed
      - source: Entity.LegalAddress.Country
        as: parent_country
```

With `inherit`, your output columns reference `parent_legal_name` instead of `Entity.LegalName_dst2`. This is clearer and doesn't break if phases are reordered.

In single-phase recipes, `_dst` columns can still be referenced directly in `output.columns` using the coalesce syntax (`fields: [Vendor Name, l3_fmly_nm_dst]`).

## Partial-match recovery

Not every record survives all phases. A vendor might match an LEI in Phase 1 but have no parent relationship in Phase 2. Multi-phase handles this by preserving partial matches:

- Records matched in Phase 1 but unmatched in Phase 2 still appear in the final output
- Columns from later phases are null for those rows
- The `match_step` and `match_tier` fields reflect the last successful match

This prevents data loss in pipelines where later phases are enrichment rather than hard requirements.

## Dry-run support

`--dry-run` works with multi-phase recipes. It validates:

- All sources load correctly (uses cache if available, downloads if not)
- Population filters reference valid fields
- Step field references exist in the source data
- Output placement rules (per-phase output required, no top-level)
- Deprecation warnings (`tabs`, missing `summary` key)

Limitations: fields that come from `_previous_matched` (like inherited LEI columns) can't be validated at dry-run time since that data only exists at runtime. These show as ⏭️ (runtime) in dry-run output -- this is expected, not an error.

## Example

See `config/recipes/gleif_parent_lookup.yaml` for a complete 3-phase pipeline:

1. Match vendor names → GLEIF Level 1 (find LEI)
2. Match LEI → GLEIF Level 2 RR (find parent relationship)
3. Match parent LEI → GLEIF Level 1 (resolve parent name)

The test variant (`gleif_parent_lookup_test.yaml`) runs against small test data and outputs Phase 3 only (CSV + md + xlsx report). The example variant (`gleif_phased_output_example.yaml`) demonstrates mixed output modes across all three phases.

See `docs/gleif_parent_matching_design_note.md` for the domain logic behind this pipeline.
