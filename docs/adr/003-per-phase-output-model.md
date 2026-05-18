# ADR-003: Per-Phase Output Model

**Status:** Accepted
**Date:** 2026-05-18
**Author:** AI Assistant
**Deciders:** drewpypro

---

## Context

Multi-phase recipes chain matching passes into a pipeline (e.g. vendor -> LEI -> parent LEI -> parent name). Prior to this change, all output configuration lived at the top level and produced a single combined report after all phases completed.

This created several problems:

1. **No intermediate output** -- a 3-phase GLEIF pipeline only produced one combined file. Users couldn't get Phase 1 results (vendor-to-LEI matches) as a standalone deliverable for database import.
2. **Mixed concerns** -- `format: xlsx` always produced a formatted report with Summary tab. There was no way to get raw data for warehouse import vs a human-readable report.
3. **Wrong summary stats** -- per-phase XLSX reports used combined pipeline stats instead of phase-specific stats.
4. **Ambiguous config** -- having both top-level and per-phase output blocks created undefined behavior about which one "wins."

## Decision

### Output config structure

Each output block (top-level or per-phase) supports two independent concerns:

```yaml
output:
  format: csv              # csv or xlsx -- raw data, no formatting
  path: output/data.csv    # optional, auto-generated if omitted
  columns:
    matched: [...]         # column mapping for matched data
    analysis: [...]        # column mapping for unmatched data
  summary: md              # none, md, xlsx, or [md, xlsx]
```

- **`format`** -- data export format. CSV or XLSX. Always raw data with no summary tab, even for XLSX. This is for database/warehouse import.
- **`summary`** -- optional reporting layer:
  - `md` -- markdown summary file (`_summary.md`)
  - `xlsx` -- formatted XLSX report with Summary + Matched + Analysis tabs (`_report.xlsx`)
  - `[md, xlsx]` -- both
  - `none` -- explicitly no summary
  - Omitted -- defaults to `none` for per-phase, backward-compatible default for top-level (see below)
- **`columns`** -- shared between data export and summary report. The `analysis` key is only used by summary reports.

### Placement rules

| Recipe type | Where output goes | Validation |
|---|---|---|
| Single-phase | Top-level `output` only | Error if no top-level `output`. Error if phase-level `output` exists. |
| Multi-phase | Per-phase `output` blocks | Error if top-level `output` exists. Error if zero phases have `output`. |

These rules prevent ambiguity. There is exactly one way to configure output for each recipe type.

### No implicit summary defaults

The `summary` key must be explicit. When `summary` is omitted from any output block (top-level or per-phase), the default is `none` -- no summary files are generated. All recipes should declare `summary: [md, xlsx]`, `summary: md`, `summary: xlsx`, or `summary: none` explicitly.

This avoids ambiguity about what output a recipe produces and makes the config self-documenting.

### File naming

| Output type | Path pattern | Example |
|---|---|---|
| Data (CSV) | `{path}` or auto | `output/phase1_vendor_lei.csv` |
| Data (XLSX) | `{path}` or auto | `output/phase1_vendor_lei.xlsx` |
| Summary MD | `{data_path}_summary.md` or auto | `output/phase1_vendor_lei_summary.md` |
| Report XLSX | `{data_path}_report.xlsx` or auto | `output/phase1_vendor_lei_report.xlsx` |

When `path` is omitted, auto-generated from recipe/phase name + timestamp.

When only summary is configured (no `format`), summary files are generated from the phase name.

## Consequences

- Single-phase recipes work identically to before (backward compatible)
- Multi-phase recipes must move `output` from top-level to per-phase (breaking change for multi-phase recipes, which are new and not yet in production)
- Each phase's output is self-contained -- Phase 2 output only contains Phase 2 data (plus whatever was inherited via `_previous_matched`)
- Users can produce raw CSV for warehouse import and formatted XLSX report from the same phase
- Summary stats are phase-specific (not combined pipeline stats)
