"""飞书审批 API 薄客户端（标准库直连，不引入 lark-oapi 重依赖）。

只做两件事：换 tenant_access_token、创建审批定义。风格与项目一致——确定性 HTTP 调用。
凭证从环境变量读（APP_ID/APP_SECRET 或 FEISHU_APP_ID/FEISHU_APP_SECRET），代码不碰明文。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_URL = f"{_BASE}/auth/v3/tenant_access_token/internal"
_CREATE_APPROVAL_URL = f"{_BASE}/approval/v4/approvals"


class FeishuError(RuntimeError):
    """飞书返回 code != 0，或 HTTP 层异常。"""


def _post_json(url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 飞书把真正的 code/msg 放在 4xx 的响应体里，urllib 默认吞掉——读出来
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise FeishuError(f"HTTP {exc.code}：{raw[:500]}") from exc


@dataclass
class FeishuClient:
    app_id: str
    app_secret: str
    _token: str | None = None

    @classmethod
    def from_env(cls) -> "FeishuClient":
        app_id = os.environ.get("FEISHU_APP_ID") or os.environ.get("APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET") or os.environ.get("APP_SECRET")
        if not app_id or not app_secret:
            raise FeishuError("环境变量缺少 APP_ID / APP_SECRET（或 FEISHU_ 前缀）")
        return cls(app_id=app_id, app_secret=app_secret)

    def tenant_access_token(self, *, refresh: bool = False) -> str:
        if self._token and not refresh:
            return self._token
        payload = _post_json(_TOKEN_URL, {"app_id": self.app_id, "app_secret": self.app_secret})
        if payload.get("code") != 0:
            raise FeishuError(f"换 token 失败：code={payload.get('code')} msg={payload.get('msg')!r}")
        self._token = payload["tenant_access_token"]
        return self._token

    def create_approval_definition(self, definition: dict[str, Any]) -> str:
        """创建审批定义，返回 approval_code（该流程在飞书里的唯一编号）。"""
        token = self.tenant_access_token()
        payload = _post_json(
            _CREATE_APPROVAL_URL, definition, headers={"Authorization": f"Bearer {token}"}
        )
        if payload.get("code") != 0:
            raise FeishuError(f"创建审批定义失败：code={payload.get('code')} msg={payload.get('msg')!r}")
        return payload["data"]["approval_code"]

    def get_approval_definition(self, approval_code: str, *, locale: str = "zh-CN") -> dict[str, Any]:
        """查看指定审批定义（读回真实结构：名称/状态/表单/节点/可见范围）。"""
        token = self.tenant_access_token()
        url = f"{_CREATE_APPROVAL_URL}/{approval_code}?locale={locale}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise FeishuError(f"HTTP {exc.code}：{raw[:500]}") from exc
        if payload.get("code") != 0:
            raise FeishuError(f"读取审批定义失败：code={payload.get('code')} msg={payload.get('msg')!r}")
        return payload["data"]
