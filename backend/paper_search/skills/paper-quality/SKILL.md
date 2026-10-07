---
name: paper-quality
description: 判断发表状态、领域 venue、机构、代码和实验信号
metadata:
  version: "1"
---

# 判断发表状态、领域 venue、机构、代码和实验信号

区分 preprint/submitted/accepted/published/unknown。OpenReview 页面存在不等于录用。会议目录只是识别辅助，发表事实需要正式页面或论文原文。机构优先论文原文和正式作者信息。公开仓库、官方代码、复现条件分别判断。不用字段齐全程度打质量分；引用数只在来源附带或用户要求时获取。任务匹配和证据先于 prestige。

下一步先明确缺口、预期收益与停止条件。使用 update_task、record_findings 和 submit_research 的当前版本语义。

具体例子按需读取 references/examples.md。

按需读取 references/venues.yaml 识别领域会议与期刊别名；目录不能代替具体发表证据。
