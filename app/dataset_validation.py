from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.io_utils import is_process_definition_json, load_standard_target
from data.schema import ProcessDefinition


@dataclass(frozen=True, slots=True)
class DatasetIssue:
    case: str
    severity: str
    message: str


def validate_dataset(root: str | Path = "data") -> list[DatasetIssue]:
    data_root = Path(root)
    issues: list[DatasetIssue] = []
    for case_dir in sorted(path for path in data_root.iterdir() if _is_case_dir(path)):
        issues.extend(validate_case(case_dir))
    return issues


def _is_case_dir(path: Path) -> bool:
    return path.is_dir() and path.name != "schema" and not path.name.startswith("__") and (path / "raw_sources").is_dir()


def validate_case(case_dir: str | Path) -> list[DatasetIssue]:
    case_path = Path(case_dir)
    issues: list[DatasetIssue] = []
    case = case_path.name

    source_dir = _source_dir(case_path)
    raw_files = _source_files(source_dir)
    if len(raw_files) < 5:
        issues.append(DatasetIssue(case, "error", f"expected at least 5 source files under {source_dir.name}, found {len(raw_files)}"))

    standard_dir = case_path / "standard"
    json_files = [path for path in sorted(standard_dir.glob("*.json")) if is_process_definition_json(path)]
    if len(json_files) != 1:
        issues.append(DatasetIssue(case, "error", f"expected 1 standard ProcessDefinition JSON, found {len(json_files)}"))
        return issues

    xlsx_files = [path for path in sorted(standard_dir.glob("*.xlsx")) if not path.name.startswith("~$")]
    if len(xlsx_files) != 1:
        issues.append(DatasetIssue(case, "error", f"expected 1 target Excel file, found {len(xlsx_files)}"))

    try:
        target = load_standard_target(json_files[0])
        process = ProcessDefinition.model_validate(target["deterministic_process_definition"])
    except (json.JSONDecodeError, ValidationError) as exc:
        issues.append(DatasetIssue(case, "error", f"standard JSON validation failed: {exc}"))
        return issues

    issues.extend(_validate_target_sections(case, target))
    issues.extend(_validate_paths(case, process))
    issues.extend(_validate_field_sequence(case, process))
    issues.extend(_validate_expense_boundary(case_path, process))
    return issues


def _source_dir(case_path: Path) -> Path:
    raw_sources2 = case_path / "raw_sources2"
    if raw_sources2.is_dir():
        return raw_sources2
    return case_path / "raw_sources"


def _source_files(source_dir: Path) -> list[Path]:
    if not source_dir.is_dir():
        return []
    return [
        path
        for path in sorted(source_dir.iterdir())
        if path.is_file() and path.name not in {"source_manifest.json", ".DS_Store"} and not path.name.startswith(".")
    ]


def _validate_target_sections(case: str, target: dict[str, object]) -> list[DatasetIssue]:
    issues: list[DatasetIssue] = []
    for key in [
        "deterministic_process_definition",
        "clarification_targets",
        "accepted_optional_clarifications",
        "forbidden_clarifications",
        "guardrail_targets",
        "evidence_map",
        "eval_weights",
    ]:
        if key not in target:
            issues.append(DatasetIssue(case, "error", f"target JSON missing section: {key}"))
    return issues


def _validate_paths(case: str, process: ProcessDefinition) -> list[DatasetIssue]:
    issues: list[DatasetIssue] = []
    node_ids = {node.node_id for node in process.flow_nodes}
    allowed = node_ids | {"END", "DRAFT"}
    for node in process.flow_nodes:
        for path in node.submit_paths:
            if path.target_node_id not in allowed:
                issues.append(
                    DatasetIssue(
                        case,
                        "error",
                        f"path target not found: {node.node_id}/{path.path_name} -> {path.target_node_id}",
                    )
                )
    return issues


def _validate_field_sequence(case: str, process: ProcessDefinition) -> list[DatasetIssue]:
    seqs = [field.seq for field in process.form_fields]
    expected = list(range(1, len(process.form_fields) + 1))
    if seqs != expected:
        return [DatasetIssue(case, "error", f"form field seq should be {expected}, found {seqs}")]
    return []


def _validate_expense_boundary(case_path: Path, process: ProcessDefinition) -> list[DatasetIssue]:
    if process.meta.process_id != "EXPENSE-001":
        return []
    text = json.dumps(process.model_dump(mode="json"), ensure_ascii=False)
    issues: list[DatasetIssue] = []
    if "≤ 5万" in text or "＞ 5万" in text:
        issues.append(DatasetIssue(case_path.name, "error", "expense process uses stale boundary labels; expected ＜5万 / ≥5万"))
    if "＜5万" not in text or "≥5万" not in text:
        issues.append(DatasetIssue(case_path.name, "error", "expense process missing expected boundary labels: ＜5万 / ≥5万"))
    return issues
