"""CLI entry point for the relational matching pipeline.

Usage:
    python -m src --recipe config/recipes/l1_reconciliation.yaml
    python -m src --recipe config/recipes/l1_reconciliation.yaml --data data/ --output output/report.xlsx
    python -m src --recipe config/recipes/l1_reconciliation.yaml --no-libpostal
"""

import argparse
import sys
import time
from pathlib import Path

# Add src/ to path so bare imports (from normalize import ...) work
sys.path.insert(0, str(Path(__file__).parent))


def _run_signal_analysis(args) -> int:
    """Run signal analysis on a data file and print a formatted report."""
    from recipe import load_source
    from signal_analysis import analyze_dataset, select_columns
    from signal_report import format_report

    file_path = Path(args.analyze)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return 1

    save_dir = Path(args.save_config) if args.save_config else None
    if save_dir and not save_dir.is_dir():
        print(f"Error: directory '{save_dir}' does not exist. Create it first.",
              file=sys.stderr)
        return 1

    # Resolve output format
    signal_format = getattr(args, "signal_format", "md") or "md"
    signal_output = getattr(args, "signal_output", None)

    try:
        df = load_source({"file": str(file_path)}, base_dir=".")
    except Exception as e:
        print(f"Error loading file: {e}", file=sys.stderr)
        return 1

    print(f"Loaded: {file_path} ({df.height} rows, {df.width} cols)")

    try:
        columns, msg = select_columns(df, args.columns)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if msg:
        print(msg)

    # Warn on large Excel output
    top_n = args.top if args.top > 0 else None
    if signal_format in ("xlsx", "both") and top_n:
        est_cells = len(columns) * top_n * 6  # ~6 cols per row
        if est_cells > 50000:
            print(f"Warning: estimated {est_cells:,} cells in Excel output "
                  f"({len(columns)} columns x {top_n} top). This may take a moment.")

    print(f"Analyzing {len(columns)} columns...")
    results = analyze_dataset(df, columns, unicode_mode="profile_only",
                              output_dir=str(save_dir) if save_dir else None)

    sections = set(s.strip() for s in args.sections.split(",")) if args.sections else None

    # Markdown report (to stdout and optionally to file)
    if signal_format in ("md", "both"):
        report = format_report(results, file_path=str(file_path), columns=columns,
                               sections=sections, top_n=top_n)
        print()
        print(report)

        if signal_output and signal_format == "md":
            out_path = signal_output
            if not out_path.endswith(".md"):
                out_path += ".md"
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                f.write(report)
            print(f"Markdown report saved: {out_path}")

    # Excel report
    if signal_format in ("xlsx", "both"):
        from signal_excel import generate_signal_excel

        if signal_output:
            xlsx_path = signal_output
            if signal_format == "both" and not xlsx_path.endswith(".xlsx"):
                xlsx_path += ".xlsx"
            elif signal_format == "xlsx" and not xlsx_path.endswith(".xlsx"):
                xlsx_path += ".xlsx"
        else:
            # Auto-generate from input filename
            from datetime import datetime as _dt
            stem = file_path.stem
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            xlsx_path = f"output/signal_analysis_{stem}_{ts}.xlsx"

        xlsx_result = generate_signal_excel(
            results, xlsx_path, top_n=top_n, summary_top_n=25)
        print(f"Excel report saved: {xlsx_result}")

        # If both, also save markdown alongside
        if signal_format == "both" and signal_output:
            md_path = xlsx_path.replace(".xlsx", ".md")
            report = format_report(results, file_path=str(file_path), columns=columns,
                                   sections=sections, top_n=top_n)
            with open(md_path, "w") as f:
                f.write(report)
            print(f"Markdown report saved: {md_path}")

    if save_dir:
        print(f"Config saved to: {save_dir}/stopwords.json, {save_dir}/aliases.json")

    return 0


def _make_phase_slug(phase_cfg: dict, phase_idx: int) -> str:
    """Generate a filesystem-safe slug from a phase name."""
    import re
    name = phase_cfg.get("name", f"phase_{phase_idx + 1}")
    # Replace non-alnum with underscores, collapse runs, strip edges
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return slug or f"phase_{phase_idx + 1}"


