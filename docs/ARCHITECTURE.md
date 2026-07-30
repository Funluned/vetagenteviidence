# VetResearch Workbench v0.3 架构说明

## 版本关系

VetResearch Workbench v0.3 复用 VetEvidence AI v0.1 的 PubMed、期刊分区、
规则提取、引用和评测能力，在 v0.2 的问题、实验和审计闭环上增加严格的
证据结果判定、问题范围绑定、网络药理学和 AutoDock Vina 预测层。

包名继续使用 `vetevidence`，以避免为产品升级进行无收益的代码重命名。

## 主数据流

```mermaid
flowchart TD
    U["用户科研问题"] --> UI["Streamlit 六步界面"]
    UI --> Q["ResearchQuestion"]
    Q --> H["2—4 条可检验假设"]
    Q --> P["最多 3 轮 PubMed 检索式"]
    P --> N["NCBI E-utilities"]
    N --> D["扩大候选池，轮询公平合并并按 PMID 去重"]

    UI --> I["RIS / EndNote / RefWorks 导入"]
    I --> ID["按 DOI 或标题与年份去重"]

    D --> G["直接 / 间接 / 主题不匹配文献分级后截断"]
    ID --> G
    G --> L["统一 LiteratureItem"]
    L --> C["ExperimentCondition 矩阵"]
    C --> A["一致性 / 显式冲突 / 证据空白"]
    G --> AD["EvidenceAdmission 直接文献证据准入"]

    UI --> CSV["FICI 或生长曲线 CSV"]
    CSV --> X["逐行校验、问题范围门槛与描述性分析"]
    UI --> NP["化合物-靶点与靶点-通路 CSV"]
    NP --> NX["可追溯网络排名"]
    UI --> DM["PDBQT + Vina 参数"]
    DM --> VM["无分数任务清单"]
    UI --> VO["用户导入 Vina 文本输出"]
    VM --> VO
    VO --> DX["任务哈希 / 版本核对与模式解析"]
    A --> R["ResearchDecisionReport"]
    AD --> R
    X --> R
    NX -. "独立计算预测层" .-> R
    DX -. "独立计算预测层" .-> R
    R --> HR["人工复核"]

    Q --> S["WorkbenchRunSnapshot"]
    H --> S
    D --> S
    ID --> S
    A --> S
    X --> S
    NX --> S
    VM --> S
    DX --> S
    R --> S
    HR --> S
    S --> FS[".workbench/runs/run_id.json"]
    FS --> UI
```

## v0.1 继承子流程

```mermaid
flowchart LR
    P["PubMedArticle"] --> J["LetPub Ranking Provider"]
    J --> L["LetPub 公开期刊页"]
    J --> K["7 天 JSON 缓存"]
    J -. "失败回退" .-> C["授权 CSV / 演示子集"]
    J --> E["rules_v1 Evidence Provider"]
    E --> R["EvidenceRecord"]
    R --> A["带引用回答"]
    R --> V["定向评测"]
```

v0.2 的多轮检索保留每个查询内部的 PubMed 相关性顺序，以轮询方式让每个非空查询每轮最多贡献一个新 PMID；跨查询重复不会消耗该查询的贡献机会，查询耗尽后由其他查询回填。合并结果继续调用上述分区和规则提取能力，没有另建第二套检索系统。

## 模块职责

| 模块 | 版本 | 职责 |
|---|---|---|
| `pubmed.py` | v0.1 | ESearch/EFetch 请求、重试、XML 解析 |
| `journal_rankings.py` | v0.1 | 按 ISSN 查询 LetPub、缓存与本地回退 |
| `models.py` | v0.1 | PubMed 文献、证据、回答和旧工作流结果 |
| `extraction.py` | v0.1 | PubMed 摘要的透明规则提取 |
| `providers.py` | v0.1 | 可替换的提取与回答 Provider 边界 |
| `answering.py` | v0.1 | 逐条引用回答和局限提示 |
| `retrieval.py` | v0.1 | 单轮检索、提取与回答 |
| `evaluation.py` | v0.1 | 定向评测、分类指标和报告 |
| `literature_import.py` | v0.2 | RIS、EndNote、RefWorks 识别、规范化和去重 |
| `imported_extraction.py` | v0.2 | 从用户导入题录中透明提取可见实验字段 |
| `workbench.py` | v0.3 | 问题、假设、结构化交互结局、引用、冲突、空白、任务与复核模型 |
| `workbench_pipeline.py` | v0.3 | 多查询融合、证据准入、实验范围门槛、评估与分层决策报告 |
| `experiment_analysis.py` | v0.3 | 带药物和病原体身份的 FICI 与生长曲线校验和描述性计算 |
| `mechanism_prediction.py` | v0.3 | 可追溯网络关系分析、Vina 任务清单、绑定输出解析和问题范围门槛 |
| `run_store.py` | v0.3 | 每个运行一个 JSON 快照的原子保存、schema v5 迁移与按 ID 恢复 |
| `app.py` | v0.3 | 六步 Streamlit UI 与会话状态 |

