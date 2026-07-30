# VetResearch Workbench v0.6

VetResearch Workbench 基于 VetEvidence AI v0.1，把科研问题、可核查文献、
实验条件、实验数据、公开数据库、网络药理学、分子对接和受控的分子动力学
技术烟测串成一个可审计的科研决策闭环。
当前首个垂直场景聚焦：

> 候选药物与抗生素对目标病原菌是否存在值得进一步验证的协同作用？

系统仅用于科研证据整理与实验设计支持，不构成医疗、兽医诊断、处方或临床建议。

## 当前已实现

### VetResearch Workbench v0.6

- 只提供 OpenMM 8.5.2 的单重复、30 步 `technical_smoke`：核验已参数化
  `System XML` 与 topology 能否完成最小化和极短积分，不把它称为 NVT、
  NPT、production 或科研级 MD；
- 保存已裁剪为所选链、无 altloc 的单模型受体 PDB、单记录 V2000 配体 SDF、
  准备后 topology、`System XML`、实际力场文件、参数化工具/命令和逐原子
  source→topology canonical 映射，并用 SHA-256 绑定；
- 在独立后台子进程中分块执行；任务状态使用文件锁和 revision
  compare-and-swap，支持取消、checkpoint、恢复及启动时遗留任务校正；
- 复核实际 System 的粒子数、force/constraint 类型与数量、topology 原子数、
  资源上限、OpenMM 平台、设备、精度、平台属性和随机种子；驱动仅在后端
  报告时记录。v0.6 在盒向量双向
  绑定完成前拒绝所有周期体系；
- 当前只生成真实温度与势能时间序列的数值健康检查。RMSD、RMSF、回转半径、
  接触、氢键、压力、密度和自由能均不计算、不展示为结果，也不允许据此解释
  稳定结合、抗菌作用或协同。

### VetResearch Workbench v0.5

- 在生成科研级对接任务前设置受体人工门禁：研究者必须确认模型、链、替代构象、
  水、辅因子/金属与其他异源原子的处理，并把原始结构、已准备 PDBQT、口袋依据、
  工具版本和各自 SHA-256 绑定到同一审批记录；
- 用类型化身份区分 RCSB PDB ID、UniProt accession、NCBI TaxID、
  PubChem CID/InChIKey 与用户自有命名空间；名称不能代替稳定标识；
- 支持批量配体和多个随机种子；每次 Vina 尝试把任务清单、引擎身份、seed、
  日志、输出构象、预测评分和 SHA-256 强绑定，失败或错配产物不进入汇总；
- 提供固定版本、随仓库分发并记录上游信息和许可证的本地 3Dmol.js 查看器，
  以及可编辑 PML；只有用户再次确认后才调用本机 Open-Source PyMOL 或 PLIP；
- PNG 必须能被图像解码器完整校验后才标为可用；PSE 只有经同一已核验 PyMOL
  重新打开验证后才标为已验证，否则明确降级为 `generated_unverified`；
- 对接表、图和相互作用解释始终标记为 `computational_prediction`。不同 seed
  之间只比较预测评分的描述性稳定性，不声称已计算跨 seed RMSD 或构象簇。

### VetResearch Workbench v0.4

- 把科研问题拆成 2—4 条可检验假设，并允许人工修改；
- 自动生成并执行最多 3 轮可见 PubMed 检索式，扩大候选池后按轮公平合并、PMID 去重，再优先保留能回答当前问题的文献；
- 按题名和摘要把文献分为“直接文献证据、间接背景、主题不匹配”，逐篇显示命中理由和判定原句；
- 排除目的、假设、方法定义和阈值说明的误判，并允许“完整实体句 + 紧邻明确组合结果句”的受控回指；直接文献证据为 0 时固定输出文献证据不足；
- 结构化记录协同、拮抗、相加和无相互作用等交互结局；同一问题出现协同与拮抗直接文献时明确显示冲突；
- 导入 RIS、EndNote、RefWorks 题录文件，按 DOI 或标题与年份去重；
- 把 PubMed 与用户导入文献整理为同一实验条件矩阵，缺失字段保持为空；
- 比较物种、模型、样本量、干预、剂量、时间、对照和指标，显示一致性、显式冲突与证据空白；
- 分析 FICI 与生长曲线 CSV，逐行保留原始值、校验错误和来源行号；两类 CSV 都必须显式填写研究对象与干预范围，并与当前研究问题匹配；
- 由用户主动查询 PubChem、UniProt、NCBI Gene/GenBank、RCSB PDB、STRING
  和 DAVID；统一保存 CID/InChIKey、UniProt accession、NCBI TaxID、
  GeneID、GenBank accession.version 与 PDB ID；
