# 真实 PubMed 正负验收案例

更新时间：2026-07-29

以下案例均使用 NCBI PubMed 公开题录和摘要；数量与排序会随外部数据库更新，不是固定产品承诺。

## 负例：当前证据不足

- 研究对象：`Streptococcus agalactiae`
- 候选干预：`quercetin`
- 联合药物：`amoxicillin`
- 三轮查询分别检索候选干预、联合药物和协同/交互组合。
- 扩大候选池、公平合并并按证据等级稳定分桶后保留 8 篇唯一文献。
- 直接文献证据：0。
- 间接背景：8。
- 报告状态：`blocked_no_direct_evidence`。

正确输出是“当前检索未发现同时覆盖研究对象、两种干预、明确交互指标和结果的直接文献证据；仅凭本次文献检索不能判断或宣称存在协同作用”。这不等于证明协同作用不存在，匹配当前药物对的真实实验数据仍使用独立证据链呈现。

## 正例：存在可核查直接文献证据

- 研究对象：`Pasteurella multocida`
- 候选干预：`florfenicol`
- 联合药物：`thiamphenicol`
- 公平合并后保留 8 篇唯一文献。
- 本次（2026-07-29）实时复跑中唯一通过严格准入的来源：PMID `31749775`。
- DOI：`10.3389/fmicb.2019.02430`。
- PubMed：https://pubmed.ncbi.nlm.nih.gov/31749775/
- 报告状态：`admitted`。

该摘要明确在猪源 `P. multocida` 中研究两种药物联用，并报告 checkerboard/FICI 与 time-kill 结果。结果只支持“部分分离株和相应实验条件下存在协同”，不能外推为所有菌株均协同。

## 准入规则

`interaction-evidence-v1` 要求同一题名或摘要句同时命中：

1. 当前研究对象；
2. 当前两种干预；
3. 明确交互指标，例如 `synergy`、`FICI`、`checkerboard` 或 `time-kill`；
4. 明确的方向性或量化结果。

仅出现 `combination`、纯 checkerboard/time-kill/FICI 方法句、FICI 阈值定义、来自第三轮联合查询或具有较高期刊分区，均不能替代上述直接文献证据条件。规则采用保守策略，缩写、同义词或跨句表达可能被降为间接背景，必须保留人工复核。
