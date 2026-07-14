# 流程设计 Agent —— AI 流程全生命周期闭环

> 把 3 年流程治理工作做成一个 **"AI 设计流程 → 上线流转 → 运行数据提效/堵点分析 → 优化建议反哺设计"** 的闭环 agent 产品。
>
> 主线文档：[`doc/产品功能全景.md`](doc/产品功能全景.md)（按功能模块的设计/现有/要做全景）

## 定位

它**不是 OA/BPM 平台**，而是一个 AI 流程全生命周期闭环。运行时只作为"产生运行数据的底座"，真正深做的是两端 AI：**设计侧（含规则 RAG + 评测）** 和 **运营分析侧**。

## 架构（4 模块 + 闭环）

```
        ┌──────────── 规则知识库 (RAG) ────────────┐
        │ 公司/系统级制度 · 流程要素规范 · 模板     │
        └───────────────┬───────────────────────────┘
                        │ 检索"适用规则"作为设计约束 + 合规评测依据
   多源需求 ──▶ ① 设计 Agent ──▶ 标准流程定义(JSON)
                        │              │ 评测：要素级准确率(对 gold) + 规则合规率(无参照)
        对话式修改 ⟲────┘              ▼
                            ② 薄运行时(happy path) ──▶ 结构化运行事件日志
                                     │   (+ 合成事件日志生成器批量造数据)
                                     ▼
                            ③ 运营分析 Agent ──▶ 效能指标 + 堵点 + 优化建议
                                     │
                                     └────▶ ④ 优化建议反哺 ① 重新设计  ⟲ 闭环
```

| 模块 | 定位 | 状态 |
|---|---|---|
| **M1 设计 Agent + 规则 RAG** | 护城河①，深做 | 设计 pipeline 成熟；规则 RAG 待建 |
| **M2 薄运行时** | 数据底座，**薄做（冻结扩展）** | engine + API + SPA 可跑通 happy path |
| **M3 运营分析 Agent** | 护城河②，深做 | 待建 |
| **M4 闭环反馈** | headline，轻量 | 待建 |

> ⚠️ 运行时（`app/runtime`、`app/api/slice1_service.py`、`app/static/index.html`）是**"薄运行时 demo"，已冻结扩展**，不做平台化功能。详见北极星文档 §5。

## 设计原则

**确定性的归确定性，LLM 的归 LLM。** 评测 = 确定性打分(对 gold) + 无参照检查(规则/judge)；编辑 = typed 工具 + 校验 + LLM 解析意图；分析 = 确定性算指标 + LLM 归因/建议。

## 运行

设计 pipeline（多源需求 → 标准流程定义 JSON）：

```bash
uv run python -m app.workflows.process_v1 --case data/06_subsidiary_major_matter --out runs/06_subsidiary
```

默认在 `--out` 后追加时间戳；加 `--no-timestamp` 可复用固定目录。

校验数据集 / 组织知识：

```bash
uv run python scripts/validate_dataset.py
uv run python scripts/validate_org_knowledge.py
```

测试：

```bash
uv run pytest
```

## Bedrock 配置

复制 `.env.example` 为 `.env`，填写 `AWS_BEARER_TOKEN_BEDROCK`。实现使用 LangChain AWS 的 `ChatBedrockConverse`，只需单个 Bedrock API key。

## 目录结构

```
app/        设计 pipeline / runtime engine / eval / reporting / API
data/       6 个真实流程案例 + knowledge(规则) + runtime 产物(gitignore)
doc/        北极星文档 + architecture / references / archive(历史)
流程样例库/  真实 EOA 流程 xlsx 样例
scripts/    数据校验 / run 归档脚本
tests/      pytest 用例
```

详细仓库说明见 [`doc/README.md`](doc/README.md)。