- 每次数据库查询保存脱敏参数、访问时间、数据库版本或发布日期、原始响应
  SHA-256、规范化记录及可下载的完整校验归档；没有 NCBI 联系邮箱或未同意
  向 STRING/DAVID 外发标识时只生成离线请求，不静默联网；
- 将 STRING 的实验、人工整理、文本挖掘和模型预测通道分开显示；
  `combined_score` 只用于排序；DAVID 富集保留目标集、背景集、TaxID、
  原始 P 值和 BH 校正后 P 值；
- 导入带来源 accession、版本和 SHA-256 的化合物—靶点、靶点—通路 CSV、XLSX 或 DOCX，按透明网络拓扑规则生成靶点排名，并导出 XLSX 结果和 DOCX 报告；
- 可用项目隔离环境中的 Open Babel 3.2.1 把单个配体的 SMI/SMILES、SDF、MOL、MOL2 或 PDB 转为经校验的 PDBQT，并直接交给现有 Vina 任务；
- 保存 AutoDock Vina 配体/受体 PDBQT 哈希、来源、搜索框、随机种子和软件版本；可只生成任务清单并导入匹配输出，也可由 Agent 受控执行已核验的本机 Vina；
- 在报告中单列“计算预测”，不允许网络排名或对接得分冒充直接文献证据、实验结果或协同证明；
- 生成带文献引用，以及 CSV 文件名、SHA-256、原始行号与计算过程的 Markdown/JSON 决策报告；
- 要求人工选择“通过、要求修改、拒绝”后再结束任务；
- 把任务事件、工具调用、失败、重试关系和人工复核保存到本地 `.workbench/runs/*.json`，可凭完整运行 ID 恢复；
- 通过本地 Streamlit 界面完成完整闭环，无需 LLM API Key。

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

如需使用 Open Babel 配体准备，在项目隔离环境中额外安装固定的可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[molecular-docking]"
```

本项目在 Python 3.11 x64 Windows 上核验的是官方 PyPI
`openbabel==3.2.1` wheel（发布于 2026-07-11，下载文件 SHA-256：
`04c64bd8db520046abdc01396ee122b3b32deb08bdd2fa136a16228ffad7bf8c`）。
其他平台请按 [Open Babel 官方安装文档](https://openbabel.org/docs/Installation/install.html)
准备兼容环境。

如需运行 v0.6 OpenMM 技术烟测，可安装 CPU/OpenCL 通用包；CUDA 12 环境可
改用单独的 CUDA 12 可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[molecular-dynamics]"
# 或
.\.venv\Scripts\python.exe -m pip install -e ".[molecular-dynamics-cuda12]"
.\.venv\Scripts\python.exe -m openmm.testInstallation
```

Windows 下直接运行官方命令可能在项目预加载 CUDA wheel DLL 前只列出
CPU/OpenCL；CUDA 的产品级验收应再运行下文
`scripts/run_md_smoke.py --platform CUDA`，并以结果中的 `actual_platform`
与 `DeviceName` 为准。

## 九步工作流

1. `问题与假设`：填写研究对象、候选干预、联合药物和预设指标，检查并修改透明规则生成的假设；
2. `文献证据`：执行最多 3 轮 PubMed 检索，查看逐篇证据等级、准入理由和判定原句，或上传 RIS、EndNote、RefWorks 导出文件；
3. `实验数据`：核查实验条件矩阵、冲突和空白，再上传 FICI 或生长曲线 CSV；
4. `数据库证据`：按 TaxID 查询六类公开数据库，审阅标识映射、版本、来源
   和原始响应归档，并把 STRING/DAVID 结果组织为分层证据网络；
