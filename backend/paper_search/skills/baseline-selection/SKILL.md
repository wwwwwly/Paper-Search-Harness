---
name: baseline-selection
description: 选择可对齐的实验基线与复现条件
metadata:
  version: "1"
---

# 选择可对齐的实验基线与复现条件

识别任务、数据、指标、基础模型、训练/推理条件和资源限制。记录路线和 aligned/conditional/incompatible/unknown。每个用户明确实验维度以 pass/fail/unknown 加来源表达；缺少关键条件时保留为条件候选。允许同路线多个有效 baseline。仓库可访问不等于官方实现或复现实验；优先论文中明确链接的代码和实验说明。

下一步先明确缺口、预期收益与停止条件。使用 update_task、record_findings 和 submit_research 的当前版本语义。

具体例子按需读取 references/examples.md。
