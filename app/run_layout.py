from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.io_utils import write_json

RUN_LAYOUT_VERSION = "v2"


def product_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "product"


def eval_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "eval"


def logs_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "logs"


def node_log_dir(run_dir: str | Path, node_name: str) -> Path:
    return logs_dir(run_dir) / node_name


def source_ingestion_dir(run_dir: str | Path) -> Path:
    return logs_dir(run_dir) / "source_ingestion"


def legacy_eval_dir(run_dir: str | Path) -> Path:
    return logs_dir(run_dir) / "legacy_eval"


def ensure_run_layout(run_dir: str | Path) -> dict[str, Path]:
    root = Path(run_dir)
    paths = {
        "root": root,
        "product": product_dir(root),
        "eval": eval_dir(root),
        "logs": logs_dir(root),
        "llm_calls": logs_dir(root) / "llm_calls",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def write_run_meta(
    run_dir: str | Path,
    *,
    case_id: str | None = None,
    case_dir: str | Path | None = None,
    status: str = "running",
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "layout_version": RUN_LAYOUT_VERSION,
        "status": status,
        "case_id": case_id,
        "case_dir": str(case_dir) if case_dir is not None else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    return write_json(payload, Path(run_dir) / "run_meta.json")
