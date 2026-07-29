# VetEvidence AI 评测报告

- 运行时间：2026-07-29T08:23:17.757876+00:00
- 真实检索词：`quercetin Streptococcus agalactiae mastitis`
- 样本数：30
- 通过：30
- 失败：0
- 定向检查通过率：100.0%

> 本报告是针对当前示范查询和受控边界场景的小样本工程检查，不是通用模型准确率，也不能替代人工全文核查。

## 分类结果

| 分类 | 样本数 | 通过 | 失败 | 通过率 |
|---|---:|---:|---:|---:|
| citation | 6 | 6 | 0 | 100.0% |
| extraction | 16 | 16 | 0 | 100.0% |
| no_hallucination | 3 | 3 | 0 | 100.0% |
| retrieval | 3 | 3 | 0 | 100.0% |
| robustness | 2 | 2 | 0 | 100.0% |

## 逐条结果

| ID | 分类 | 结果 | 问题 | 实际值 |
|---|---|---|---|---|
| RET-01 | retrieval | 通过 | 示范查询是否检索到目标论文？ | true |
| RET-02 | retrieval | 通过 | 示范查询是否至少返回两篇文献？ | 2 |
| RET-03 | retrieval | 通过 | 目标论文年份是否为 2026？ | 2026 |
| CIT-01 | citation | 通过 | 目标论文 DOI 是否与 PubMed 元数据一致？ | "10.1016/j.rvsc.2026.106289" |
| CIT-02 | citation | 通过 | 回答是否包含目标 PMID？ | true |
| CIT-03 | citation | 通过 | 回答是否包含目标 DOI？ | true |
| CIT-04 | citation | 通过 | 每条关键结论是否同时带有对应 PMID？ | true |
| CIT-05 | citation | 通过 | 引用原句是否同时支持 NF-κB、NLRP3 和铁死亡表述？ | {"missing_terms": []} |
| CIT-06 | citation | 通过 | 引用数量是否与证据记录数量一致？ | 2 |
| EXT-01 | extraction | 通过 | 病原体是否提取为 Streptococcus agalactiae？ | "Streptococcus agalactiae" |
| EXT-02 | extraction | 通过 | 疾病是否提取为乳腺炎？ | "乳腺炎" |
| EXT-03 | extraction | 通过 | 物种是否提取为小鼠？ | "小鼠" |
| EXT-04 | extraction | 通过 | 模型字段是否包含小鼠乳腺炎？ | true |
| EXT-05 | extraction | 通过 | 总样本量是否提取为 25？ | 25 |
| EXT-06 | extraction | 通过 | 药物是否提取为 Quercetin？ | "Quercetin" |
| EXT-07 | extraction | 通过 | 剂量是否提取为 25、50、100 mg/kg？ | "25, 50, 100 mg/kg" |
| EXT-08 | extraction | 通过 | 给药途径是否提取为腹腔注射？ | "腹腔注射" |
| EXT-09 | extraction | 通过 | 给药时间是否包含 24 h before？ | true |
| EXT-10 | extraction | 通过 | 对照字段是否包含每组 n=5？ | true |
| EXT-11 | extraction | 通过 | 结果字段是否包含髓过氧化物酶信息？ | true |
| EXT-12 | extraction | 通过 | 机制字段是否包含 NF-κB？ | true |
| EXT-13 | extraction | 通过 | 机制字段是否包含 NLRP3？ | true |
| EXT-14 | extraction | 通过 | 机制字段是否包含铁死亡？ | true |
| EXT-15 | extraction | 通过 | 关键结论是否包含 ferroptosis？ | true |
| EXT-16 | extraction | 通过 | 第二篇体外研究的物种是否提取为牛？ | "牛" |
| HAL-01 | no_hallucination | 通过 | 第二篇摘要未报告总样本量时是否保持为空？ | true |
| HAL-02 | no_hallucination | 通过 | 第二篇摘要未报告剂量时是否保持为空？ | true |
| HAL-03 | no_hallucination | 通过 | 第二篇摘要未报告给药途径时是否保持为空？ | true |
| ROB-01 | robustness | 通过 | 没有证据时是否明确说不足以回答且不给引用？ | true |
| ROB-02 | robustness | 通过 | 两条受控结论冲突时是否同时保留两条？ | true |

## 失败与错误分类

本次定向检查没有失败项；仍需增加不同病原、药物、物种和全文样本，不能据此声称系统已达到通用准确率。

## 已知局限

- 大部分字段检查集中在一个真实示范查询，覆盖面有限。
- 当前提取器是透明规则，不理解未列入规则的新表达方式。
- 引用检查验证了 PMID、DOI、来源原句和结论的机械对应，尚未完成人工语义支持度复核。
- PubMed 摘要不等于论文全文，剂量、对照和局限可能缺失。
