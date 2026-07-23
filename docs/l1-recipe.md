# The First Recipe: L1 Reconciliation

Everything below describes the **first recipe** built on this framework: the real-world problem that motivated building it. The framework itself (normalization, signal analysis, matching engine, reporting) is reusable. Only the recipe config changes between use cases.

## The Problem

A large population of vendor records (~15k rows, 232 columns) was migrated into a source-of-record system over the past decade. During migration, parent-child relationships broke:

- **Parent IDs (`tpty_l1_id`)** were populated with placeholder or incorrect values
- **Parent names (`l1_fmly_nm`)** don't match the actual parent entities
- **Contract dates (`cntrct_cmpl_dt`)** are invalid for the migrated population

The only reliable field for the migrated records is the **child/vendor name (`l3_fmly_nm`)**. The goal: use that name to find the correct parent by matching against trusted sources and inheriting the real parent relationship.

## Terminology

| Term | Meaning |
|---|---|
| **L1** | Parent level (Supplier). What we're trying to derive. |
| **L3** | Child/Vendor level. The name we match on. |
| **Pop1** | Target population. Migrated records with broken L1 fields (vendor_id starts with V7) |
| **Pop3** | Valid population. Remaining records from same dataset, not guaranteed clean |
| **core_parent** | Trusted reference dataset with reliable L1-L3 relationships |
| **Date gate** | A filter that excludes destination records older than 2 years |

## Field Mapping

| Level | core_parent_export | tp_multi_pop_dataset |
|---|---|---|
| L3 (Child/Vendor) | Vendor Name, Vendor ID | l3_fmly_nm, vendor_id |
| L1 (Parent/Supplier) | Supplier Name, Supplier ID | l1_fmly_nm, tpty_l1_id |
| Address | Address1, Address2 | hq_addr1, hq_addr2 |

> **Note:** The `source.type` field in the recipe (e.g. `type: trusted_reference`) is informational only -- it documents intent but has no runtime effect.

> [!IMPORTANT]
> Pop1's parent-level fields were populated with placeholder or incorrect data during migration. The recipe marks these as `invalid_fields` in the population config (currently informational only, see [Issue #74](https://git.drewpy.pro/drewpypro/relational_matching/issues/74)).
> The `invalid_fields` key has no runtime effect currently -- it's informational only. See [Issue #74](https://git.drewpy.pro/drewpypro/relational_matching/issues/74) for future plans.
> The goal is to derive correct L1 by matching L3 names against trusted sources and inheriting the parent relationship.

## Datasets

**core_parent_export.csv** (Trusted Reference)
- Master vendor/supplier database with recent, trusted data
- Key columns: Updated (date), Vendor Name/ID (L3), Supplier Name/ID (L1), Address1/Address2

**tp_multi_pop_dataset.csv** (Multi-Population Source)
- Contains 3 populations in one file, separated by filter logic:

| Population | Filter | Description |
|---|---|---|
| Pop1 (target) | `vendor_id LIKE V7%` | Target for reconciliation. Invalid L1 fields |
| Garbage | non-V7 + `data_entry_type = 'Migrated'` + `rq_intk_user` contains "Data Migration" or "Goblindor" | Excluded from matching |
| Pop3 (valid) | Everything remaining | Valid records from real users, not guaranteed clean |

> [!CAUTION]
> Pop3 may still contain data quality issues. It simply passed the garbage filter. This is why exact name matching is required rather than trusting Pop3 fields at face value.

<details>
<summary><b>Key Column Details</b></summary>

- **l3_fmly_nm**: Child/Vendor name. The primary matching field.
- **tpty_assm_nm**: Infosec assessment third-party name. Better data quality when populated. Always included in report output for manual review.
- **cntrct_cmpl_dt**: Contract completion date. Invalid for Pop1. Used for 2-year recency on destination records only.
- **hq_addr1, hq_addr2**: Address fields. Mixed quality. Data may be in either column, split across both or abbreviated.
- **data_entry_type**: `Migrated` (from another system) or `net_new` (generated through normal business processes).
- **rq_intk_user**: Identifies automated migrations (Data Migration, Goblindor) vs. normal users.
- **Other columns** (timestamp, cntrc_expt, obgr_mgr_ext, tep_dgr, ogr_mdi): informational only, not used for matching.

