"""把设计 Agent 产出的 ProcessDefinition 真发到飞书审批（创建审批定义）。

链路：读 .env → 翻译成飞书定义 → 拿 tenant_access_token → POST 创建 → 打印 approval_code。

用法：
  uv run python scripts/feishu_push_approval.py            # 真发（默认请假锚案例）
  uv run python scripts/feishu_push_approval.py --dry-run  # 只翻译、打印 payload，不发
  uv run python scripts/feishu_push_approval.py --source path/to/def.json --name "xxx"

凭证从项目根 .env 读（APP_ID/APP_SECRET）；脚本不打印 secret。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.feishu import translate_to_feishu_approval  # noqa: E402
from app.integrations.feishu.client import FeishuClient, FeishuError  # noqa: E402
from data.schema import ProcessDefinition  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data/cases/leave_request/standard/target.json"
DEFAULT_NAME = "员工请假申请流程（AI设计）"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load_process(source: Path) -> ProcessDefinition:
    raw = json.loads(source.read_text(encoding="utf-8"))
    # 锚案例是带包装的（deterministic_process_definition）；也支持直接给裸 ProcessDefinition
    if "deterministic_process_definition" in raw:
        raw = raw["deterministic_process_definition"]
    return ProcessDefinition.model_validate(raw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--name", type=str, default=DEFAULT_NAME)
    parser.add_argument("--dry-run", action="store_true", help="只翻译打印，不真发")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    process = load_process(args.source)
    result = translate_to_feishu_approval(process, name_override=args.name)

    print(f"流程：{process.meta.process_name} → 飞书名称「{args.name}」")
    print(f"审批链：{' → '.join(n['id'] for n in result.definition['node_list'])}")
    if result.notes:
        print(f"翻译说明（{len(result.notes)} 条）：")
        for note in result.notes:
            print(f"  · {note}")

    if args.dry_run:
        print("\n[dry-run] 定义体（form_content 已内联）：")
        print(json.dumps(result.definition, ensure_ascii=False, indent=2))
        return

    try:
        client = FeishuClient.from_env()
        approval_code = client.create_approval_definition(result.definition)
    except FeishuError as exc:
        print(f"\n❌ {exc}")
        sys.exit(1)

    print(f"\n✅ 创建成功！approval_code = {approval_code}")
    print("去飞书客户端「审批 → 发起申请」里搜这个名字，应该能看到并发起。")


if __name__ == "__main__":
    main()
