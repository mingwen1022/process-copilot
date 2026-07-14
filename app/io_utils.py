from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from data.schema import ProcessDefinition


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: Path
    text: str


def read_raw_sources(case_dir: str | Path) -> list[SourceDocument]:
    raw_dir = Path(case_dir) / "raw_sources"
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"raw_sources directory not found: {raw_dir}")

    docs: list[SourceDocument] = []
    for path in sorted(raw_dir.glob("*.txt")):
        docs.append(SourceDocument(path=path, text=path.read_text(encoding="utf-8")))
    return docs


def compose_source_context(sources: list[SourceDocument]) -> str:
    parts: list[str] = []
    for source in sources:
        parts.append(f"## 来源文件：{source.path.name}\n\n{source.text.strip()}")
    return "\n\n---\n\n".join(parts)


def find_standard_json(case_dir: str | Path) -> Path:
    standard_dir = Path(case_dir) / "standard"
    candidates = [path for path in sorted(standard_dir.glob("*.json")) if is_process_definition_json(path)]
    if len(candidates) == 1:
        return candidates[0]

    target_candidates = [path for path in candidates if is_standard_target_json(path)]
    if len(target_candidates) == 1:
        return target_candidates[0]

    pure_candidates = [path for path in candidates if is_standard_process_json(path)]
    if len(pure_candidates) == 1:
        return pure_candidates[0]

    if len(candidates) != 1:
        raise FileNotFoundError(
            f"expected exactly one ProcessDefinition JSON under {standard_dir}, found {len(candidates)}"
        )
    return candidates[0]


def is_process_definition_json(path: str | Path) -> bool:
    return is_standard_process_json(path) or is_standard_target_json(path)


def is_standard_process_json(path: str | Path) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("meta"), dict)
        and isinstance(payload.get("form_fields"), list)
        and isinstance(payload.get("flow_nodes"), list)
    )


def is_standard_target_json(path: str | Path) -> bool:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    process = payload.get("deterministic_process_definition") if isinstance(payload, dict) else None
    return (
        isinstance(process, dict)
        and isinstance(process.get("meta"), dict)
        and isinstance(process.get("form_fields"), list)
        and isinstance(process.get("flow_nodes"), list)
    )


def load_standard_target(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if is_standard_target_json(path):
        return payload
    return {
        "deterministic_process_definition": payload,
        "clarification_targets": [],
        "accepted_optional_clarifications": [],
        "forbidden_clarifications": [],
        "guardrail_targets": [],
        "evidence_map": {},
        "eval_weights": {},
    }


def load_process_definition(path: str | Path) -> ProcessDefinition:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    data = payload.get("deterministic_process_definition", payload) if isinstance(payload, dict) else payload
    return ProcessDefinition.model_validate(data)


def write_process_json(process: ProcessDefinition, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(process.model_dump(mode="json"), ensure_ascii=False, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def write_json(payload: Any, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = _strip_json_fence(text.strip())
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM output did not contain a JSON object")

    candidate = stripped[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM output contained invalid JSON: {exc}") from exc


def _strip_json_fence(text: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else text
