"""反哺闭环 · 运行洞察层（模块8）。

InsightStore：运行侧确定性写入、设计侧读取的共享结构化存储。详见 doc/反哺闭环-设计.md。
"""

from app.insights.store import InsightStore, channel_for

__all__ = ["InsightStore", "channel_for"]