5. `网络药理`：导入可追溯的 CSV/XLSX/DOCX 网络关系并导出 XLSX/DOCX 结果；
6. `分子对接`：审批受体模型、链、替代构象、水和异源原子处理后，批量导入
   带类型化身份的配体并按多个 seed 运行已核验 Vina；审阅强绑定产物、
   3Dmol.js 三维视图、可编辑 PML，以及经用户确认后生成的 PyMOL/PLIP 产物；
7. `分子动力学`：提交已参数化且已审核的 OpenMM 输入，后台运行单重复
   30 步技术烟测，并查看取消、checkpoint、恢复、平台与产物校验状态；
8. `决策报告`：生成带来源、风险、计算预测边界和下一步的报告，完成人工复核；
9. `运行记录`：查看事件、工具调用和失败记录，下载快照或凭完整运行 ID 恢复。

页面提供合成 RIS、FICI 和生长曲线演示数据。这些文件只用于验证工作流，页面与报告会明确标记，不能作为科研事实。

## 支持的输入

### 公开数据库查询

数据库查询必须由用户点击提交，不会在 Streamlit 重跑时自动重复。所有
生物实体查询要求明确的 NCBI TaxID；名称或符号映射出现多个候选时，结果
会标记歧义，不替用户静默选择。STRING 与 DAVID 会把标识列表发送给第三方
服务，界面因此要求单独确认；敏感或未公开列表应下载离线请求并在获批环境
中处理。详细的数据源、证据类型与归档口径见
[数据库连接与证据网络](docs/DATABASE_CONNECTORS.md)。

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

### Open Babel 配体准备

配体可以直接上传已准备的 PDBQT，也可以在页面中选择“使用 Open Babel
准备”。自动准备只接受单个、最大 10 MB 的 `.smi`、`.smiles`、`.sdf`、
`.mol`、`.mol2` 或 `.pdb` 配体；不接收压缩包、多记录 SDF、多行 SMILES
或多 `MODEL` PDB，也不自动转换受体。

用户可选择是否生成三维坐标和是否按指定 pH 质子化；部分电荷固定使用
Gasteiger。系统只以受控参数数组调用 Open Babel，记录输入与输出 SHA-256、
Open Babel 版本与可执行文件 SHA-256、参数、退出码和耗时。非零退出、超时、
错误输出、多个分子、无效 PDBQT、全零或完全重合坐标都不会形成可供 Vina
使用的配体。成功输出可下载，并直接进入下方 Vina 任务清单。

Open Babel 3.2.1 使用 `GPL-2.0-only` 许可证。仓库只声明可选依赖，不捆绑
wheel 或二进制文件；如需把 Open Babel 与本项目一同再次分发，必须单独完成
许可证合规审查。自动转换不能替代对互变异构体、立体化学、质子化状态、
可旋转键和三维构象的人工核查。

### AutoDock Vina

页面接受上述 Open Babel 成功准备的配体 PDBQT，或用户上传的配体 PDBQT；
受体始终要求上传经人工核查的 PDBQT。用户还需填写配体与受体来源 accession、
版本、受体研究对象、搜索框、`exhaustiveness`、`num_modes`、随机种子和
Vina 版本。系统先生成不含任何分数的任务清单，之后有两条执行路径：

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

配体 PDBQT 必须有 `ATOM/HETATM`，并且只能包含一组完整的
`ROOT/ENDROOT/TORSDOF` 记录；受体至少要有 `ATOM/HETATM`。外部运行时应把
任务清单页面显示的
`VetEvidence-Manifest-SHA256: <digest>` 标记与 Vina 日志一起保存；导入时
该标记、Vina 版本、模式编号和 `num_modes` 必须同时通过校验。

