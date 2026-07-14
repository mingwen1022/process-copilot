"""流程参与者 AI 副驾（模块4）：运维异常处理 / 发起问答 / 在办任务协助。

三个副驾共享同一层抽象——实例接口（Instance Port，见 data/schema/instance_schema.py）
——只依赖 InstanceContext/InstanceAction，不直接依赖任何具体运行时。当前只有 Mock
实现：diagnostics.py 用现有的 condition_eval + resolve_node_assignees 现算出快照，
不接冻结的 OA 引擎，也不接飞书。

设计文档：doc/流程参与者副驾-设计.md
"""