</details>

## Context

- Data migration between systems is a normal process, but over the past decade, large migrations occurred under time pressure with inadequate tooling
- As migrations progressed they improved, but early batches lacked reconciliation back to trusted records. Some fields were populated with best-effort or placeholder values
- Pop1 records (all V7* vendor IDs) were migrated by Data Migration or Goblindor users with invalid `l1_fmly_nm`, `tpty_l1_id`, and `cntrct_cmpl_dt`

## Matching Flow

**Step 1: Pop1 -> core_parent.** Match `l3_fmly_nm` to `Vendor Name` (Raw/Clean exact). On match, inherit L1 (Supplier Name/ID). Destination `Updated` must be within 2 years.

**Step 2: Pop1 -> Pop3.** Match `l3_fmly_nm` to `l3_fmly_nm` (Raw/Clean exact). On match, inherit `tpty_l1_id`/`l1_fmly_nm`. Pop3's `cntrct_cmpl_dt` must be within 2 years.

**Step 3: Fuzzy Pop1 -> core_parent.** Same as Step 1 but with RapidFuzz `token_sort_ratio` at threshold 70.

**Step 4: Fuzzy Pop1 -> Pop3.** Same as Step 2 but fuzzy at threshold 70.

All steps run against **all** Pop1 records -- the "cascade" is resolved at the end via dedup, not by excluding matched records from later steps. In `best_match` mode (default), dedup keeps the earliest step then highest scores when multiple steps match the same record. In single-phase `all_matches` mode, each record retains every exact candidate or every threshold-passing fuzzy candidate from its first successful tier. This can produce N×M candidates and a correspondingly larger report and runtime; multi-phase behavior is unchanged.

### Manual exclusions

To suppress specific bad matches at a named step (so the record falls through to later steps), list them in one CSV and point the recipe at it:

```yaml
exclusions: config/exclusions.csv   # top-level recipe key
```

```csv
step,vnd_id,note
Match Pop1 to core_parent,V7001,wrong core_parent match -- reviewed 2026-07
Match Pop1 to Pop3,V7042,pop3 dupe
```

Each row routes the id into that step's existing `exclude` mechanism, **matched against the population `record_key`** -- the CSV column name is only a label, not a field in your data. Excluded records still cascade to later steps. Any inline per-step `exclude` block continues to apply and is merged with the file. The run summary reports per-step exclusion counts and IDs.

