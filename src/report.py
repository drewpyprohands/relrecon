"""
Report generation module for the relational matching framework.

Generates formatted Excel workbooks from matching pipeline results:
- Summary tab: recipe config, per-step counts, cascade explanation
- Matched tab: matched records with derived fields, confidence, validation columns
- Analysis tab: unmatched records with reason codes

Uses openpyxl for Excel formatting (headers, conditional formatting, column widths).
"""

from pathlib import Path
from typing import Optional

import polars as pl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)

# Conditional fill colors for address scores
SCORE_HIGH = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")  # Green
SCORE_MED = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")   # Yellow
SCORE_LOW = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")    # Red

ANALYSIS_HEADER_FILL = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")


# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Main Report columns (order matters for Excel output)
MAIN_REPORT_COLUMNS = [
    ("l3_fmly_nm", "Source L3 Name"),
    ("vendor_id", "Source Vendor ID"),
    ("tpty_assm_nm", "Assessment Name"),
    ("hq_addr1", "Source Address 1"),
    ("hq_addr2", "Source Address 2"),
    ("derived_l1_name", "Derived L1 Name"),
    ("derived_l1_id", "Derived L1 ID"),
    ("match_step", "Match Source"),
    ("match_tier", "Match Tier"),
    ("name_score", "Name Score"),
    ("addr_score", "Address Score"),
    ("addr_street_match", "Street Match"),
    ("addr_comparison", "Address Comparison"),
    ("addr_tier", "Address Tier"),
]

# Destination fields (check for suffixed columns from join)
# Multiple entries per header handle core_parent vs Pop3 column naming
DEST_COLUMNS = [
    ("Vendor Name", "Dest L3 Name"),
    ("Vendor Name_dst", "Dest L3 Name"),
    ("l3_fmly_nm_dst", "Dest L3 Name"),
    ("Address1", "Dest Address 1"),
    ("hq_addr1_dst", "Dest Address 1"),
    ("Address2", "Dest Address 2"),
    ("hq_addr2_dst", "Dest Address 2"),
]

ANALYSIS_COLUMNS = [
    ("l3_fmly_nm", "L3 Name"),
    ("vendor_id", "Vendor ID"),
    ("tpty_assm_nm", "Assessment Name"),
    ("hq_addr1", "Address 1"),
    ("hq_addr2", "Address 2"),
    ("l1_fmly_nm", "L1 Name (invalid)"),
    ("tpty_l1_id", "L1 ID (invalid)"),
    ("data_entry_type", "Data Entry Type"),
    ("rq_intk_user", "Request User"),
    ("reason_code", "Reason Code"),
    ("rejection_step", "Rejection Step"),
    ("best_rejected_score", "Best Rejected Score"),
]


# ---------------------------------------------------------------------------
# Sheet writing helpers
# ---------------------------------------------------------------------------

def _write_headers(ws, columns: list, header_fill=HEADER_FILL):
    """Write formatted headers to a worksheet."""
    for col_idx, (_, header) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = header_fill
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


def _write_data(ws, df: pl.DataFrame, columns: list, start_row: int = 2):
    """Write DataFrame rows to worksheet, matching columns by name.

    Uses iter_rows. Required for cell-by-cell openpyxl writes.
    Not a data processing loop (ADR-001 prohibits iterrows for data ops,
    not for output serialization).
    """
    available_cols = set(df.columns)

    for row_idx, row in enumerate(df.iter_rows(named=True), start_row):
        for col_idx, (col_name, _) in enumerate(columns, 1):
            if col_name in available_cols:
                value = row[col_name]
                # Round score columns for clean display
                if col_name in ("addr_score", "name_score", "addr_street_score",
                                "best_rejected_score") and value is not None:
                    try:
                        value = round(float(value), 2)
                    except (ValueError, TypeError):
                        pass
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = THIN_BORDER

                # Conditional formatting for score columns (0-100 scale)
                if col_name in ("addr_score", "name_score") and value is not None:
                    try:
                        score = float(value)
                        if score >= 80:
                            cell.fill = SCORE_HIGH
                        elif score >= 60:
                            cell.fill = SCORE_MED
                        else:
                            cell.fill = SCORE_LOW
                    except (ValueError, TypeError):
                        pass


def _auto_width(ws, columns: list, min_width: int = 10, max_width: int = 40):
    """Auto-adjust column widths based on header length."""
    for col_idx, (_, header) in enumerate(columns, 1):
        width = min(max(len(header) + 4, min_width), max_width)
        ws.column_dimensions[get_column_letter(col_idx)].width = width


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _coalesce_variant_columns(df: pl.DataFrame, column_defs: list) -> pl.DataFrame:
    """Coalesce variant columns created by diagonal concat.

    Handles both recipe-driven column defs (with 'fields' key for variants)
    and legacy DEST_COLUMNS format (multiple entries sharing a header).

    For recipe-driven: entries with ``fields: [col1, col2]`` are coalesced
    into the first present column.

    For legacy: groups DEST_COLUMNS by header and coalesces variants.
    """
    for entry in column_defs:
        if isinstance(entry, dict) and "fields" in entry:
            # Recipe-driven: explicit variant list
            variants = entry["fields"]
            present = [v for v in variants if v in df.columns]
            if len(present) > 1:
                df = df.with_columns(
                    pl.coalesce(present).alias(present[0])
                )
        # Legacy tuple format handled below

    # Legacy: group tuple-format DEST_COLUMNS by header
    header_variants: dict[str, list[str]] = {}
    for entry in column_defs:
        if isinstance(entry, tuple):
            col_name, header = entry
            header_variants.setdefault(header, []).append(col_name)

    for _header, variants in header_variants.items():
        present = [v for v in variants if v in df.columns]
        if len(present) > 1:
            df = df.with_columns(
                pl.coalesce(present).alias(present[0])
            )
    return df


