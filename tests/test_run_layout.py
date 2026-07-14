from __future__ import annotations

import json
from pathlib import Path

from app.run_layout import ensure_run_layout, eval_dir, logs_dir, product_dir, write_run_meta
from scripts.archive_runs import archive_runs


def test_ensure_run_layout_creates_product_eval_and_logs(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "active" / "20260622_010203_case"

    layout = ensure_run_layout(run_dir)
    write_run_meta(run_dir, case_id="case", case_dir="data/case", status="running")

    assert layout["product"] == product_dir(run_dir)
    assert layout["eval"] == eval_dir(run_dir)
    assert layout["logs"] == logs_dir(run_dir)
    assert product_dir(run_dir).is_dir()
    assert eval_dir(run_dir).is_dir()
    assert logs_dir(run_dir).is_dir()
    assert (logs_dir(run_dir) / "llm_calls").is_dir()

    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["layout_version"] == "v2"
    assert meta["case_id"] == "case"
    assert meta["status"] == "running"


def test_archive_runs_moves_old_entries_and_keeps_active_legacy(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    (runs_dir / "active").mkdir(parents=True)
    (runs_dir / "legacy").mkdir()
    (runs_dir / "old_run").mkdir()
    (runs_dir / "old_run" / "file.txt").write_text("old", encoding="utf-8")
    (runs_dir / "old_file.txt").write_text("file", encoding="utf-8")

    manifest = archive_runs(runs_dir, label="test")

    archive_root = Path(manifest["archive_dir"])
    assert archive_root.is_dir()
    assert (archive_root / "old_run" / "file.txt").read_text(encoding="utf-8") == "old"
    assert (archive_root / "old_file.txt").read_text(encoding="utf-8") == "file"
    assert (archive_root / "legacy_manifest.json").exists()
    assert not (runs_dir / "old_run").exists()
    assert not (runs_dir / "old_file.txt").exists()
    assert (runs_dir / "active").is_dir()
    assert (runs_dir / "legacy").is_dir()

    saved = json.loads((archive_root / "legacy_manifest.json").read_text(encoding="utf-8"))
    assert saved["archive_dir"] == saved["archive_root"]
    assert len(saved["moved"]) == 2
    assert {Path(item["path"]).name for item in saved["skipped"]} == {"active", "legacy"}