## 关键数据契约

### 多来源文献

`LiteratureItem` 使用 `source_id` 和 `source_type` 区分 PubMed 与用户导入来源。PMID 是可选字段：PubMed 文献保留真实 PMID，用户导入记录没有 PMID 时保持为空。

`ExperimentCondition` 固定比较物种、模型、样本量、干预、剂量、途径、时间、对照、指标、机制和关键结果，同时保留题名、摘要、来源 ID、PMID、DOI、来源 URL、来源片段和 `EvidenceQualification`。

`interaction-evidence-v2` 采用保守准入：结果句必须包含研究对象、两种干预、
交互指标和结构化交互结局；或者仅允许前一句完整绑定三个实体、后一句以
“该组合/the combination”明确回指并报告结果。目的、假设、评价计划、方法、
公式和阈值定义句不会准入。中文实体使用无空格匹配规则。直接文献同时保存
`synergy`、`antagonism`、`additive` 或 `indifferent` 等结构化结局，同一问题
出现协同和拮抗时生成可追溯冲突。

每轮查询获取 `max(20, 页面保留数 × 3)` 条候选记录（上限 100）。
候选先按各查询原始排序轮询去重，再按直接、间接、主题不匹配稳定分桶，
最后才截断到页面保留数。

### 可追溯结论

文献型 `EvidenceReference` 至少包含 PMID、DOI、来源 URL 或来源片段中的一项，任意 `source_id` 不能单独作为证据。`TraceableConclusion` 必须含至少一个合法 `EvidenceReference`；没有可追溯来源时，决策报告拒绝生成。

CSV 型 `EvidenceReference` 强制记录文件名、64 位输入 SHA-256、与哈希一致
的来源 ID、原始行号和完整计算过程。FICI 与生长曲线的每个有效行都必须让
两种干预和 `population_or_strain` 匹配当前问题；错配数据只能形成证据空白，
不能进入结论。

计算预测不使用 `EvidenceReference` 冒充正式证据。`MechanismPredictionBundle`
独立保存网络关系输入的 accession、版本、行号和 SHA-256，以及 Vina 配体、
受体、搜索框、随机种子、引擎版本、原始输出哈希和解析模式。报告单列该层，
其内容不会成为协同结论或建议的证据引用。

### 审计快照

`WorkbenchRunSnapshot` 聚合：

- 科研问题、检索计划和可修改假设；
- PubMed 结果与导入文献；
- 实验条件、冲突、空白和 CSV 分析；
- 网络药理学结果、Vina 待运行清单和已解析对接输出；
- 任务事件、工具调用、失败和重试关系；
- 决策报告与人工复核。

`RunStore` 将每个快照写入 `.workbench/runs/<run_id>.json`，先写临时文件再
原子替换。快照使用 `schema_version=5`；旧快照会补建检索计划和迁移事件，
旧版证据分级、无法证明输入哈希的旧分析及派生报告会保守失效，v4 快照会
获得空的机制预测层而不会伪造结果。

每次报告刷新生成新的报告 ID 和稳定的科研内容 SHA-256。人工复核事件保存该哈希和当时的完整报告快照，后续刷新不会抹掉已复核版本的审计依据。

## 分析规则

### FICI

必需列：

```text
drug_a_mic_alone
drug_a_mic_combo
drug_b_mic_alone
drug_b_mic_combo
drug_a
drug_b
population_or_strain
```

