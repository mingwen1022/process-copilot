from __future__ import annotations

import json

import pytest

from app.api.demo_state import DemoStateService


def _svc(tmp_path, rebuilt: list) -> DemoStateService:
    """一套自足的演示态：db 文件 + 一个 analytics 目录 + eval_runs 目录，
    rebuild 回调只记一次调用，用来断言 restore 触发了内存重建。"""
    root = tmp_path
    db = root / "data/runtime/runtime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("DB-V0", encoding="utf-8")
    analytics = root / "data/analytics/leave"
    analytics.mkdir(parents=True, exist_ok=True)
    eval_runs = root / "runs/eval_runs"
    (eval_runs / "leave").mkdir(parents=True, exist_ok=True)
    (eval_runs / "leave" / "index.json").write_text("[V0]", encoding="utf-8")
    return DemoStateService(
        project_root=root,
        db_path=db,
        analytics_dirs=[analytics],
        eval_runs_dir=eval_runs,
        rebuild_in_memory=lambda: rebuilt.append(1),
    )


def test_status_empty_before_freeze(tmp_path) -> None:
    svc = _svc(tmp_path, [])
    assert svc.status() == {"has_baseline": False, "frozen_at": None}


def test_restore_without_baseline_raises(tmp_path) -> None:
    svc = _svc(tmp_path, [])
    with pytest.raises(RuntimeError):
        svc.restore()


def test_freeze_then_restore_reverts_db_edit(tmp_path) -> None:
    rebuilt: list = []
    svc = _svc(tmp_path, rebuilt)
    svc.freeze()
    assert svc.status()["has_baseline"] is True

    (tmp_path / "data/runtime/runtime.db").write_text("DB-DIRTY", encoding="utf-8")
    result = svc.restore()

    assert (tmp_path / "data/runtime/runtime.db").read_text() == "DB-V0"  # 覆盖回起点
    assert rebuilt == [1]  # 触发了内存重建
    assert result["restored"] is True


def test_restore_deletes_file_created_after_baseline(tmp_path) -> None:
    """基线时 diagnosis_history.json 不存在；演示中被创建 → restore 必须删掉，才算真回起点。"""
    svc = _svc(tmp_path, [])
    svc.freeze()  # analytics 目录此刻是空的，该文件不进基线

    dirty = tmp_path / "data/analytics/leave/diagnosis_history.json"
    dirty.write_text("[demo-generated]", encoding="utf-8")
    result = svc.restore()

    assert not dirty.exists()
    assert result["items_removed"] >= 1


def test_restore_prunes_eval_runs_added_during_demo(tmp_path) -> None:
    """跑一次新评测会在 eval_runs 下多出一个 case 目录 + 改动 index；目录整体替换后
    应回到基线内容（新目录被清、index 复原），不留孤儿。"""
    svc = _svc(tmp_path, [])
    svc.freeze()

    eval_runs = tmp_path / "runs/eval_runs"
    (eval_runs / "expense").mkdir(parents=True, exist_ok=True)
    (eval_runs / "expense" / "index.json").write_text("[NEW-RUN]", encoding="utf-8")
    (eval_runs / "leave" / "index.json").write_text("[V1-MUTATED]", encoding="utf-8")

    svc.restore()

    assert not (eval_runs / "expense").exists()  # 演示中新增的评测目录被清
    assert (eval_runs / "leave" / "index.json").read_text() == "[V0]"  # 原有的复原


def test_freeze_is_idempotent_and_refreshes(tmp_path) -> None:
    svc = _svc(tmp_path, [])
    first = svc.freeze()
    # 改一版再 freeze：基线应更新为新内容，旧残留不留
    (tmp_path / "data/runtime/runtime.db").write_text("DB-V1", encoding="utf-8")
    svc.freeze()
    (tmp_path / "data/runtime/runtime.db").write_text("DB-DIRTY", encoding="utf-8")
    svc.restore()
    assert (tmp_path / "data/runtime/runtime.db").read_text() == "DB-V1"
    assert first["has_baseline"] is True


def test_baseline_meta_records_tracked_manifest(tmp_path) -> None:
    svc = _svc(tmp_path, [])
    svc.freeze()
    meta = json.loads((tmp_path / "data/runtime/_baseline/baseline_meta.json").read_text(encoding="utf-8"))
    assert "frozen_at" in meta
    # db + eval_runs 存在→入清单；空的 analytics 文件不入
    tracked = meta["tracked"]
    assert any("runtime.db" in t for t in tracked)
    assert any("eval_runs" in t for t in tracked)
