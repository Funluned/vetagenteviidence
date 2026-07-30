# VetResearch Workbench v0.3

VetResearch Workbench 基于 VetEvidence AI v0.1，把科研问题、可核查文献、
实验条件、实验数据、网络药理学和分子对接串成一个可审计的科研决策闭环。
当前首个垂直场景聚焦：

> 候选药物与抗生素对目标病原菌是否存在值得进一步验证的协同作用？

系统仅用于科研证据整理与实验设计支持，不构成医疗、兽医诊断、处方或临床建议。

## 当前已实现

### VetResearch Workbench v0.3

- 把科研问题拆成 2—4 条可检验假设，并允许人工修改；
- 自动生成并执行最多 3 轮可见 PubMed 检索式，扩大候选池后按轮公平合并、PMID 去重，再优先保留能回答当前问题的文献；
- 按题名和摘要把文献分为“直接文献证据、间接背景、主题不匹配”，逐篇显示命中理由和判定原句；
- 排除目的、假设、方法定义和阈值说明的误判，并允许“完整实体句 + 紧邻明确组合结果句”的受控回指；直接文献证据为 0 时固定输出文献证据不足；
- 结构化记录协同、拮抗、相加和无相互作用等交互结局；同一问题出现协同与拮抗直接文献时明确显示冲突；
- 导入 RIS、EndNote、RefWorks 题录文件，按 DOI 或标题与年份去重；
- 把 PubMed 与用户导入文献整理为同一实验条件矩阵，缺失字段保持为空；
- 比较物种、模型、样本量、干预、剂量、时间、对照和指标，显示一致性、显式冲突与证据空白；
- 分析 FICI 与生长曲线 CSV，逐行保留原始值、校验错误和来源行号；两类 CSV 都必须显式填写研究对象与干预范围，并与当前研究问题匹配；
- 导入带来源 accession、版本和 SHA-256 的化合物—靶点、靶点—通路 CSV、XLSX 或 DOCX，按透明网络拓扑规则生成靶点排名，并导出 XLSX 结果和 DOCX 报告；
- 保存 AutoDock Vina 配体/受体 PDBQT 哈希、来源、搜索框、随机种子和软件版本；可只生成任务清单并导入匹配输出，也可由 Agent 受控执行已核验的本机 Vina；
- 在报告中单列“计算预测”，不允许网络排名或对接得分冒充直接文献证据、实验结果或协同证明；
- 生成带文献引用，以及 CSV 文件名、SHA-256、原始行号与计算过程的 Markdown/JSON 决策报告；
- 要求人工选择“通过、要求修改、拒绝”后再结束任务；
- 把任务事件、工具调用、失败、重试关系和人工复核保存到本地 `.workbench/runs/*.json`，可凭完整运行 ID 恢复；
- 通过六步 Streamlit 界面完成完整闭环，无需 LLM API Key。

### 继承自 VetEvidence AI v0.1

- 使用 NCBI E-utilities 检索真实 PubMed 题录和摘要；
- 保留标题、作者、期刊、年份、PMID、DOI、摘要和来源链接；
- 按 ISSN 查询 LetPub，同时显示中科院 2025 年 3 月升级版与 WOS JIF 分区，并支持缓存和本地 CSV 回退；
- 使用透明规则提取病原体、模型、样本量、药物、剂量、途径、结果和机制；
- 任何生成式结论保留 PMID、DOI 或来源片段，未报告信息不补造；
- 保留 30 条定向评测及其适用边界。

## Windows 快速启动

项目要求 Python 3.11 或更高版本。在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

终端显示地址后，在浏览器打开 `http://localhost:8501`。

## 六步工作流

1. `问题与假设`：填写研究对象、候选干预、联合药物和预设指标，检查并修改透明规则生成的假设；
2. `文献证据`：执行最多 3 轮 PubMed 检索，查看逐篇证据等级、准入理由和判定原句，或上传 RIS、EndNote、RefWorks 导出文件；
3. `实验数据`：核查实验条件矩阵、冲突和空白，再上传 FICI 或生长曲线 CSV；
4. `机制预测`：导入可追溯的 CSV/XLSX/DOCX 网络关系并导出 XLSX/DOCX 结果；生成 Vina 任务清单后，可由 Agent 运行已核验的本机 Vina，或导入带任务哈希的外部输出；
5. `决策报告`：生成带来源、风险、计算预测边界和下一步的报告，完成人工复核；
6. `运行记录`：查看事件、工具调用和失败记录，下载快照或凭完整运行 ID 恢复。

页面提供合成 RIS、FICI 和生长曲线演示数据。这些文件只用于验证工作流，页面与报告会明确标记，不能作为科研事实。

## 支持的输入

### 文献题录

- RIS：通常使用 `.ris`；
- EndNote：通常使用 `.enw` 或文本导出；
- RefWorks：使用其带标签的文本导出。

系统不会绕过知网等平台权限自动抓取，也不会把用户导入记录伪装成 PubMed 文献。非 PubMed 来源没有 PMID 时保持为空。

