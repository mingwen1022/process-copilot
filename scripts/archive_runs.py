from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def archive_runs(runs_dir: Path, *, label: str = "before_output_format_v2", dry_run: bool = False) -> dict:
    runs_dir = runs_dir.resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_root = runs_dir / "legacy" / f"{timestamp}_{label}"
    active_dir = runs_dir / "active"
    manifest = {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "runs_dir": str(runs_dir),
        "archive_root": str(archive_root),
        "archive_dir": str(archive_root),
        "dry_run": dry_run,
        "moved": [],
        "skipped": [],
    }

    runs_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)

    for path in sorted(runs_dir.iterdir(), key=lambda item: item.name):
        if path.name in {"legacy", "active"}:
            manifest["skipped"].append({"path": str(path), "reason": "reserved run layout directory"})
            continue
        destination = archive_root / path.name
        manifest["moved"].append({"from": str(path), "to": str(destination)})
        if dry_run:
            continue
        if destination.exists():
            raise FileExistsError(f"archive destination already exists: {destination}")
        shutil.move(str(path), str(destination))

    manifest_path = archive_root / "legacy_manifest.json"
    if not dry_run:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive old runs into runs/legacy/<timestamp>_before_output_format_v2.")
    parser.add_argument("--runs-dir", default="runs", help="Runs directory. Defaults to ./runs.")
    parser.add_argument("--label", default="before_output_format_v2", help="Archive label suffix.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned moves without moving files.")
    args = parser.parse_args()

    manifest = archive_runs(Path(args.runs_dir), label=args.label, dry_run=args.dry_run)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
