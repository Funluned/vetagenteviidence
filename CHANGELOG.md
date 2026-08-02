# Changelog

本文件记录 VetResearch Workbench 面向使用者的重要变更。

## [0.7.0] - 2026-08-02

本条目描述 VetResearch Workbench v0.7.0 的发布代码、验证结果与已知限制。

### 新增

- 建立版本化的 27 题离线评测集，覆盖九类证据、引用、范围、提示注入和工具
  故障边界，并固化规则、RAG 和 Fake Agent 基线及稳定哈希。
- 增加本地 SQLite 证据索引和可追溯检索，保留来源 ID、PMID／DOI、字段位置、
  授权范围、数据版本与内容哈希；免费默认路径使用关键词 Top 3。
- 增加可替换的 LLM Provider 契约、DeepSeek 真实评测 Provider、受限的 Research
  Agent，以及只读的 Evidence Reviewer 实验模式。
- 为 Agent 增加类型化状态、允许工具清单、步骤／Token／费用／重试硬上限、
  Prompt 哈希、调用审计、延迟和失败记录。
- 增加证据等级、逐条引用校验、证据不足拒答、提示注入隔离和人工复核状态。
- 增加 Ubuntu、Windows、macOS 的 Python 3.11 CI，并修复可执行文件与跨平台
  路径处理的已知差异。

### 验证结果

- 当前零费用 Fake 合同基线为规则 `20/27`、单 Agent `25/27`、双 Agent
  `25/27`。Fake 只验证编排和安全合同，不代表 DeepSeek 或科研质量。
- 历史 DeepSeek 全 27 题报告按当时 v1 scorer 记录单／双 Agent 均为
  `24/27`；使用修正 scorer 对同一份历史输出离线重算后均为 `22/27`。
- 后续定向复测验证了直接证据门禁、检索实体补齐、严格 JSON 契约和一次
  2048→4096 的有界截断恢复。不同代码版本和不同批次的结果不合并为新总分。
- 发布收口第一次全 27 题真实运行得到单 Agent `26/27`、双 Agent `25/27`，
  70 次 HTTP 200，精确记账 `¥0.4456658`；它暴露并永久记录了 `INJ-02` 证据
  注入误准入和 `DIR-01` Reviewer 截断缺口。
- 两项修复后，[第二次独立全量报告](data/eval/v0.7/results/agent_deepseek_deepseek-v4-pro_20260802T085911981817Z.json)
  固化为当前 v0.7.0 结果：单 Agent `23/27`，失败 `CON-02`、`CIT-01`、
  `TOOL-02`、`TOOL-03`；双 Agent `24/27`，失败 `CON-02`、`TOOL-02`、
  `TOOL-03`。输入／结果 SHA-256 分别为
  `901b7d507c483c38cc60d2ac33476dda9908f178ace936cf7676a960c87f92cb` 与
  `3962523562e38944eb09b6d997ac96b055377d3184fc05a7be0a298dd83568c0`。
- 第二次全量的单／双指标为：Recall `0.80/0.80`、Citation Precision
  `0.75/1.00`、Unsupported Claim Rate `0.25/0`、Abstention Accuracy
  `0.92/0.96`、Task Completion `1.00/1.00`。共 71 次 HTTP 200，其中
  69 次正常停止、2 次长度截断，Provider HTTP 自动重试为 0；Token 为
  `64,663 / 55,318 / 49,388`（输入／输出／reasoning）。
- 第二次全量精确记账 `¥0.4318394`：共享 Research `¥0.1837620`，
  Reviewer 增量 `¥0.2480774`；两次发布收口全量累计 `¥0.8775052`。
  单／双中位延迟为 `9.7549 s / 23.3779 s`，增量 `13.6231 s`。
- `INJ-02`、`DIR-01` 在第二次全量的单／双路径均通过。`TOOL-02` 真实
  触发 Reviewer 2048→4096 恢复；恢复成功后 Reviewer 因检索不完整继续
  安全拒答，没有把格式恢复误当成研究任务通过。
- 两项零费用修复后，Fake 单／双基线更新为 `25/27`，Windows 全量测试为
  `648 passed, 1 skipped`；规则、RAG 与 Fake 基线均复跑匹配。

### 已知限制

- 免费默认工作台不调用 LLM；DeepSeek 仅用于隔离的开发者评测，只有开发者或
  评测操作者在显式真实评测时提供自己的 API Key，并逐次确认费用上限。
- 当前关键词检索 Recall@3 为 `4/5`；实验性的特征哈希向量和混合模式分别为
  `2/5`、`3/5`。项目没有证明语义向量检索带来收益。
- 第一次发布收口全量报告作为缺陷发现证据原样保留；第二次独立报告是
  当前 v0.7.0 结果。两份报告不覆盖、不回写、不拼分。
- 本批双 Agent 改善了 Citation Precision、Unsupported Claim Rate 和
  Abstention Accuracy，并多通过一题；但 Recall／Task Completion 不变，
  规划／工具失败仍共享，费用和延迟更高。结合历史批次波动，双 Agent 只保留
  为实验模式，不作为默认路径。
- v0.7.0 金标准状态仍是 `engineering_gold_pending_domain_expert_review`；系统基于
  PubMed 摘要及合法导入材料，不能替代全文核查、原始数据审计或领域专家判断。
- Dockerfile 尚未完成实际镜像构建验证；孙奇本人从零安装、讲解、修改和排错
  验收按既定决定延期到 GitHub 工程收口之后。
- 系统仅用于科研证据整理和实验设计支持，不提供医疗、兽医诊断、处方或临床
  建议，所有决策报告仍须人工最终复核。