系统逐行验证有限且大于零的数值，计算两个 FIC 之和，并按透明阈值分类：

- `FICI ≤ 0.5`：协同；
- `0.5 < FICI ≤ 1`：相加；
- `1 < FICI ≤ 4`：无相互作用（indifferent）；
- `FICI > 4`：拮抗。

坏行保留原始字段、CSV 行号和错误，不会静默丢弃。

### 生长曲线

必需列为 `population_or_strain`、`intervention`、`comparator`、`time`、
`group`、`value`。每组至少两个不同时间点；系统校验输入及均值、标准差、
AUC 均为有限数值后，才对均值曲线使用梯形法计算分组 AUC。

### 网络药理学与分子对接

网络药理学只对用户提供的化合物—靶点与靶点—通路关系做交集和透明拓扑
排名，不调用黑盒靶点预测。相同 `target_accession` 只有在 organism 也一致时
才能连接。Vina 清单本身不含分数；解析器要求输出中的 AutoDock Vina 版本与
清单一致，并检测标准 `mode/affinity` 表头和数值模式行。两类结果都必须与
当前问题的两种干预和研究对象匹配。

这两类分析都只提供描述性结果，不自动执行显著性推断、模型比较或因果判断。

## 关键取舍

### 顺序工作流而非多 Agent 框架

当前所谓 Agent 能力体现在可见检索计划、显式工具调用、任务状态、失败、重试和人工复核，而不是并发角色数量。单进程顺序编排更容易验证来源和恢复运行。

### 单体 Streamlit 而非 FastAPI

当前只有一个本地客户端，Streamlit 会话状态与本地 JSON 快照已满足六步闭环。增加独立 API 服务会扩大部署和跨进程状态范围，但不会提高当前证据质量。

### 不使用向量数据库

首个场景只处理少量题录、摘要和结构化 CSV，没有跨项目语义知识库需求。向量数据库不是完成当前闭环的必要条件。

### 规则优先

问题拆解、字段提取、冲突检测、FICI 和生长曲线均使用可见规则。规则覆盖有限，但能明确验证“来源是什么、为何得到该字段、失败发生在哪里”。

## 可靠性

- NCBI `429/5xx` 与网络错误最多重试两次；
- 多轮检索最多 3 个查询，扩大候选池后保留各查询内部顺序，轮询公平合并并按 PMID 去重，再按证据等级稳定分桶和限制全局数量；
- 间接或主题不匹配文献可展示和审计，但不能进入协同文献结论；直接文献证据为 0 时报告固定声明文献证据不足；
- LetPub 失败时依次使用过期缓存和本地 CSV，不阻断 PubMed 主流程；
- 导入记录按 DOI 优先、标题与年份兜底去重；
- CSV 校验保留每一行的原始值和错误；
- 没有证据时拒绝生成结论；
- 任务状态从追加式事件序列推导，失败信息不会因后续成功而消失；
- 运行快照可序列化、原子保存并恢复；
- 合成演示数据在输入和报告中明确标记；
- schema v5 会重建旧快照的文献条件，并让旧评估和报告安全失效后重新生成；

真实负例保留 8 篇文献但无直接文献证据，状态为 `blocked_no_direct_evidence`；真实正例保留 8 篇文献，本次复跑中严格规则只准入 PMID `31749775`。外部 PubMed 数据会更新，数量和排序只代表当前验收。

## 安全边界

- 只自动读取公开 PubMed 元数据和摘要；
- RIS、EndNote、RefWorks 与实验 CSV 由用户主动上传并在本机处理；
- 不自动抓取知网，不读取未授权全文，不支持扫描 PDF/OCR；
- `.env`、API Key、用户数据和 `.workbench` 运行记录不进入仓库；
- 页面和报告持续显示非诊断声明；
- 用户导入来源不伪造 PMID，未报告字段不补造；
- 合成演示数据不能成为科研事实；
- 当前恢复机制把不可猜测的完整运行 ID 作为本机访问凭证，仅适用于可信单用户环境；共享部署必须增加账号与对象级授权；
- 报告必须经过人工复核，且不能替代全文、原始数据和验证性实验。
