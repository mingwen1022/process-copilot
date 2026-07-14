from __future__ import annotations

import shutil
from pathlib import Path

from app.workflows.process_v1 import source_context_builder, source_file_loader


def test_uploaded_multiformat_sources_are_parsed_without_binary_prompt_content(tmp_path: Path) -> None:
    fixture_dir = Path("data/cases/EOA140_subsidiary_major_matter/raw_sources")
    case_dir = tmp_path / "case"
    upload_dir = case_dir / "uploaded_sources"
    out_dir = tmp_path / "run"
    upload_dir.mkdir(parents=True)
    sources = []
    for index, fixture in enumerate(sorted(path for path in fixture_dir.iterdir() if path.name != "source_manifest.json"), start=1):
        target = upload_dir / fixture.name
        shutil.copyfile(fixture, target)
        sources.append(
            {
                "source_id": f"upload_{index:03d}",
                "title": fixture.name,
                "original_filename": fixture.name,
                "source_type": fixture.suffix.removeprefix(".") or "file",
                "mime_type": None,
                "size_bytes": target.stat().st_size,
                "storage_path": str(target),
            }
        )
    (case_dir / "uploaded_source_manifest.json").write_text(
        '{"sources":['
        + ",".join(
            [
                "{"
                + f'"source_id":"{source["source_id"]}",'
                + f'"title":"{source["title"]}",'
                + f'"original_filename":"{source["original_filename"]}",'
                + f'"source_type":"{source["source_type"]}",'
                + f'"size_bytes":{source["size_bytes"]},'
                + f'"storage_path":"{source["storage_path"]}"'
                + "}"
                for source in sources
            ]
        )
        + "]}",
        encoding="utf-8",
    )

    loaded = source_file_loader(
        {
            "case_dir": str(case_dir),
            "out_dir": str(out_dir),
            "validation_retry_count": 0,
            "max_validation_retries": 1,
        }
    )
    built = source_context_builder(loaded)

    expected_source_count = len([path for path in fixture_dir.iterdir() if path.is_file() and path.name != "source_manifest.json"])
    assert len(loaded["source_file_manifest"]) == expected_source_count
    assert any(chunk["source_type"] == "docx" and "子公司" in chunk["text"] for chunk in loaded["source_chunks"])
    assert any(chunk["source_type"] == "html" and "子公司" in chunk["text"] for chunk in loaded["source_chunks"])
    assert any(chunk["source_type"] == "xlsx" and "处理人" in chunk["text"] for chunk in loaded["source_chunks"])
    assert any(chunk["source_type"] == "txt" and "战略发展部" in chunk["text"] for chunk in loaded["source_chunks"])
    assert any("EOA140" in chunk["text"] for chunk in loaded["source_chunks"])
    assert any("环节" in chunk["text"] for chunk in loaded["source_chunks"])

    warning_types = {warning["warning_type"] for warning in loaded["ingestion_warnings"]}
    assert "requires_vision_ocr" in warning_types
    assert "requires_ocr" in warning_types

    source_context = built["source_context"]
    assert "PK\x03\x04" not in source_context
    assert "PNG IHDR" not in source_context
    assert "%PDF-1.4" not in source_context
    assert built["context_budget_report"]["selected_chunk_count"] == len(built["selected_seed_chunks"])


def test_raw_source_fallback_ignores_internal_manifest_files(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    raw_dir = case_dir / "raw_sources"
    out_dir = tmp_path / "run"
    raw_dir.mkdir(parents=True)
    (raw_dir / "01_需求说明.txt").write_text("员工请假申请，需要部门主管审批。", encoding="utf-8")
    (raw_dir / "source_manifest.json").write_text('{"internal": true}', encoding="utf-8")

    loaded = source_file_loader(
        {
            "case_dir": str(case_dir),
            "out_dir": str(out_dir),
            "validation_retry_count": 0,
            "max_validation_retries": 1,
        }
    )

    assert [item["file_name"] for item in loaded["source_file_manifest"]] == ["01_需求说明.txt"]
    assert loaded["source_package_ref"]["source_count"] == 1


def test_image_source_is_vision_transcribed_when_transcriber_injected(tmp_path: Path) -> None:
    from PIL import Image

    case_dir = tmp_path / "case"
    raw_dir = case_dir / "raw_sources"
    raw_dir.mkdir(parents=True)
    (raw_dir / "01_说明.txt").write_text("员工请假申请，部门主管审批。", encoding="utf-8")
    Image.new("RGB", (60, 40), "white").save(raw_dir / "02_群聊截图.png")

    def fake_transcriber(path: Path, source_type: str) -> str:
        return "周敏·HR 10:02 病假证明先留待确认。"

    state = source_file_loader(
        {"case_dir": str(case_dir), "out_dir": str(tmp_path / "run"), "validation_retry_count": 0, "max_validation_retries": 1},
        image_transcriber=fake_transcriber,
    )
    warning_types = {w["warning_type"] for w in state["ingestion_warnings"]}
    assert "vision_transcribed" in warning_types
    assert "requires_vision_ocr" not in warning_types
    image_chunks = [c for c in state["source_chunks"] if c["source_type"] == "image"]
    assert image_chunks and "病假证明先留待确认" in image_chunks[0]["text"]
    assert image_chunks[0]["metadata"].get("vision_transcribed") is True


def test_image_source_without_transcriber_stays_warning(tmp_path: Path) -> None:
    from PIL import Image

    case_dir = tmp_path / "case"
    raw_dir = case_dir / "raw_sources"
    raw_dir.mkdir(parents=True)
    Image.new("RGB", (60, 40), "white").save(raw_dir / "01_截图.png")

    state = source_file_loader(
        {"case_dir": str(case_dir), "out_dir": str(tmp_path / "run"), "validation_retry_count": 0, "max_validation_retries": 1},
    )
    warning_types = {w["warning_type"] for w in state["ingestion_warnings"]}
    assert "requires_vision_ocr" in warning_types
    assert not [c for c in state["source_chunks"] if c["source_type"] == "image"]
