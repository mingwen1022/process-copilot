"""飞书发布编排：取 ProcessDefinition → 确定性翻译 → 预览有损映射 / 真发到飞书。

翻译与客户端本身已实测可用（app.integrations.feishu）；这里只做"取定义→翻译→预览/推送"
的编排，供 UI 的「发布到飞书」页调用。凭证从项目根 .env 读（APP_ID/APP_SECRET），
进程未必加载过 .env，所以这里兜底加载一次；代码不打印/不返回 secret 明文。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.integrations.feishu import translate_to_feishu_approval
from app.integrations.feishu.client import FeishuClient, FeishuError
from data.schema import ProcessDefinition

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv_once(path: Path) -> None:
    """把 .env 里的键补进 os.environ（已存在的不覆盖）。只在缺凭证时兜底用。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _creds_ready() -> bool:
    app_id = os.environ.get("FEISHU_APP_ID") or os.environ.get("APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or os.environ.get("APP_SECRET")
    return bool(app_id and app_secret)


class FeishuPublishService:
    def __init__(self, slice1: Any, *, dotenv_path: Path | None = None) -> None:
        self._slice1 = slice1
        _load_dotenv_once(dotenv_path or (PROJECT_ROOT / ".env"))

    def _load_process(self, workflow_definition_id: str) -> ProcessDefinition:
        row = self._slice1.workflow_definition(workflow_definition_id)
        raw = json.loads(row["definition_json"])
        # 锚案例可能带包装；上架的 definition_json 是裸 ProcessDefinition——两种都收
        if isinstance(raw, dict) and "deterministic_process_definition" in raw:
            raw = raw["deterministic_process_definition"]
        return ProcessDefinition.model_validate(raw)

    @staticmethod
    def _resolve(translation: Any) -> tuple[str, list[str], int]:
        """把翻译结果里的 @i18n@ key 还原成真实文案，返回（审批名, 审批链, 字段数）。"""
        i18n = {
            t["key"]: t["value"]
            for res in translation.definition["i18n_resources"]
            for t in res["texts"]
        }
        name = i18n.get(translation.definition["approval_name"], translation.definition["approval_name"])
        chain = [
            i18n.get(n["name"], n["name"])
            for n in translation.definition["node_list"]
            if n.get("name")
        ]
        field_count = len(json.loads(translation.definition["form"]["form_content"]))
        return name, chain, field_count

    def preview(self, workflow_definition_id: str, *, name_override: str | None = None) -> dict[str, Any]:
        """只翻译、不真发：返回审批名/审批链/字段数/有损映射说明。免费、无副作用。"""
        process = self._load_process(workflow_definition_id)
        translation = translate_to_feishu_approval(process, name_override=name_override)
        name, chain, field_count = self._resolve(translation)
        return {
            "workflow_definition_id": workflow_definition_id,
            "process_name": process.meta.process_name,
            "approval_name": name,
            "node_chain": chain,
            "field_count": field_count,
            "notes": translation.notes,
            "linearized": translation.linearized,
            "creds_ready": _creds_ready(),
        }

    def push(self, workflow_definition_id: str, *, name_override: str | None = None) -> dict[str, Any]:
        """真发到飞书：创建审批定义，返回 approval_code。失败返回结构化错误、不抛。"""
        process = self._load_process(workflow_definition_id)
        translation = translate_to_feishu_approval(process, name_override=name_override)
        name, _, _ = self._resolve(translation)
        try:
            client = FeishuClient.from_env()
            approval_code = client.create_approval_definition(translation.definition)
        except FeishuError as exc:
            return {"pushed": False, "error": str(exc), "notes": translation.notes}
        return {
            "pushed": True,
            "approval_code": approval_code,
            "approval_name": name,
            "notes": translation.notes,
        }