def _write_output(
    output_cfg: dict,
    matched_df,
    unmatched_df,
    output_path: str,
    stats: dict,
    recipe: dict,
    recipe_file: str,
    mermaid_mode: str = "default",
    timing: dict | None = None,
):
    """Write output files for a single-phase recipe (backward compatible)."""
    import time

    from recipe import resolve_summary_modes
    from report import apply_column_mapping, generate_report, write_raw_data

    summary_modes = resolve_summary_modes(output_cfg)
    fmt = output_cfg.get("format", "xlsx")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if "xlsx" in summary_modes and fmt == "xlsx":
        # Legacy mode: xlsx format + xlsx summary = formatted report
        t_report = time.time()
        report_path = generate_report(
            matched_df, unmatched_df, output_path,
            stats=stats, recipe=recipe, recipe_file=recipe_file,
        )
        print(f"Report saved: {report_path} ({time.time() - t_report:.2f}s)")
    else:
        # Raw data export
        export_df = apply_column_mapping(matched_df, output_cfg)
        write_raw_data(export_df, output_path, fmt)
        print(f"Data saved: {output_path} ({fmt}, {export_df.height} rows)")

    if "md" in summary_modes:
        try:
            from summary import generate_summary
            summary_md = generate_summary(
                recipe, stats, matched_df, timing=timing,
                mermaid=mermaid_mode, recipe_file=recipe_file,
            )
            summary_path = output_path.rsplit(".", 1)[0] + "_summary.md"
            with open(summary_path, "w") as f:
                f.write(summary_md)
            print(f"Summary saved: {summary_path}")
        except Exception as exc:
            print(f"[WARN] Summary generation failed: {exc}", file=sys.stderr)

    if "xlsx" in summary_modes and fmt != "xlsx":
        # Summary xlsx report alongside CSV/parquet data
        try:
            report_path = output_path.rsplit(".", 1)[0] + "_report.xlsx"
            generate_report(
                matched_df, unmatched_df, report_path,
                stats=stats, recipe=recipe, recipe_file=recipe_file,
            )
            print(f"Report saved: {report_path}")
        except Exception as exc:
            print(f"[WARN] Report generation failed: {exc}", file=sys.stderr)


