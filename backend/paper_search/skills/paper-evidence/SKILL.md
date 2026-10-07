---
name: paper-evidence
description: 定向读取摘要、网页和 PDF 并提取可引用证据
metadata:
  version: "1"
---

# 定向读取摘要、网页和 PDF 并提取可引用证据

先看检索片段，再读取上下文，最后补关键实验条件。保留页码、章节、否定结果和适用范围；不能从搜索摘要推断实验结论。已有证据可以跨 Worker 复用。PDF 证据任务不要求搜索查询或新增论文数量。每条发现引用工具生成的完整 evidence ID，原始内容保留在 Artifact。

下一步先明确缺口、预期收益与停止条件。使用 update_task、record_findings 和 submit_research 的当前版本语义。

具体例子按需读取 references/examples.md。