项目不捆绑或下载 AutoDock Vina、Meeko 或结构数据库，也不会执行未通过身份
核验或未经用户选择的外部二进制程序。Open Babel 只作为固定版本的可选本机
配体准备依赖，不改变 Vina 的独立安装政策。结构准备与对接得分都属于计算
预测，不能单独证明结合、抗菌活性或药物协同。默认 Docker 镜像也不安装
`molecular-docking` 可选依赖。

### 科研级批量对接与可视化

v0.5 在原有单任务执行链之上增加一层不可绕过的科研门禁。受体审批记录必须
同时绑定原始结构与已准备 PDBQT 的 SHA-256，并明确选择模型和链、替代构象
策略、是否移除水、保留或移除哪些辅因子/金属/异源原子、受体研究对象与
NCBI TaxID、RCSB PDB ID/UniProt accession，以及搜索口袋的证据依据。审批
后任一输入、准备策略或搜索框发生变化，必须重新审批。

配体使用 PubChem CID + InChIKey，或显式的用户自有命名空间与来源文件哈希。
批处理按“一个配体 × 一个 seed”生成独立任务；只有任务清单哈希、Vina
可执行文件哈希与版本、seed、绑定日志、输出 PDBQT、解析模式和预测评分全部
一致时，才进入稳定性汇总。汇总只报告每个 seed 的 Vina 预测评分及其均值、
标准差、极差等描述性统计；当前不计算、也不声称跨 seed RMSD 或构象聚类。

每个已验证任务可生成只包含所选受体链与明确保留异源原子的复合物、可编辑
PML 和本地 3Dmol.js 三维视图。3Dmol.js 的固定 ES module、许可证与上游
元数据随仓库分发，不依赖运行时 CDN。Open-Source PyMOL 和 PLIP 都是用户
另行合法安装的可选外部程序，只有再次明确确认且任务/脚本/复合物哈希匹配
后才执行。PLIP 首版只固定生成 XML/TXT；对接图由已验证的 PyMOL 和本地
3Dmol.js 生成，避免 PLIP 图片工具失败拖垮相互作用报告。PNG 通过完整图像
解码校验后才标为可用；PSE 若未由同一已核验 PyMOL 重新打开验证，就只标为
`generated_unverified`，不伪装成已验证会话。

PDBQT 转回 PDB 会丢失部分键级、电荷和原子类型语义，因此 3D 展示与 PLIP
相互作用识别只作启发式解释；它们不能修复错误的质子化、配体化学状态或受体
准备，也不能替代实验。详细输入、审批、执行与产物契约见
[科研级分子对接工作流](docs/DOCKING_WORKFLOW.md)。

### OpenMM 分子动力学技术烟测

v0.6 只接受经研究者审核的参数化输入，不会从 PDBQT 或 SMILES 猜测 MD
化学体系。真实执行必须提供已裁剪为所选链、无 altloc 的单模型受体 PDB、
恰好一个 V2000 记录的配体 SDF、匹配的非周期 topology PDB、OpenMM
`System XML`、实际力场/参数文件、参数化工具与版本、准确命令参数，以及
包含链、残基、原子名、altloc、元素和源/拓扑索引的 canonical 原子映射。
缺失残基、质子化、互变异构、立体化学、形式电荷、金属、共价连接和非标准
残基仍由研究者负责；存在未解决的金属、共价连接或未知残基风险时阻断执行。

OpenMM 由独立 worker 执行最小化和 30 步短积分。每个有限步块检查取消状态，
页面保留进程句柄时还会在协作取消超时后终止 worker；CLI worker 另有 300 秒
硬截止。checkpoint 绑定 manifest、System、topology、seed、OpenMM 版本及
实际 Context 的平台、设备和精度指纹。恢复前重新核验所有绑定和 SHA-256；
成功结果重新加载时也会
校验产物清单。二进制 checkpoint 只保证在兼容 OpenMM、平台与硬件环境下
尝试恢复，portable state 不代表跨平台逐位可重复。