### FICI CSV

必须包含研究对象、两种药物身份和四个 MIC 数值列，其他列可作为原始记录一并保留：

```text
drug_a,drug_b,population_or_strain,drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
```

`drug_a`、`drug_b` 必须对应当前科研问题中的两种干预，
`population_or_strain` 必须填写当前研究对象、病原体或包含该对象名称的菌株标识；
缺少或不匹配的范围信息不能进入当前问题的报告结论。

计算公式为：

```text
FICI = drug_a_mic_combo / drug_a_mic_alone
     + drug_b_mic_combo / drug_b_mic_alone
```

当前透明分类阈值为：`≤ 0.5` 协同、`≤ 1` 相加、`≤ 4`
无相互作用（indifferent）、`> 4` 拮抗。该结果是描述性分类，不能替代
独立重复与 time-kill 等正交验证。

### 生长曲线 CSV

必须包含研究范围、时间、组别和测量值：

```text
population_or_strain,intervention,comparator,time,group,value
```

`population_or_strain`、`intervention`、`comparator` 必须与当前科研问题一致。
系统按组和时间点汇总重复值的均值、标准差与样本数；每个组至少需要
2 个不同时间点，且所有输入与计算结果必须为有限数值，才能用梯形法计算
该组 AUC。系统不自动进行显著性检验或模型比较。可直接下载
`data/templates/` 中的 CSV 模板。

### 网络药理学 CSV / XLSX / DOCX

化合物—靶点表必须包含：

```text
compound,compound_accession,organism,target,target_accession
```

靶点—通路表必须包含：

```text
organism,target,target_accession,pathway,pathway_accession
```

两张表都可使用 CSV、XLSX 或 DOCX。XLSX 必须只有一个非空工作表，DOCX
必须只有一个非空表格；系统不把自由文本 Word 文档猜测成结构化关系。页面
同时提供三种格式的模板。两个文件都必须记录来源名称、数据集 accession 和
版本，系统会保存输入 SHA-256。当前透明排名为
`compound_degree × pathway_degree`，仅反映导入关系的网络拓扑，不代表靶点
已验证。当前垂直流程只接受科研问题中的两种干预和一个研究对象；两种干预都
必须实际参与该研究对象的靶点—通路交集，混入其他化合物或物种会被拒绝。
分析完成后可下载包含来源、输入哈希、交集靶点和通路关系的 XLSX 结果或
DOCX 报告；导出格式不会提升结果的证据等级。

### AutoDock Vina

页面要求上传配体和受体 PDBQT，填写各自来源 accession、版本、受体研究对象、
搜索框、`exhaustiveness`、`num_modes`、随机种子和 Vina 版本。系统先生成
不含任何分数的任务清单，之后有两条执行路径：

- 如果发现显式配置、`VINA_EXECUTABLE`、本机标准安装目录或 `PATH` 中的
  Vina，用户可选择“由 Agent 运行本机 Vina”。系统会在执行前后核验版本和
  可执行文件 SHA-256，以固定参数列表、隔离临时目录和超时限制运行，不通过
  shell 拼接命令；
- 也可只下载任务清单，在外部运行后导入文本输出。导入内容必须包含匹配的
  任务清单哈希、软件版本、`mode/affinity` 表头和合规模式行；这种路径只能
  核验格式、关联关系和内容哈希，不能认证文件确由 Vina 实际运行产生。

