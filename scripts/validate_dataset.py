from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dataset_validation import validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate process agent V1 dataset.")
    parser.add_argument("--root", default="data", help="Dataset root directory.")
    args = parser.parse_args()

    issues = validate_dataset(Path(args.root))
    if not issues:
        print("Dataset validation passed.")
        return 0

    for issue in issues:
        print(f"[{issue.severity}] {issue.case}: {issue.message}")
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