当前唯一分析输出是实际记录的温度和势能时间序列及其宽松数值健康检查；
`technical_smoke_passed` 只表示最小执行和规定产物通过。v0.6 不提供科研级
NVT/NPT、production、多重复、收敛、RMSD/RMSF、回转半径、接触、氢键、
压力、密度、MM/GBSA、MM/PBSA、FEP、ABFE 或任何结合自由能。详细边界见
[分子动力学 technical smoke](docs/MOLECULAR_DYNAMICS.md)。

## 配置与期刊分区

如需填写 NCBI 联系邮箱、API Key、STRING 调用方身份或 DAVID 注册邮箱，
可在启动前为当前终端设置：

```powershell
$env:NCBI_EMAIL = "your_email@example.com"
$env:NCBI_API_KEY = "your_api_key"
$env:STRING_CALLER_IDENTITY = "VetEvidenceAI"
$env:DAVID_EMAIL = "registered_email@example.com"
$env:VINA_EXECUTABLE = "C:\path\to\vina.exe"
$env:OPENBABEL_EXECUTABLE = "C:\path\to\obabel.exe"
$env:PYMOL_EXECUTABLE = "C:\path\to\pymol.exe"
$env:PLIP_EXECUTABLE = "C:\path\to\plip.exe"
$env:PLIP_BABEL_LIBDIR = "C:\path\to\OpenBabel\bin"
$env:PLIP_BABEL_DATADIR = "C:\path\to\OpenBabel\share\openbabel\3.1.0"
```

`.env.example` 仅说明变量名；不要提交真实 `.env` 或密钥。

项目默认按 PubMed 返回的 ISSN 查询 LetPub，并缓存 7 天。LetPub 不可用时依次使用过期缓存和本地 CSV 回退。可通过 `LETPUB_LOOKUP_ENABLED=false` 关闭动态查询，或用 `JOURNAL_RANKINGS_CSV` 指向机构授权数据。详细口径见[期刊分区数据说明](docs/JOURNAL_RANKINGS.md)。

## 验证

运行全量自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

自动化测试覆盖文献证据准入、实验分析、网络文件适配与导出、Open Babel
单配体准备、受体审批、类型化结构身份、批量多 seed Vina 任务与强绑定产物、
3Dmol.js/PyMOL/PLIP 降级路径，以及数据库请求脱敏、真实字段解析、TaxID
门禁、证据分层、原始响应归档和审计留痕。MD 测试另覆盖输入与 manifest
绑定、System/topology/source mapping 复核、后台任务状态、取消、
checkpoint/resume、资源与平台门禁、产物哈希和只允许真实温度/势能健康
检查的分析边界。定向评测是受控工程检查，不代表通用模型准确率。

官方 AutoDock Vina `1IEP` 示例只用于技术烟测：核对真实进程、日志、构象、
预测评分和哈希能否贯通，不能把示例结构或分数写入本项目的兽医科研结论。

本机在 VetEvidence Windows CUDA 依赖预加载后运行 OpenMM 8.5.2 官方
`testInstallation`，Reference、CPU、CUDA 与 OpenCL 均通过 force 差异容差。
完全公开的合成两原子 N+C fixture 已分别强制 CPU 与 CUDA 完成
单重复 30 步真实技术烟测，两者各产生 6 个真实温度样本和 6 个真实势能样本
并通过 QC；CUDA 实际使用 RTX 5070 Laptop GPU 的 `DeviceIndex=0` 与
`mixed` 精度。该 fixture 不是生物分子科研体系，这些结果只验证运行环境和
最小执行链，不代表体系稳定、采样收敛或存在结合。

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
- MD 任务、原始输入、参数化输入、attempt、checkpoint 和结果清单保存在
  `.workbench/md/`，读取时复核路径边界、清单和 SHA-256；
