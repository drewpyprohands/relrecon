"""Hash artifacts for recipes with no output.groups (Issue #97 no-op check).

Drives the real CLI, so source-order determinism (PR #87) engages exactly as
it does for a user. Run on this branch and on main; the digests must match.
Usage: .venv/bin/python tests/noop_hashes.py <out_dir> [runs]
"""

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# xlsx (creation timestamp) and _summary.md (wall-clock timings) are excluded
# from hashing: neither is byte-stable on any branch, by design.
RECIPES = [
    "tests/recipes/decision_record_test.yaml",
    "tests/recipes/merged_output_test.yaml",
    "tests/recipes/source_order_test.yaml",
    "config/recipes/multipop_comparison_rollup.yaml",
]


def run(recipe_path, out_dir):
    slug = Path(recipe_path).stem
    target = out_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"), "-m", "src",
            "--recipe", str(ROOT / recipe_path),
            "--data", str(ROOT / "data"),
            "--output", str(target / "data.csv"),
        ],
        cwd=ROOT, check=True, capture_output=True,
    )
    return target


if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    for i in range(runs):
        for recipe_path in RECIPES:
            target = run(recipe_path, out_dir / f"run{i + 1}")
            for artifact in sorted(target.glob("*")):
                if artifact.suffix == ".xlsx" or artifact.name.endswith("_summary.md"):
                    continue
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                print(f"{digest}  {target.name}/{artifact.name}")