# Backward-compatible alias (used by legacy code paths without recipe-driven columns)
def _coalesce_dest_columns(df):
    return _coalesce_variant_columns(df, DEST_COLUMNS)


def _resolve_columns(df: pl.DataFrame, column_defs: list) -> list:
    """Resolve which columns exist in the DataFrame."""
    available = set(df.columns)
    resolved = []
    seen_headers = set()

    for col_name, header in column_defs:
        if col_name in available and header not in seen_headers:
            resolved.append((col_name, header))
            seen_headers.add(header)

    return resolved


def _build_columns_from_recipe(recipe_columns: list, df: pl.DataFrame) -> list:
    """Build (col_name, header) list from recipe output.columns config.

    Handles both single-field and multi-field (variant) entries.
    For variant entries, returns the first present column after coalescing.
    """
    available = set(df.columns)
    resolved = []

    for entry in recipe_columns:
        header = entry["header"]
        if "fields" in entry:
            # Variant column: use first present (coalesce already ran)
            for f in entry["fields"]:
                if f in available:
                    resolved.append((f, header))
                    break
        elif "field" in entry:
            if entry["field"] in available:
                resolved.append((entry["field"], header))

    return resolved


def generate_report(matched_df: pl.DataFrame, unmatched_df: pl.DataFrame,
                    output_path: str, stats: Optional[dict] = None,
                    recipe: Optional[dict] = None,
                    recipe_file: str | None = None) -> str:
    """Generate the Excel report with Summary, Matched, and Analysis tabs.

    Args:
        matched_df: Matched records from pipeline
        unmatched_df: Unmatched records from pipeline
        output_path: Path to write the Excel file
        stats: Optional pipeline stats dict
        recipe: Optional recipe dict. When present and output.columns
                is defined, uses recipe-driven column mapping instead
                of hardcoded defaults. Also enables the Summary tab.
        recipe_file: Optional recipe filename for summary tab header

    Returns:
        Path to the generated report
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Determine column source
    recipe_columns = None
    if recipe:
        recipe_columns = recipe.get("output", {}).get("columns", None)

    wb = Workbook()

    # --- Main Report Tab ---
    ws_main = wb.active
    ws_main.title = "Matched"

    if matched_df.height > 0:
        if recipe_columns and "matched" in recipe_columns:
            # Recipe-driven columns
            matched_df = _coalesce_variant_columns(matched_df, recipe_columns["matched"])
            main_cols = _build_columns_from_recipe(recipe_columns["matched"], matched_df)
        else:
            # Legacy hardcoded columns
            matched_df = _coalesce_dest_columns(matched_df)
            main_cols = _resolve_columns(matched_df, MAIN_REPORT_COLUMNS + DEST_COLUMNS)

        _write_headers(ws_main, main_cols)
        _write_data(ws_main, matched_df, main_cols)
        _auto_width(ws_main, main_cols)

        # Freeze top row
        ws_main.freeze_panes = "A2"
    else:
        ws_main.cell(row=1, column=1, value="No matches found")

    # --- Analysis Tab ---
    ws_analysis = wb.create_sheet("Analysis")

    if unmatched_df.height > 0:
        if "reason_code" not in unmatched_df.columns:
            unmatched_df = unmatched_df.with_columns(
                pl.lit("no_name_match").alias("reason_code"),
                pl.lit(None).cast(pl.String).alias("rejection_step"),
                pl.lit(None).cast(pl.Float64).alias("best_rejected_score"),
            )

        if recipe_columns and "analysis" in recipe_columns:
            analysis_cols = _build_columns_from_recipe(recipe_columns["analysis"], unmatched_df)
        else:
            analysis_cols = _resolve_columns(unmatched_df, ANALYSIS_COLUMNS)

        _write_headers(ws_analysis, analysis_cols, header_fill=ANALYSIS_HEADER_FILL)
        _write_data(ws_analysis, unmatched_df, analysis_cols)
        _auto_width(ws_analysis, analysis_cols)

        ws_analysis.freeze_panes = "A2"
    else:
        ws_analysis.cell(row=1, column=1, value="All records matched")

    # --- Summary Tab (first sheet) ---
    if recipe and stats:
        try:
            from summary import write_summary_tab
            ws_summary = wb.create_sheet("Summary", 0)  # Insert at position 0
            write_summary_tab(ws_summary, recipe, stats, matched_df,
                              recipe_file=recipe_file)
        except Exception as exc:
            import sys
            print(f"[WARN] Summary tab generation failed: {exc}", file=sys.stderr)

    wb.save(str(out))
    return str(out)


# ---------------------------------------------------------------------------
# Convenience: run full pipeline + generate report
# ---------------------------------------------------------------------------

def run_and_report(recipe_path: str, base_dir: str = ".",
                   output_path: Optional[str] = None) -> str:
    """Run the matching pipeline and generate the Excel report.

    Args:
        recipe_path: Path to recipe YAML/JSON
        base_dir: Base directory for data files
        output_path: Override output path (default from recipe)

    Returns:
        Path to generated report
    """
    from matching import run_pipeline
    from recipe import load_recipe

    recipe = load_recipe(recipe_path)
    result = run_pipeline(recipe, base_dir=base_dir)

    if output_path is None:
        output_path = recipe.get("output", {}).get("path", "output/report.xlsx")

    path = generate_report(
        result["matched"],
        result["unmatched"],
        output_path,
        stats=result["stats"],
        recipe=recipe,
    )

    return path