本机执行只有在退出码为 0、日志和输出 PDBQT 均通过校验后才保存得分。成功
任务会留存 Vina 版本、可执行文件 SHA-256、受控参数、退出码、耗时和输出
PDBQT SHA-256，并在 `.workbench/vina/<run_id>/<task_id>/` 保存绑定任务哈希
的 `run.log`、`poses.pdbqt` 与 `metadata.json`。失败任务不会生成或保留分数，
失败原因仍写入运行事件。官方的结构准备与运行步骤见
[AutoDock Vina 基础对接文档](https://autodock-vina.readthedocs.io/en/stable/docking_basic.html)。

配体 PDBQT 至少要有 `ATOM/HETATM`、`ROOT` 和 `TORSDOF` 记录，受体至少
要有 `ATOM/HETATM`。外部运行时应把任务清单页面显示的
`VetEvidence-Manifest-SHA256: <digest>` 标记与 Vina 日志一起保存；导入时
该标记、Vina 版本、模式编号和 `num_modes` 必须同时通过校验。

项目不捆绑、下载 AutoDock Vina、Meeko 或结构数据库，也不会执行未通过身份
核验或未经用户选择的外部二进制程序。对接得分仍是计算预测，不能单独证明
结合、抗菌活性或药物协同。

## 配置与期刊分区

如需填写 NCBI 联系邮箱或 API Key，可在启动前为当前终端设置：

```powershell
$env:NCBI_EMAIL = "your_email@example.com"
$env:NCBI_API_KEY = "your_api_key"
```

`.env.example` 仅说明变量名；不要提交真实 `.env` 或密钥。

项目默认按 PubMed 返回的 ISSN 查询 LetPub，并缓存 7 天。LetPub 不可用时依次使用过期缓存和本地 CSV 回退。可通过 `LETPUB_LOOKUP_ENABLED=false` 关闭动态查询，或用 `JOURNAL_RANKINGS_CSV` 指向机构授权数据。详细口径见[期刊分区数据说明](docs/JOURNAL_RANKINGS.md)。

## 验证

运行全量自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

自动化测试覆盖文献证据准入、实验分析、网络文件适配与导出、Vina 任务绑定、
本机受控执行和审计留痕。定向评测是受控工程检查，不代表通用模型准确率。

真实负例使用 `quercetin + amoxicillin / Streptococcus agalactiae`：候选分级后保留 8 篇文献，但直接文献证据为 0，报告状态为 `blocked_no_direct_evidence`。真实正例使用 `florfenicol + thiamphenicol / Pasteurella multocida`：本次（2026-07-29）实时复跑保留 8 篇文献，只有 PMID `31749775` 通过严格直接文献证据准入。PubMed 是实时外部数据源，未来复跑的数量和排序可能变化。

运行包含真实 PubMed 查询的 v0.1 定向评测：

```powershell
.\.venv\Scripts\python.exe scripts\run_evaluation.py
```

评测结果写入 `data/eval/latest_results.json` 和 `docs/EVALUATION.md`。

## 数据与审计

- PubMed 题录和摘要来自 [NCBI Entrez Programming Utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/)；
- 期刊分区参考 [LetPub](https://www.letpub.com.cn/index.php?page=journalapp)，正式评价或投稿前仍需复核；
- 用户导入题录、实验 CSV、机制关系文件和结构文件只在本机处理；
- 每次运行保存为 `.workbench/runs/<run_id>.json`，该目录已被 Git 忽略；
- 报告中的文献结论关联 PMID、DOI、来源 URL 或原文片段；实验分析关联 CSV 文件名、SHA-256、原始行号和可复算过程；计算预测独立记录关系数据、结构和 Vina 输出的 accession、版本、参数及 SHA-256，本机 Vina 成功执行还记录可执行文件哈希、退出码、日志和输出 PDBQT。

## 项目文档

- [产品需求](docs/PRD.md)
- [架构说明](docs/ARCHITECTURE.md)
- [期刊分区数据说明](docs/JOURNAL_RANKINGS.md)
- [真实正负验收案例](docs/REAL_CASES.md)
- [评测报告](docs/EVALUATION.md)
- [演示脚本](docs/DEMO_SCRIPT.md)
- [简历证据](docs/RESUME_EVIDENCE.md)
- [面试讲解](docs/INTERVIEW_GUIDE.md)
- [项目复盘](docs/RETROSPECTIVE.md)

## Docker

安装 Docker 后运行：

```powershell
docker build -t vetevidence-ai .
docker run --rm -p 8501:8501 vetevidence-ai
```

本机当前未安装 Docker，因此 Dockerfile 尚未完成实际镜像构建验证。
默认 Docker 镜像不包含 AutoDock Vina；如需在容器内使用 Agent 本机执行路径，
必须另行安装或挂载兼容的 Vina 可执行文件，并通过 `VINA_EXECUTABLE` 或容器
`PATH` 提供。未提供 Vina 时仍可生成任务清单并导入外部运行输出。

## 当前边界

- 首个垂直场景只覆盖候选药物与抗生素协同作用，不代表支持所有实验类型；
- 当前只解析 PubMed 摘要和 RIS、EndNote、RefWorks 题录导出，不读取未授权全文，也不支持扫描 PDF/OCR；
- 用户导入记录的正确性仍需人工核查，系统不会把缺失信息补造为事实；
- FICI 和生长曲线只做透明、可追溯的描述性分析；
- 网络药理学和分子对接只属于计算预测层，不进入直接文献或实验协同证据；
- 当前不自动下载化合物、靶点、结构或 Vina；Agent 只在用户明确选择后受控执行已发现且通过版本与哈希核验的本机 Vina。用户仍须合法取得输入并核查结构准备、质子化状态和搜索框；
- 直接文献证据规则宁可漏报也不误报：要求同一题名或摘要句明确出现研究对象、两种干预、交互指标和结果；仅描述 checkerboard、time-kill 或 FICI 方法与阈值不构成结果证据，缩写、同义词或跨句表达可能需要人工复核；
- 合成演示数据只用于验证计算和页面流程，不得进入科研结论；
- 规则提取无法覆盖所有摘要表达方式，冲突检测也只识别显式方向差异；
- 系统不训练或微调模型，不使用多 Agent 框架、向量数据库或复杂权限系统；
- 当前仅面向可信的单用户本机运行；完整运行 ID 在本机恢复流程中相当于访问凭证，不适合直接用于共享部署；
- 决策报告必须人工复核，不能替代全文核查、原始数据审计或验证性实验。
