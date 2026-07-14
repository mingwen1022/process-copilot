"""演示"起始态"快照 / 一键切回（repeatable demo）。

背景：一场演示会做很多对话——改流程、生成诊断、运维订正、跑评测…这些会真实写进
runtime.db、analytics 的几个 json、以及评测历史，导致"改完问题就没法二次演示"。这里把
"哪些东西算演示状态"固定下来，提供两个动作：

- freeze()：把当前这几样拷进 _baseline/，定为起始态（一次性，或想更新起点时再点）。
- restore()：把 _baseline/ 覆盖回去 + 重建内存态，回到起始态（可反复）。

设计要点：
1. 需要快照的"耐重启状态"就这几样——runtime.db（设计会话/材料/消息/发布状态）
   + analytics 的 diagnosis_history.json / custom_metrics.json + runs/eval_runs/（评测历史）。
   其余（合成数据、语料、org、agent 轨迹）演示中只读不写，不进快照。
2. 内存态（InsightStore、ops/todo 在办实例）本来就在进程重启时归零——restore 里
   直接调 rebuild_in_memory 回调重跑一遍启动播种即可，不用逐个对象手术。
3. 评测历史(runs/eval_runs)与 db 里的流程定义共用 id：跑一次评测会同时写两边。若只回滚
   db、不回滚评测历史，就会留下"评测指向已不存在的流程"的孤儿——所以两者必须一起快照。
4. restore 必须能"删掉基线里没有、演示中被创建出来的东西"（新诊断历史 / 新转正指标 /
   新评测运行），否则回不到真正的起点——所以按基线清单逐项 copy-or-delete。目录用整体
   替换（rmtree+copytree）天然满足这点：不在基线里的子项被整体覆盖掉。
5. db 没开 WAL、无 -wal/-shm 伴生文件；服务都用 with _connect() 短连接，restore
   这一刻没有长连接持有 db 文件，直接覆盖安全。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.runtime.models import now_iso

# analytics 目录下随演示写入的可变文件
_ANALYTICS_MUTABLE_FILES = ("diagnosis_history.json", "custom_metrics.json")


class DemoStateService:
    def __init__(
        self,
        *,
        project_root: Path,
        db_path: Path,
        analytics_dirs: list[Path],
        eval_runs_dir: Path,
        rebuild_in_memory: Callable[[], None],
        baseline_dir: Path | None = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        # 全部规范成绝对路径——db_path 可能是相对的（runtime.store.db_path），若混着相对/绝对，
        # 后面 relative_to(root) 会抛 ValueError。统一到绝对，快照/还原/清单都稳。
        self._db_path = self._abs(db_path)
        self._analytics_dirs = [self._abs(d) for d in analytics_dirs]
        self._eval_runs_dir = self._abs(eval_runs_dir)
        self._rebuild_in_memory = rebuild_in_memory
        self._baseline_dir = self._abs(baseline_dir) if baseline_dir else self._db_path.parent / "_baseline"
        self._meta_path = self._baseline_dir / "baseline_meta.json"

    def _abs(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self._root / p)

    # ── 被跟踪的可变项（文件或目录，绝对路径）──────────
    def _tracked_paths(self) -> list[Path]:
        paths = [self._db_path]  # runtime.db（文件）
        for adir in self._analytics_dirs:
            for name in _ANALYTICS_MUTABLE_FILES:
                paths.append(adir / name)  # 诊断历史 / 自定义指标（文件）
        paths.append(self._eval_runs_dir)  # 评测运行历史（目录，整体快照）
        return paths

    def _baseline_slot(self, live_path: Path) -> Path:
        """基线目录里对应某个 live 项的落点——用相对 project_root 的路径保结构、避免重名。"""
        try:
            rel = live_path.relative_to(self._root)
        except ValueError:
            rel = Path(live_path.name)
        return self._baseline_dir / rel

    @staticmethod
    def _copy_in(src: Path, dst: Path) -> None:
        """把 src（文件或目录）拷到 dst，dst 预先清干净——目录用 copytree，文件用 copy2。"""
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    # ── 状态 ─────────────────────────────────────
    def status(self) -> dict[str, Any]:
        exists = self._meta_path.exists()
        frozen_at = None
        if exists:
            try:
                frozen_at = json.loads(self._meta_path.read_text(encoding="utf-8")).get("frozen_at")
            except (OSError, ValueError):
                frozen_at = None
        return {"has_baseline": exists, "frozen_at": frozen_at}

    # ── 定起始态 ─────────────────────────────────
    def freeze(self) -> dict[str, Any]:
        if self._baseline_dir.exists():
            shutil.rmtree(self._baseline_dir)  # 全量重建，避免上次基线的残留混进来
        self._baseline_dir.mkdir(parents=True, exist_ok=True)

        tracked: list[str] = []
        for live in self._tracked_paths():
            if not live.exists():
                continue  # 当前不存在的项（如还没生成的 custom_metrics.json）：基线不放，还原时据此删
            self._copy_in(live, self._baseline_slot(live))
            tracked.append(str(live.relative_to(self._root)))

        frozen_at = now_iso()
        self._meta_path.write_text(
            json.dumps({"frozen_at": frozen_at, "tracked": tracked}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"has_baseline": True, "frozen_at": frozen_at, "tracked_count": len(tracked)}

    # ── 一键切回 ─────────────────────────────────
    def restore(self) -> dict[str, Any]:
        if not self._meta_path.exists():
            raise RuntimeError("还没有设定起始态，请先点「设为起始态」。")

        restored = 0
        removed = 0
        for live in self._tracked_paths():
            slot = self._baseline_slot(live)
            if slot.exists():
                self._copy_in(slot, live)  # 基线里有 → 覆盖回去（目录整体替换）
                restored += 1
            elif live.exists():
                self._remove(live)  # 基线里没有、演示中被创建出来 → 删掉，才算真回到起点
                removed += 1

        # 文件回位后重建内存态（清 InsightStore + 重造在办实例 + 重跑启动播种）
        self._rebuild_in_memory()
        return {"restored": True, "items_restored": restored, "items_removed": removed, **self.status()}
