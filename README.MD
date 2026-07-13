# **Rel**ational **Recon**ciliation Engine

A config-driven framework for reconciling datasets with broken or unreliable hierarchical relationships. Define a recipe, run the engine, get an auditable Excel report with a markdown summary and Mermaid flow diagram.

## Why This Exists

When entity records get bulk-migrated between systems, relationships often break. Parent IDs get populated with placeholder data, key fields are overwritten with junk and downstream data becomes untrustworthy. You're left with thousands of records that need reconciliation against trusted sources, and no reliable way to automate it.

This framework solves that class of problem. Rather than writing a one-off script for each reconciliation task, the engine is **config-driven**: define your sources, populations, matching steps, and output format in a recipe YAML, and the engine handles normalization, matching, address scoring, and report generation.

The first use case ([L1 Reconciliation](docs/l1-recipe.md)) reconciles ~15k migrated vendor records to trusted parent IDs by matching child-level names across populations. Future reconciliation problems only need a new recipe, not new code

## Quickstart

```bash
# Clone
git clone https://github.com/drewpypro/relrecon.git
cd relrecon

# Install dependencies
pip install -r requirements.txt

# Run the pipeline against included synthetic data
python3 -m src --recipe config/recipes/l1_reconciliation.yaml --data data/

# Output: timestamped xlsx + markdown summary in output/

# Other options
python3 -m src --help                       # See all flags
python3 -m src --recipe ... --dry-run       # Validate recipe without running
python3 -m src --recipe ... --no-libpostal  # Force built-in address tokenizer
python3 -m src --recipe ... --mermaid detailed  # Detailed mermaid diagram in summary

# Run tests
python3 -m pytest tests/ -q
```