**Naming the id column.** The id column defaults to `vnd_id`, but if your data uses a different identifier, name it whatever you like and either let it auto-detect (when it's the only non-`note` column) or point at it explicitly:

```yaml
exclusions:
  file: config/exclusions.csv
  id_column: supplier_id
```

```csv
step,supplier_id,note
Match Pop1 to core_parent,S1001,merged into parent
```

Address matching is **supporting evidence** in all steps, not standalone match criteria.

### Address Matching

<details>
<summary><b>How address matching works (detailed walkthrough)</b></summary>

**Problem:** Address data is split unpredictably across two columns. We can't trust which column holds what.

**Approach:**

1. **Build variants**: for each record, `addr1_only`, `addr2_only`, `addr_merged` (concatenated)

2. **Normalize**: apply tier (raw, clean, or normalized with alias expansion)

3. **Score (full)**: RapidFuzz token_sort_ratio on the normalized full strings

4. **Parse**: extract street name using libpostal or built-in tokenizer

5. **Score (street)**: RapidFuzz ratio on extracted street names, 60/40 weighting when street match detected

6. **Compare**: specific field pairs first (addr1<>addr1, addr1<>addr2, etc.), then merged<>merged.
   Best weighted score across all tiers and comparisons wins. On equal scores, specific fields preferred.

See [how-scoring-works.md](how-scoring-works.md) for a detailed walkthrough with worked examples.

**Example:**
```
Pop1:  "194 6th Avenue Floor 7"        -> street: 6th Avenue
Core:  "194 6TH AVENUE FL 7 NY 10005"  -> street: 6th Avenue

Street match: 100% | Token overlap: 67% | Weighted: ~85%
```

**Key insight:** 75% overall + street match = strong signal. 90% overall with no street match = suspicious (matching on city/state/zip only).

</details>

### Date Rules

- 2-year recency window applies to **destination records only**:
    - core_parent: `Updated` must be within 2 years
    - Pop3: `cntrct_cmpl_dt` must be within 2 years
- Pop1's `cntrct_cmpl_dt` is invalid and is NOT checked

## Step Defaults

Recipes with repeated config across steps can use `step_defaults` at the recipe root to reduce duplication. Values are deep-merged into every step before validation -- step-level values always win on conflict.

**Example 1: All steps share the same inherit (single destination pop)**

```yaml
step_defaults:
  address_support:
    source: [hq_addr1, hq_addr2]
    destination: [hq_addr1, hq_addr2]
    parser: auto
    tiers: [clean]
    weights:
      street_name: 0.75
  inherit:
    - source: l1_fmly_nm
      as: derived_l1_name
    - source: tpty_l1_id
      as: derived_l1_id

steps:
  - name: Exact Match Pop1 L3 to Pop3 L3
    source: pop1
    destination: pop3
    match_fields:
      - source: l3_fmly_nm
        destination: l3_fmly_nm
        method: exact
        tiers: [raw, clean, normalized]
    address_support:
      threshold: 75  # override just the threshold, inherit the rest
    # inherit comes from step_defaults (all steps use same pop3 columns)
```

**Example 2: Steps have different destination pops with different columns**

```yaml
step_defaults:
  address_support:
    parser: auto
    tiers: [clean]
    weights:
      street_name: 0.6
  # NO inherit here -- steps override it per destination

steps:
  - name: Match Pop1 to core_parent
    source: pop1
    destination: core_parent
    match_fields:
      - source: l3_fmly_nm
        destination: Vendor Name
        method: exact
        tiers: [raw, clean]
    address_support:
      source: [hq_addr1, hq_addr2]
      destination: [Address1, Address2]  # core_parent column names
      threshold: 75
    inherit:  # core_parent columns
      - source: Supplier Name
        as: derived_l1_name
      - source: Supplier ID
        as: derived_l1_id

  - name: Match Pop1 to Pop3
    source: pop1
    destination: pop3
    match_fields:
      - source: l3_fmly_nm
        destination: l3_fmly_nm
        method: exact
        tiers: [raw, clean]
    address_support:
      source: [hq_addr1, hq_addr2]
      destination: [hq_addr1, hq_addr2]  # pop3 column names
      threshold: 75
    inherit:  # pop3 columns (different names than core_parent)
      - source: l1_fmly_nm
        as: derived_l1_name
      - source: tpty_l1_id
        as: derived_l1_id
```

**Merge rules:**
- **Dicts** merge recursively. A step can override `weights.street_name` without losing other default weights.
- **Lists** (like `inherit`) replace entirely. If a step defines its own `inherit`, the default `inherit` is ignored.
- **Scalars** (strings, numbers) in the step replace the default.

> [!NOTE]
> When a step defines `inherit`, it completely replaces any `inherit` from step_defaults. This is by design -- lists don't merge because the order and content matter. Use `inherit` in step_defaults only when all steps inherit the same columns. Otherwise, define it per step as shown in Example 2.

See `tests/recipes/step_defaults_example.yaml` for a compact example using step_defaults. Compare with `l1_recon_80.yaml` (explicit per-step) to see the verbosity difference.

## Output Report

With `summary: [md, xlsx]` (configured in the recipe's `output` block), the pipeline produces:

**Excel report** (`_report.xlsx` or the main `.xlsx` when format is also xlsx) with four tabs:

- **Summary**: recipe config, population descriptions, per-step match counts, cascade explanation, pipeline timing
- **Matched**: source/destination L3 names (side-by-side), derived L1 ID + Name, match source, match tier, address scores with source and destination addresses and `tpty_assm_nm` for review
- **Analysis**: unmatched Pop1 records with reason codes for human review
- **Recipe**: verbatim echo of the resolved recipe (incl. exclusions, derived columns, tie-breakers). Round-trips: parse column A as YAML to reconstruct the recipe. Never written to the matched/unmatched CSVs, which are DW imports.

**Markdown summary** (`_summary.md`) with the same information plus a Mermaid cascade diagram and a resolved-recipe section.

The `summary` key must be explicit. Without it, only raw data is produced (no report tabs, no markdown). See ADR-003 for details.

The output section also supports a `tie_breaker` config for selecting among duplicate destination matches and same-population matching (where source == destination). See [How Scoring Works](how-scoring-works.md#tie-breaker-outputtie_breaker) for details.

### Unmatched companion export

For a raw data export (`format: csv` or `parquet`), set `emit_unmatched: true` to also write an unmatched companion next to the matched file:

```yaml
output:
  format: csv
  emit_unmatched: true
  columns:
    matched: [...]
    analysis: [...]   # resolves the companion's columns
```

- Matched export is unchanged: `{base}.{format}` via `columns.matched`.
- Companion is new: `{base}_unmatched.{format}` via `columns.analysis`, same format, UTF-8 + header row (DW-importable). `reason_code` and rejection fields are backfilled when absent, matching the Analysis tab.
- Columns are purely recipe-driven -- nothing synthesized. The filename is the matched/unmatched discriminator; union the two files downstream for a combined view.
- Define `columns.analysis` for the companion to match the Analysis tab. Without it, the companion exports all columns (same fallback as the matched export without `columns.matched`), which will not match the tab's curated columns.
- Zero unmatched rows still writes a header-only file, so a downstream job sees the file every run. Header stability holds for normal runs; a degenerate run (empty source population or a missing tracked field) can yield a frame with no columns, collapsing the header to just the backfilled reason columns.
- Pairs with the raw data export, not the xlsx report (whose Analysis tab already carries unmatched records). Off by default. Setting it in xlsx report mode has no effect and emits a validation warning.

## Name Normalization Note

> [!IMPORTANT]
> **For this recipe, names should not be normalized.** Suffixes like Inc/Ltd/Pty Ltd distinguish different entities and L1 parents in this dataset.
> Example: "Armitage Solutions Pty Ltd" (V562841 -> S07589) vs "Armitage Solutions Ltd" (V562900 -> S07601) are **different L1 parents** that normalization would incorrectly merge.
> Other recipes may benefit from name normalization. Add `normalized` to the `tiers` list in the recipe's match_fields to enable it. The choice is recipe-specific.

## Constraints

- **Dataset is global**: addresses span multiple countries/formats (not US-only)
- Must handle hierarchical relationships (L1 to L3, vendor to parent)
- Address field quality varies (addr2 unreliable); prioritize addr1
- 2-year recency window on destination records only (Pop1 dates are invalid)
- Must distinguish between 3 populations in the multi-pop dataset
- Pop3 is not guaranteed clean. Exact matching required
- Report must be auditable (show *why* each match fired)
- Must run on local developer hardware (HP ZBook: Ryzen 9 PRO 7940HS 8-core, 64GB RAM)

## Recipe Variants

Several recipe variants demonstrate different features and trade-offs:

| Recipe | Match Rate | Key Difference |
|---|---|---|
| `l1_reconciliation.yaml` | ~69% (31/45) | Baseline 4-step cascade with 2-field address |
| `l1_recon_80.yaml` | ~80% (36/45) | Higher match rate with street_weight=0.75 |
| `step_defaults_example.yaml` | ~84% (38/45) | Uses step_defaults + core_parent fallback |

### step_defaults

The `step_defaults` feature reduces recipe verbosity by defining `address_support` and `inherit` blocks once at the top level. Steps inherit these defaults unless they provide a local override. Compare `step_defaults_example.yaml` (compact) with `l1_recon_80.yaml` (explicit per-step) to see the difference.

### Address Field Selection

The test dataset has three address fields: `hq_addr1` (street), `hq_addr2` (city/state/zip), and `hq_addr3` (country). Using 2-field matching (addr1 + addr2) vs 3-field (addr1 + addr2 + addr3) produces different address scores but matches the same records. The 3-field approach was removed because `hq_addr3` is primarily country codes, which adds noise to address scoring without improving match accuracy.