- 报告中的文献结论关联 PMID、DOI、来源 URL 或原文片段；实验分析关联 CSV
  文件名、SHA-256、原始行号和可复算过程；计算预测独立记录关系数据、类型化
  配体/受体身份、受体人工审批、结构和 Vina 输出的版本、seed、参数、日志、
  构象与 SHA-256；Open Babel 配体准备另记录输入/输出哈希、版本、可执行文件
  哈希、参数、退出码和耗时。本机 Vina 成功执行还记录强绑定产物；可视化记录
  PML、复合物、PNG/PSE/PLIP 状态及哈希，但证据等级仍为
  `computational_prediction`。

## 项目文档

- [产品需求](docs/PRD.md)
- [架构说明](docs/ARCHITECTURE.md)
- [数据库连接与证据网络](docs/DATABASE_CONNECTORS.md)
- [科研级分子对接工作流](docs/DOCKING_WORKFLOW.md)
- [分子动力学 technical smoke](docs/MOLECULAR_DYNAMICS.md)
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
默认 Docker 镜像不包含 AutoDock Vina、Open Babel、Open-Source PyMOL、
PLIP 或 OpenMM 可选环境；如需使用这些程序，必须另行合法安装或挂载并显式
配置。仓库只捆绑固定版本的 3Dmol.js 前端资产及其许可证/上游元数据。未提供
Vina 时仍可生成任务清单；未提供 PyMOL/PLIP 时仍可下载 PML、复合物和查看
本地 3Dmol.js 视图；未提供 OpenMM 或经过审核的参数化输入时，MD 页面只报告
依赖/输入缺口，不伪造轨迹或分析结果。

## 当前边界

- 首个垂直场景只覆盖候选药物与抗生素协同作用，不代表支持所有实验类型；
- 当前只解析 PubMed 摘要和 RIS、EndNote、RefWorks 题录导出，不读取未授权全文，也不支持扫描 PDF/OCR；
- 用户导入记录的正确性仍需人工核查，系统不会把缺失信息补造为事实；
- FICI 和生长曲线只做透明、可追溯的描述性分析；
- 数据库关联、STRING 网络和 DAVID 富集不等于靶点或通路已经实验验证；
  零结果也不等于生物学关系不存在；
- 网络药理学和分子对接只属于计算预测层，不进入直接文献或实验协同证据；
- v0.6 分子动力学仅为单重复 30 步 OpenMM 技术烟测，只检查真实温度和势能
  序列是否满足最小数值安全条件；它不是 NVT/NPT/production，不生成
  RMSD/RMSF、回转半径、接触、氢键、压力、密度或自由能，也不能证明稳定
  结合、抗菌活性或协同；
- Vina 的预测评分不是实验结合能；多个 seed 只形成描述性评分稳定性，不提供
  当前尚未计算的跨 seed RMSD 或构象簇；
- PDBQT→PDB 会损失部分化学语义，PLIP 和三维图因此只能作启发式解释；生成
  的 PNG/PSE 还必须按各自校验状态展示，不能用“有文件”冒充“已验证”；
- 当前只在用户主动提交后从公开接口获取数据库记录，不自动运行未知在线
  预测服务或下载 Vina；Open Babel 只转换用户主动提供的单个配体，且不准备
  受体。Agent 只在用户明确选择后受控执行已发现且通过版本与哈希核验的本机
  工具。用户仍须合法取得输入并核查结构、互变异构体、立体化学、质子化
  状态和搜索框；
- 直接文献证据规则宁可漏报也不误报：要求同一题名或摘要句明确出现研究对象、两种干预、交互指标和结果；仅描述 checkerboard、time-kill 或 FICI 方法与阈值不构成结果证据，缩写、同义词或跨句表达可能需要人工复核；
- 合成演示数据只用于验证计算和页面流程，不得进入科研结论；
- 规则提取无法覆盖所有摘要表达方式，冲突检测也只识别显式方向差异；
- 系统不训练或微调模型，不使用多 Agent 框架、向量数据库或复杂权限系统；
- 当前仅面向可信的单用户本机运行；完整运行 ID 在本机恢复流程中相当于访问凭证，不适合直接用于共享部署；
- 决策报告必须人工复核，不能替代全文核查、原始数据审计或验证性实验。