> **Note:** libpostal is optional. The engine includes a built-in address tokenizer that works without any external dependencies. For better accuracy on international/complex addresses, see the [libpostal install guide](docs/implementation-plan.md#libpostal-installation).

## How It Works

The engine has seven core modules, all driven by a single recipe config:

| Module | Purpose |
|---|---|
| **recipe.py** | Recipe loader, JSON Schema validation, filter DSL parser, field validation |
| **normalize.py** | 3-tier normalization: Raw (as-is), Clean (lowercase, strip punctuation), Normalized (aliases + stopwords, addresses only) |
| **signal_analysis.py** | Profile datasets to bootstrap stopwords, aliases, and unicode maps before configuring a recipe |
| **address.py** | Two-pass address parsing + scoring with optional libpostal. Merges and cross-compares address fields, weights street name as strongest signal |
| **matching.py** | Core engine: loads populations, applies filters, runs multi-step name matching (exact + fuzzy), scores addresses, resolves multi-match conflicts |
| **report.py** | Excel output with Summary, Matched, Analysis, and Recipe tabs. Recipe-driven column mapping with variant coalescing |
| **recipe_echo.py** | Echoes the resolved recipe into the xlsx Recipe tab and markdown report section. Round-trips to an equivalent recipe |
| **summary.py** | Run summary generator. Plain-English markdown + Excel Summary tab from recipe config + pipeline stats. Includes Mermaid cascade diagram |

### Recipes

A recipe defines **what** to match. The engine handles **how**. Here's the structure:

```yaml
name: L1 Reconciliation
description: Reconcile Pop1 parent IDs to trusted L1 records

sources:
  core_parent:
    file: core_parent_export.csv
  multi_pop:
    file: tp_multi_pop_dataset.csv

populations:
  pop1:                         # Target: records to reconcile
    source: multi_pop
    record_key: vendor_id       # Unique ID for dedup
    filter:
      - field: vendor_id
        op: starts_with
        value: "V7"
  garbage:                      # Exclude: automated migration junk
    source: multi_pop
    filter: [...]
    action: exclude
  pop3:                         # Remainder: everything else
    source: multi_pop
    filter: []

steps:
  - name: Match Pop1 to core_parent
    source: pop1
    destination: core_parent
    match_fields:
      - source: l3_fmly_nm
        destination: Vendor Name
        method: exact           # or: fuzzy (with threshold + scorer)
        tiers: [raw, clean]
    address_support:            # Address scoring as supporting evidence
      source: [hq_addr1, hq_addr2]
      destination: [Address1, Address2]
      threshold: 60
    date_gate:                  # Only match against recent records
      field: Updated
      max_age_years: 2
      applies_to: destination
    inherit:                    # On match, derive the parent (L1)
      - source: Supplier Name
        as: derived_l1_name

output:
  format: xlsx
  match_mode: best_match        # or all_matches for analysis
  columns:                      # Optional: controls report layout
    matched:
      - field: l3_fmly_nm
        header: Source L3 Name
      - fields: [Vendor Name, l3_fmly_nm_dst]  # Coalesce variants
        header: Dest L3 Name
```

See the full recipe at [`config/recipes/l1_reconciliation.yaml`](config/recipes/l1_reconciliation.yaml).

To create a new recipe for a different reconciliation problem, copy this YAML and adjust the sources, populations, field mappings, and matching steps. The engine code stays the same.

### Key Concepts

**Populations:** A recipe splits source data into named populations using a filter DSL. Each population has a role: matching target (source records to reconcile), destination (trusted records to match against), or excluded (junk filtered out). Populations with no explicit filter become the remainder after other filters are applied. See [Filter Ops Reference](docs/filter-ops.md) for the full DSL.

**Matching cascade:** Steps run in order. Records that don't match or fail a threshold in one step move to the next. A record is only unmatched if it fails all steps.

**Matching methods:** `exact` uses Polars joins (fast, no Python loops). `fuzzy` uses RapidFuzz `cdist` for a full C++ score matrix. Configurable `threshold` (0-100) and `scorer` (token_sort_ratio, token_set_ratio, ratio, partial_ratio, WRatio).

**Normalization tiers:** Raw (as-is), Clean (lowercase + strip punctuation), Normalized (Clean + alias replacement + stopword removal, addresses only). Matching tries tiers in order; first match wins.

**Address scoring:** Post-match supporting evidence, not a matching criterion. Merges and cross-compares address fields, parses via libpostal or built-in tokenizer, weights street name at 60%.

**Inheritance:** On match, the recipe defines which fields to pull from the destination record into the result. This is how you derive new relationships (e.g. match on child name, inherit the parent ID).

**Match modes:** `best_match` (default) keeps one result per source record. Earlier steps and higher scores win. `all_matches` returns every candidate for analysis.

**Report columns:** The `output.columns` section is optional. When defined, it controls exactly which columns appear and in what order. Use `fields` (plural) for variant columns from different steps. The engine coalesces them automatically.

**Run summary:** Each run generates a markdown summary (`_summary.md`) and an Excel Summary tab with recipe config, per-step match counts, cascade explanation, and a Mermaid flow diagram. Use `--mermaid disabled` to skip the diagram.

**Recipe validation:** Recipes are validated against JSON Schema (`config/recipe_schema.json`). `additionalProperties: false` at every level catches typos and YAML indent mistakes as warnings. Structural errors block the pipeline. Run `--dry-run` for a full validation report.

### Signal Analysis

Before configuring a recipe, the signal analysis module can profile source data to suggest normalization config:

- Top N tokens per column at Raw and Clean tiers
- Auto-detect column type (name, address, date, ID)
- Suggested stopwords and alias groups
- Unicode profiling and flagging
- Data quality summary (null %, unique %, duplicates)

Signal analysis is a **separate profiling step** that does not run as part of the main pipeline. You invoke it against your source data to generate suggested `stopwords.json` and `aliases.json`, then reference those files in your recipe's `normalization` section. These only take effect when a step uses `tiers: [... normalized]`. The L1 recipe uses `[raw, clean]` only, so stopwords/aliases are not applied in that recipe. See `src/signal_analysis.py`.

## Documentation

| Document | Purpose |
|---|---|
| **[L1 Recipe](docs/l1-recipe.md)** | The first use case: problem context, field mappings, matching flow, constraints |
| **[ADR-001](docs/adr/001-matching-engine-tooling.md)** | Why Polars + RapidFuzz + libpostal (four options benchmarked) |
| **[Implementation Plan](docs/implementation-plan.md)** | Build phases, technical decisions, PR history, libpostal install guide |
| **[Filter Ops Reference](docs/filter-ops.md)** | Standard and special filter ops, population vs step filters, how to extend |
| **[How Scoring Works](docs/how-scoring-works.md)** | Name and address scoring: methods, tiers, worked example, report columns |
| **[Signal Analysis](docs/signal-analysis.md)** | Pre-pipeline data profiling: usage, output guide, stopword/alias workflow |

## License

[MIT](LICENSE)
