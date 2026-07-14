from __future__ import annotations

from pathlib import Path
from typing import Any

from app.io_utils import find_standard_json, load_process_definition
from app.runtime.store import SQLiteRuntimeStore
from data.schema import ProcessDefinition


DATA_ROOT = Path("data")


def import_standard_processes(
    store: SQLiteRuntimeStore,
    *,
    data_root: str | Path = DATA_ROOT,
) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    for case_dir in sorted(Path(data_root).glob("0*")):
        standard_dir = case_dir / "standard"
        if not standard_dir.exists():
            continue
        if not sorted(standard_dir.glob("*.json")):
            continue
        process = load_process_definition(find_standard_json(case_dir))
        process_key = case_dir.name
        artifact_paths = _artifact_paths(standard_dir)
        store.save_process_definition(
            process_key,
            process,
            source_type="standard",
            source_case_dir=str(case_dir),
            artifact_paths=artifact_paths,
        )
        imported.append(process_summary(process_key, process, source_type="standard", artifact_paths=artifact_paths))
    return imported


def process_summary(
    process_key: str,
    process: ProcessDefinition,
    *,
    source_type: str,
    artifact_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "process_key": process_key,
        "process_id": process.meta.process_id,
        "process_name": process.meta.process_name,
        "version": process.meta.version,
        "responsible_dept": process.meta.responsible_dept,
        "source_type": source_type,
        "field_count": len(process.form_fields),
        "node_count": len(process.flow_nodes),
        "path_count": sum(len(node.submit_paths) for node in process.flow_nodes),
        "artifact_paths": artifact_paths or {},
    }


def _artifact_paths(standard_dir: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for path in sorted(standard_dir.iterdir()):
        if path.suffix == ".xlsx":
            paths["xlsx"] = str(path)
        elif path.suffix == ".drawio":
            paths["drawio"] = str(path)
        elif path.suffix == ".mermaid":
            paths["mermaid"] = str(path)
    return paths