def _write_phase_output(
    phase_cfg: dict,
    phase_idx: int,
    phase_df,
    phase_stats: dict,
    overall_stats: dict,
    recipe: dict,
    recipe_file: str,
    timestamp: str,
    mermaid_mode: str = "default",
):
    """Write output files for a single phase in a multi-phase pipeline."""
    from recipe import resolve_summary_modes
    from report import apply_column_mapping, generate_report, write_raw_data

    phase_output = phase_cfg.get("output", {})
    fmt = phase_output.get("format", "csv")
    summary_modes = resolve_summary_modes(phase_output)
    phase_name = phase_cfg.get("name", f"Phase {phase_idx + 1}")
    phase_slug = _make_phase_slug(phase_cfg, phase_idx)

    # Resolve data output path
    data_path = phase_output.get("path")
    if not data_path:
        ext = fmt if fmt in ("csv", "parquet") else "xlsx"
        data_path = f"output/{phase_slug}_{timestamp}.{ext}"

    Path(data_path).parent.mkdir(parents=True, exist_ok=True)

    # Write data file
    export_df = apply_column_mapping(phase_df, phase_output)
    write_raw_data(export_df, data_path, fmt)
    print(f"Phase {phase_idx + 1} data: {data_path} ({fmt}, {export_df.height} rows)")

    # Build phase-specific stats for summaries
    phase_input = phase_stats.get("input_count", 0)
    phase_matched = phase_stats.get("matched_count", 0)
    p_stats = {
        "total_source": phase_input,
        "matched_count": phase_matched,
        "unmatched_count": phase_input - phase_matched,
        "step_details": phase_stats.get("step_details", []),
        "step_counts": phase_stats.get("step_counts", {}),
        "phases": [phase_stats],
    }

    # Build a phase-scoped mini-recipe for report generation (generate_report)
    mini_recipe = {
        "name": phase_name,
        "sources": recipe.get("sources", {}),
        "populations": phase_cfg.get("populations", {}),
        "steps": phase_cfg.get("steps", []),
        "output": phase_output,
    }

    base_path = data_path.rsplit(".", 1)[0]

    if "md" in summary_modes:
        try:
            from summary import generate_phase_summary
            summary_md = generate_phase_summary(
                phase_cfg=phase_cfg,
                phase_idx=phase_idx,
                phase_stats=phase_stats,
                recipe=recipe,
                recipe_file=recipe_file,
                mermaid=mermaid_mode,
            )
            summary_path = base_path + "_summary.md"
            with open(summary_path, "w") as f:
                f.write(summary_md)
            print(f"Phase {phase_idx + 1} summary: {summary_path}")
        except Exception as exc:
            print(f"[WARN] Phase {phase_idx + 1} summary failed: {exc}", file=sys.stderr)

    if "xlsx" in summary_modes:
        try:
            report_path = base_path + "_report.xlsx"
            generate_report(
                phase_df, None, report_path,
                stats=p_stats, recipe=mini_recipe, recipe_file=recipe_file,
            )
            print(f"Phase {phase_idx + 1} report: {report_path}")
        except Exception as exc:
            print(f"[WARN] Phase {phase_idx + 1} report failed: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="relational_matching",
        description="Config-driven relational matching engine. "
        "Runs a recipe against source datasets and generates an Excel report.",
    )
    parser.add_argument(
        "--recipe",
        default="config/recipes/l1_reconciliation.yaml",
        help="Path to recipe YAML/JSON (default: config/recipes/l1_reconciliation.yaml)",
    )
    parser.add_argument(
        "--data",
        default="data",
        help="Base directory for data files referenced in the recipe (default: data)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Override output report path (default: from recipe config)",
    )
    parser.add_argument(
        "--no-libpostal",
        action="store_true",
        help="Force built-in address tokenizer even if libpostal is installed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate recipe and load data without running the matching pipeline",
    )
    parser.add_argument(
        "--mermaid",
        nargs="?",
        const="default",
        default="default",
        choices=["default", "detailed", "disabled"],
        help="Mermaid diagram mode in summary: default, detailed, or disabled (default: default)",
    )
    parser.add_argument(
        "--analyze",
        default=None,
        metavar="FILE",
        help="Run signal analysis on a data file instead of the matching pipeline",
    )
    parser.add_argument(
        "--columns",
        default=None,
        help="'auto' to detect name/address columns, or comma-separated names. Default: all string columns",
    )
    parser.add_argument(
        "--save-config",
        default=None,
        metavar="DIR",
        help="Save suggested stopwords.json and aliases.json to this directory (use with --analyze)",
    )
    parser.add_argument(
        "--sections",
        default=None,
        help="Comma-separated report sections to include "
             "(quality,tokens,stopwords,aliases,unicode,suggestions). Default: all",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Max items per section (tokens, aliases, stopwords). 0 = show all. Default: 15",
    )
    parser.add_argument(
        "--signal-output",
        default=None,
        metavar="FILE",
        help="Output path for signal analysis report (auto-generates if not set, use with --analyze)",
    )
    parser.add_argument(
        "--signal-format",
        default="md",
        choices=["md", "xlsx", "both"],
        help="Signal analysis output format: md (markdown), xlsx (Excel), both (default: md)",
    )
    parser.add_argument(
        "--profile-imports",
        action="store_true",
        help="Print import timing for each module at startup (debugging startup delay)",
    )

    args = parser.parse_args()

    # Signal analysis mode
    if args.analyze:
        return _run_signal_analysis(args)

    # Validate recipe path exists
    recipe_path = Path(args.recipe)
    if not recipe_path.exists():
        print(f"Error: recipe not found: {recipe_path}", file=sys.stderr)
        return 1

    # Validate data directory exists
    data_dir = Path(args.data)
    if not data_dir.is_dir():
        print(f"Error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    _profile = getattr(args, "profile_imports", False)
    if _profile:
        import time as _ptime

    # Disable libpostal if requested
    if args.no_libpostal:
        import address
        address.LIBPOSTAL_AVAILABLE = False
        print("libpostal disabled -- using built-in address tokenizer")
    elif _profile:
        _t = _ptime.time()
        try:
            from postal.parser import parse_address as _lp  # noqa: F401
            print(f"[profile] libpostal import: {_ptime.time()-_t:.3f}s (available)")
        except (ImportError, SystemError, OSError):
            print(f"[profile] libpostal import: {_ptime.time()-_t:.3f}s (not available)")

    if _profile:
        _t = _ptime.time()

    from recipe import (
        RecipeValidationError,
        build_filter_expr,
        filter_population,
        format_validation_summary,
        load_recipe,
        load_source,
        validate_fields,
        validate_recipe,
    )
    if _profile:
        print(f"[profile] recipe imports: {_ptime.time()-_t:.3f}s")
        _t = _ptime.time()

    from matching import run_pipeline
    if _profile:
        print(f"[profile] matching imports: {_ptime.time()-_t:.3f}s")
        _t = _ptime.time()

    from report import generate_report  # noqa: F401 (pre-warm import for profiling)
    if _profile:
        print(f"[profile] report imports: {_ptime.time()-_t:.3f}s")
        _t = _ptime.time()

    # Load and validate recipe
    print(f"Loading recipe: {recipe_path}")
    try:
        recipe = load_recipe(str(recipe_path))
    except (ValueError, FileNotFoundError) as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1

    # load_recipe already ran validate_recipe (raises on errors).
    # Call again to capture schema warnings for dry-run display.
    schema_warnings = validate_recipe(recipe)

    print(f"Recipe: {recipe.get('name', 'unnamed')}")
    print(f"Data directory: {data_dir}")

    if args.dry_run:
        # Enhanced dry-run: load data, build populations, validate fields
        sources = {}
        for name, cfg in recipe["sources"].items():
            sources[name] = load_source(
                cfg, str(data_dir),
                recipe_name=recipe.get("name", ""),
                source_name=name,
            )

        # Collect all populations (top-level or from phases)
        all_populations = recipe.get("populations", {})
        if not all_populations and "phases" in recipe:
            for phase in recipe["phases"]:
                all_populations.update(phase.get("populations", {}))

        # Pre-validate filter fields before building populations
        filter_errors = []
        for pop_name, pop_cfg in all_populations.items():
            src_name = pop_cfg.get("source", "")
            if src_name not in sources:
                continue
            src_cols = set(sources[src_name].columns)
            for cond in pop_cfg.get("filter", []):
                if "field" in cond and cond["field"] not in src_cols:
                    available = ", ".join(sorted(src_cols)[:10])
                    filter_errors.append(
                        f'Population "{pop_name}" filter field "{cond["field"]}" '
                        f"not found. Available: {available}"
                    )
        if filter_errors:
            print("\n❌ Filter field errors:", file=sys.stderr)
            for e in filter_errors:
                print(f"  {e}", file=sys.stderr)
            return 1

        populations = {}
        for pop_name, pop_cfg in all_populations.items():
            src_name = pop_cfg["source"]
            if src_name.startswith("_"):
                continue
            src_df = sources[src_name]
            if "filter" in pop_cfg and pop_cfg["filter"]:
                filtered = filter_population(src_df, pop_cfg)
                populations[pop_name] = {"config": pop_cfg, "df": filtered, "source": src_name}
            else:
                populations[pop_name] = {"config": pop_cfg, "df": None, "source": src_name}

        # Compute remainder populations
        for pop_name, pop_data in populations.items():
            if pop_data["df"] is not None:
                continue
            src_df = sources[pop_data["source"]]
            remainder = src_df
            for other_name, other_data in populations.items():
                if other_name == pop_name or other_data["source"] != pop_data["source"]:
                    continue
                other_cfg = other_data["config"]
                if "filter" in other_cfg and other_cfg["filter"]:
                    remainder = remainder.filter(~build_filter_expr(other_cfg["filter"]))
            for garb_name, garb_cfg in all_populations.items():
                if garb_name == pop_name:
                    continue
                if garb_cfg.get("action") == "exclude" and "filter" in garb_cfg and garb_cfg["filter"]:
                    remainder = remainder.filter(~build_filter_expr(garb_cfg["filter"]))
            pop_data["df"] = remainder

        val_errors, val_warnings = validate_fields(recipe, sources, populations)
        summary = format_validation_summary(recipe, sources, populations, val_errors, val_warnings, schema_warnings)
        print(summary)
        return 1 if val_errors else 0

    print("Loading sources...")
    t0 = time.time()
    try:
        result = run_pipeline(recipe, base_dir=str(data_dir))
    except RecipeValidationError as e:
        print(f"\nError: {e}", file=sys.stderr)
        print("Hint: run with --dry-run for detailed validation report", file=sys.stderr)
        return 1
    elapsed = time.time() - t0

    stats = result.get("stats", {})
    timing = result.get("timing", {})
    print(f"Pipeline complete in {elapsed:.2f}s")
    if timing:
        phases = [("load", "Load"), ("setup", "Setup"), ("match", "Match"), ("resolve", "Resolve")]
        parts = [f"{label} {timing[k]:.2f}s" for k, label in phases if k in timing]
        print(f"  Timing:            {' | '.join(parts)}")
    print(f"  Source records:    {stats.get('total_source', 'N/A')}")
    print(f"  Matched:           {stats.get('matched_count', 'N/A')}")
    print(f"  Unmatched:         {stats.get('unmatched_count', 'N/A')}")

    # --- Output generation ---
    from datetime import datetime as _dt

    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    mermaid_mode = getattr(args, "mermaid", "default")
    is_multi_phase = "phases" in recipe

    if is_multi_phase:
        # Multi-phase: per-phase output (ADR-003)
        phase_snapshots = result.get("phase_snapshots", [])
        phase_stats_list = result.get("phases", [])

        for phase_idx, phase_cfg in enumerate(recipe["phases"]):
            phase_output = phase_cfg.get("output")
            if not phase_output or phase_idx >= len(phase_snapshots):
                continue
            phase_df = phase_snapshots[phase_idx]
            if phase_df.height == 0:
                print(f"Phase {phase_idx + 1}: skipped (empty)")
                continue

            _write_phase_output(
                phase_cfg=phase_cfg,
                phase_idx=phase_idx,
                phase_df=phase_df,
                phase_stats=phase_stats_list[phase_idx] if phase_idx < len(phase_stats_list) else {},
                overall_stats=stats,
                recipe=recipe,
                recipe_file=str(recipe_path.name),
                timestamp=timestamp,
                mermaid_mode=mermaid_mode,
            )
    else:
        # Single-phase: top-level output (backward compatible)
        output_cfg = recipe.get("output", {})
        output_path = args.output
        if output_path is None:
            recipe_name = recipe.get("name", "report").lower().replace(" ", "_")
            recipe_name = "".join(c if c.isalnum() or c == "_" else "" for c in recipe_name)
            from recipe import resolve_summary_modes
            fmt = output_cfg.get("format", "xlsx")
            summary_modes = resolve_summary_modes(output_cfg)
            # Legacy xlsx report mode: keep .xlsx extension
            if fmt == "xlsx" and "xlsx" in summary_modes:
                ext = "xlsx"
            else:
                ext = fmt if fmt in ("csv", "parquet") else "xlsx"
            output_path = f"output/{recipe_name}_{timestamp}.{ext}"

        _write_output(
            output_cfg=output_cfg,
            matched_df=result["matched"],
            unmatched_df=result["unmatched"],
            output_path=output_path,
            stats=stats,
            recipe=recipe,
            recipe_file=str(recipe_path.name),
            mermaid_mode=mermaid_mode,
            timing=result.get("timing"),
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
