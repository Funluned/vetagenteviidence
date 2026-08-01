# 执行检查点

## 当前阶段

VetResearch Workbench v0.7 阶段 2 已完成：独立版本化的 27 题离线评测集、
理想产品金标准、七项固定指标、评分运行器和 `rules_v1` 快照已经落盘并可复跑。
阶段提交 `b61d742` 已推送至 `codex/vetresearch-workbench`，GitHub Actions
运行 `30698205011` 的 Ubuntu、Windows、macOS 三平台均通过。
当前停在阶段 3 开始前；真实 LLM、RAG、单 Agent 与双 Agent 尚未实现或调用。
孙奇本人从零安装、讲解、修改和排错验收已按决定延期到 GitHub 工程状态稳定后，
不作为当前门禁。

## 2026-08-01 v0.7 阶段 2 评测集与规则基线（已完成）

### 本阶段交付契约

- [x] 固定 `v0.7.0` 的 27 个合成离线场景，九类边界各 3 题；输入、金标准、
  评分方法和边界分别落盘且一一对应。
- [x] 所有文献式标识为 `SYN-*`、URL 为 `example.invalid`，不冒充真实论文、
  PMID、DOI、实验数据或科研事实。
- [x] 真实运行现有 `rules_v1`，保存每题实际结果、七项指标、数据与实现哈希；
  快照复核会重算内容哈希。
- [x] 全程不联网、不启用或调用 LLM，不实现 RAG 或 Agent，不读取 API Key。
- [x] 规则基线允许失败，CI 门禁检查可复现性，不要求把现有规则改成 27/27。

### 基线与已知差距

- 规则基线 `20/27`，评测运行错误 0；冻结回放 Recall@3 `3/5`、合成
  claim-citation Precision `4/7`、Unsupported Claim `3/7`、Abstention
  `20/25`、Task Completion `26/27`，LLM 调用、Token 与 API 费用均为 0。
- `DIR-02` 暴露中文病原名开头“无”被否定规则误伤；`CIT-01~03` 暴露回答器
  不复核上游 claim 与原句；`INJ-01~02` 暴露来源内容投毒可污染准入；
  `TOOL-02` 暴露多路检索缺少部分成功保留。
- `CIT-*` 是预造冲突结构化记录的信任边界测试，不表示规则提取器自行产生了
  三条错误科研结论；`INJ-03` 也只证明当前 CSV 文本列不会触发外部动作。
- 旧版 30 条实时单查询字段评测保持不变，不能用其 `30/30` 替代 v0.7 产品
  边界成绩；冻结回放 Recall 也不能写成实时 PubMed 或 RAG 召回率。

### 当前边界

- 金标准仍标记为 `engineering_gold_pending_domain_expert_review`；工程结构已
  固化，但正式科研语义仍需领域人工复核。
- 阶段提交 `b61d742` 已按确认推送；本地 HEAD 与远端开发分支 SHA 完全一致。
- 下一阶段才允许设计 LLM Provider、RAG 和单 Agent；本阶段没有提前实现。

### 验证证据

- v0.7 专项、旧评测兼容与制品回归：`15 passed`。
- 最终全量自动回归：`449 passed, 1 skipped in 24.64s`；跳过项是既有平台条件。
- `pip check`、Python 编译、基线快照重放校验和 `git diff --check` 均通过。
- 远端 CI [`30698205011`](https://github.com/Funluned/vetagenteviidence/actions/runs/30698205011)
  全绿：Ubuntu `447 passed, 3 skipped`，Windows `446 passed, 4 skipped`，
  macOS `447 passed, 3 skipped`。

## 2026-08-01 v0.7 阶段 1 跨平台修复与三系统 CI（已完成）

### 本阶段交付契约

- [x] 保留生产端对外部可执行文件的 POSIX `X_OK` 安全检查，只修正测试夹具权限，并增加不可执行负例。
- [x] provenance 文件名归一化不依赖宿主系统，覆盖 Windows、POSIX、UNC 与混合分隔符。
- [x] Ubuntu、Windows、macOS 使用 Python 3.11 执行依赖安装、编译检查与全量 pytest。
- [x] SHA-256 固定的 vendored 资产在 Windows clone 中保持原始字节，不用弱化哈希断言换取通过。
- [x] 不提前加入真实 LLM、RAG 或 Agent 代码，不调用付费模型或外部敏感数据。

### 修改与验证证据

- 功能与 CI 提交：`b8dd513 修复跨平台测试并建立三系统 CI`。
- 首次 CI 仅 Windows 因 Git 自动换行转换导致固定资产 SHA-256 不一致；Ubuntu 与 macOS 已通过，业务测试没有跨平台逻辑失败。
- 收口提交：`003e2e0 固定哈希资产的跨平台检出字节`，通过 `.gitattributes` 的精确 `-text` 规则保护 vendored 资产，并新增项目制品回归。
- 修复前干净 Windows clone 将工作树哈希从 `95513f64…6d427` 改为 `38630fd5…9011`；修复后保持 `w/lf attr/-text` 且哈希与固定值一致。
- 最终本地全量回归：`441 passed, 1 skipped in 23.66s`；`pip check`、Python 编译检查和 `git diff --check` 通过。
- 远端 CI [`30695477003`](https://github.com/Funluned/vetagenteviidence/actions/runs/30695477003) 全绿：Ubuntu `439 passed, 3 skipped`，Windows `438 passed, 4 skipped`，macOS `439 passed, 3 skipped`。
- 全量自动测试只证明当前测试集与三套 runner 未发现回归，不证明真实外部工具、科研结论、用户价值或商业价值。

### 当前边界

- 阶段 1 已关闭；下一阶段是独立版本化评测集和规则基线，不在本阶段改动范围内。
- 当前 Provider 仍是规则实现，PubMed 流程仍不是混合检索 RAG，现有 Agent 仍是受控顺序工作流。
- 没有创建或合并 PR、发布 Release、修改仓库设置，也没有触发付费模型调用。

## 2026-08-01 结局指标重复输入校验（已完成）

### 本阶段交付契约

- [x] 结局指标继续复用现有 Unicode、大小写与标点归一化规则。
- [x] `FICI`、`fici`、`F-I-C-I` 等归一化后相同的重复写法在输入校验阶段明确报错，不进入任务创建。
- [x] 错误同时保留首次写法与当前重复写法，要求用户合并后重试，不静默删除或改写科研指标。
- [x] 不同的正常指标及既有 Unicode／连字符名称继续通过。

### 验证证据

- 修改范围：`src/vetevidence/input_validation.py`、`tests/test_input_validation.py`。
- 输入校验专项：`7 passed`。
- 工作台相关回归：`48 passed`。
- 最终全量自动回归：`428 passed`，无失败。
- `git diff --check` 通过；本次未修改 PubMed、排序、模型、数据库或界面结构。
- 全量自动测试只证明当前测试集未发现回归，不能证明真实界面、外部服务、科研结论、用户价值或商业价值。
- 本次只创建本地提交，不推送 GitHub。

## 已确认环境

- 工作目录：已有空 Git 仓库，直接作为项目根目录使用。
- Python：3.11.15。
- Git：2.55.0.windows.2。
- PubMed 网络：NCBI ESearch 请求成功。
- 示例检索：`Streptococcus agalactiae mastitis quercetin` 返回真实 PMID。

## 本阶段验收

- [x] 依赖安装成功
- [x] PubMed 客户端返回真实数据
- [x] Streamlit 页面能启动
- [x] 首批测试全部通过
- [x] 孙奇确认页面运行结果无问题
- [x] Pydantic 证据模型覆盖任务书要求字段
- [x] 缺失字段保持为空
- [x] 证据回答带 PMID、DOI 和来源原句
- [x] Markdown 与 CSV 导出可用
- [x] 建立 30 条定向评测并生成 JSON/Markdown 报告
- [x] 临时 NCBI 故障自动重试
- [x] 页面显示请求次数、LLM 零成本和分类评测结果
- [x] 文献列表、证据表和导出同时显示中科院 2025 年 3 月升级版与 WOS JIF 分区
- [x] 按 PubMed ISSN 动态查询每种期刊的 LetPub 公开页
- [x] 展示 JIF 收录集、Q1—Q4 和分类名次
- [x] LetPub 查询支持 7 天缓存、过期缓存和本地 CSV 回退
- [x] 支持通过 `JOURNAL_RANKINGS_CSV` 替换为机构授权数据
- [x] 未匹配期刊明确显示“未收录”

## 验证证据

- 自动测试：`21 passed`，新增 LetPub HTML 解析、缓存和失败回退测试。
- 定向评测：`30/30 passed`；报告明确说明不是通用准确率。
- 依赖检查：`No broken requirements found`。
- 密钥扫描：未发现疑似凭证字符串。
- 真实查询：`quercetin Streptococcus agalactiae mastitis` 返回 2 篇文献。
- LetPub 实测：目标论文所在期刊显示中科院农林科学 3 区、兽医学 3 区，以及 WOS JIF Q2（SCIE，54/170）。
- LetPub 实测：`Animals` 显示中科院农林科学 2 区、两个小类 2 区，以及两个 WOS JIF Q1 分类。
- GitHub：项目已上传至 `https://github.com/Funluned/vetagenteviidence`，默认分支为 `main`。
- 目标论文：PMID `42250334`，DOI `10.1016/j.rvsc.2026.106289`。
- 目标论文提取：小鼠、样本量 25、Quercetin、`25, 50, 100 mg/kg`、腹腔注射、`24 h`、NF-κB、NLRP3、铁死亡及来源原句。
- 页面验收：文献列表、证据表、证据回答、评测、导出五个标签页均已实际检查。
- 异常验收：空白检索词会显示“检索词不能为空”，不会抛出未处理异常。
- 本地服务：`http://localhost:8501`。

## VetResearch Workbench v0.2 首个垂直闭环

### 已完成能力

- [x] 用本地标签 `v0.1.0` 冻结 VetEvidence AI 基线，并在 `codex/vetresearch-workbench` 开发
- [x] 把科研问题拆成 2—4 条带生成规则、验证方法和成功标准的可检验假设
- [x] 自动生成并执行候选干预、对照干预、联合/协同三类 PubMed 检索式
- [x] 扩大各轮 PubMed 候选池，轮询公平合并、PMID 去重并在最终截断前优先保留当前问题的直接文献证据
- [x] 将文献分为直接证据、间接背景和无关，逐篇显示判定理由与判定原句
- [x] 直接文献证据为 0 时生成文献证据不足报告，禁止间接文献支撑协同文献结论
- [x] 支持 RIS、EndNote、RefWorks 题录导入及 DOI/标题年份去重
- [x] 非 PubMed 来源使用通用来源 ID，不伪造 PMID
- [x] 形成物种、模型、样本量、干预、剂量、时间、对照和指标的实验条件矩阵
- [x] 只在同一主题存在显式相反方向时标记冲突，并逐项展示证据空白
- [x] 校验并分析 FICI CSV，保留原始行、错误、FIC、FICI 和阈值分类，并核对每行药物身份
- [x] 校验并分析生长曲线 CSV，计算时间点均值、标准差、重复数和梯形 AUC
- [x] 生成带 PMID、DOI 或 CSV 文件名、SHA-256、原始行号与计算过程的结论、风险和下一步实验建议
- [x] 保存任务事件、工具调用、失败、重试关系和人工复核到 `.workbench/runs`
- [x] 提供历史运行恢复和 Markdown/JSON/完整快照下载
- [x] 合成演示数据始终标记为不可作为科研证据

### 新阶段验证证据

- 全量自动测试：`78 passed`；其中 v0.1 原有 21 项保持通过。
- 真实三轮 NCBI 检索：
  - `quercetin Streptococcus agalactiae`
  - `amoxicillin Streptococcus agalactiae`
  - `quercetin amoxicillin Streptococcus agalactiae (synergy OR interaction OR combination)`
- 扩大候选池后三轮分别返回 7、24、0 条；公平合并 31 个候选并按证据等级稳定分桶后保留 8 篇间接背景，前两轮各贡献 4 条。
- 上述真实负例的 8 篇文献均为间接背景，直接文献证据为 0；报告状态为 `blocked_no_direct_evidence`。
- 真实正例 `florfenicol + thiamphenicol / Pasteurella multocida` 保留 8 篇文献，严格规则只准入 PMID `31749775`（DOI `10.3389/fmicb.2019.02430`）。
- 正例扩大候选池后三轮返回 24、24、7 条，融合 40 个唯一候选；最终仍为直接文献证据 1、间接背景 7。
- 浏览器实测正例显示“直接文献 1、间接背景 7”，报告 `EvidenceAdmission=admitted` 且只用 PMID `31749775` 支撑文献结论。
- 回归测试覆盖首轮占满名额、跨查询重复不消耗贡献机会、空查询与耗尽查询回填，以及全局数量上限。
- 浏览器实际验收五步页面：问题与假设、文献证据、实验数据、决策报告、运行记录。
- 浏览器实际运行合成 RIS 导入、FICI、生长曲线、报告、人工复核和历史恢复。
- FICI 演示数据保留 3 个有效行；生长曲线演示计算 `combination AUC=0.93`、`control AUC=2.92`。
- schema v4 会按含“明确结果”的新准入规则重建旧条件，并让旧评估和报告安全失效；新快照仍保留事件、工具调用和人工复核链。
- 来源模型、无效 CSV 隔离、时区校验、快照迁移及报告内容哈希均有回归测试。
- Windows 下快照原子替换的短暂文件锁已增加有限重试，并完成浏览器与磁盘落盘验收。
- GitHub 开发分支 `codex/vetresearch-workbench` 已推送，远端提交、树哈希与本地 HEAD 完全一致；`v0.1.0` 基线标签已同步。

## VetResearch Workbench v0.3 机制预测与证据门槛

### 已完成能力

- [x] 科研问题同时校验自然语言和结构化研究范围，研究对象、两种干预与结局不得为空或互相矛盾
- [x] 文献准入升级为 `interaction-evidence-v2`：排除目的、假设、方法、术语定义、计算预测与不确定措辞，支持受控相邻句结果指代、中文匹配及协同/拮抗冲突
- [x] “主题不匹配”文献与直接证据、间接背景隔离，不能进入研究回答或建议
- [x] FICI CSV 强制包含药物对与菌株/种群身份，生长曲线强制包含干预、对照、时间点及研究对象，并对非有限数值和范围错配安全阻断
- [x] 网络药理学只连接带物种、稳定靶点编号和来源版本的两张用户输入表；两种当前干预必须实际进入当前物种的交集，混入其他化合物或物种会被拒绝；结果保留 SHA-256、原始行号、来源及透明排序公式
- [x] 化合物—靶点与靶点—通路输入均支持 CSV、XLSX、DOCX；三种格式统一使用固定顺序表头并拒绝重复/额外列，Office 文件另拒绝公式、合并/嵌套表格和多非空工作表/表格，结果可导出 XLSX 与 DOCX
- [x] 分子对接准备清单固定记录经最小 PDBQT 内容校验的配体/受体哈希、结构编号和版本、物种、网格、随机种子、Vina 版本及 canonical 清单哈希
- [x] 只有任务清单哈希与版本匹配、且含合规 mode/affinity 表的用户导入输出才能形成 docking 结果；准备清单本身不产生分数
- [x] Agent 可在用户选择后执行已核验的本机 AutoDock Vina；执行前后复核版本与可执行文件 SHA-256，固定参数、禁用 shell、限制超时，并原子保存绑定日志、输出 PDBQT 和元数据；UI 缓存探测结果且禁止外部日志覆盖已有本机执行审计
- [x] 配体可上传已准备的 PDBQT，或由 Open Babel 3.2.1 从单个 SMI/SMILES、SDF、MOL、MOL2、PDB 受控准备并直接交给 Vina；受体仍须上传人工核查的 PDBQT
- [x] Open Babel 输入限制为单分子和 10 MB，支持可选 3D 与 pH 质子化，电荷固定为 Gasteiger；多分子、超时、工具错误、无效或退化坐标均不产出可用 PDBQT
- [x] Open Babel 审计保留输入/输出 SHA-256、版本、可执行文件 SHA-256、受控参数、退出码和耗时
- [x] 计算预测在快照与决策报告中单独成节，不等同于实验或直接文献证据，也不能单独触发科研建议
- [x] 快照升级到 schema v6；旧快照迁移时按新规则重建文献条件，并让不再可信的旧评估、报告与人工复核安全失效

### 验证证据

- 全量自动测试：`187 passed`。
- 浏览器实际验收六步页面：问题与假设、文献证据、实验数据、机制预测、决策报告、运行记录。
- 浏览器加载合成网络演示后显示 2 个输入化合物、1 个目标物种、2 个交集靶点和 2 条通路；页面同时显示 CSV/XLSX/DOCX 模板、10 MB 上传限制、XLSX/DOCX 结果下载及本机 Vina 1.2.7 身份信息。
- 浏览器完成合成 RIS、范围匹配的 FICI 分析和报告生成；报告单列“计算预测（不等同于实验或直接文献证据）”，页面无未处理异常。
- 单元测试覆盖网络表必填列、物种/靶点连接、来源哈希、范围错配、多格式等价输入、恶意 Office 结构、公式注入防护、Vina 清单、任务哈希与版本一致性、本机执行失败路径、产物原子保存及禁止凭空生成 docking 分数。
- Excel 结果和两种模板已用独立工作簿引擎逐页检查，4 个结果页均无公式，来源 SHA-256、靶点排名和通路名称完整。
- 使用 AutoDock Vina 官方 v1.2.7 `1iep` 示例实际运行：退出码 0，解析 2 个模式，最佳 affinity `-8.663 kcal/mol`；该数值仅证明执行链可运行，不是本项目科研结论。

### Open Babel 配体准备增量验证

- 官方 PyPI Windows CPython 3.11 x64 wheel `openbabel==3.2.1` 已安装到项目 `.venv`；下载文件 SHA-256 为 `04c64bd8db520046abdc01396ee122b3b32deb08bdd2fa136a16228ffad7bf8c`，`pip check` 未发现依赖冲突。
- 本机真实单分子 SMILES → PDBQT 已通过，输出含非零三维坐标并可进入既有 Vina 配体校验链。
- 仓库不捆绑 Open Babel wheel 或二进制。Open Babel 为 `GPL-2.0-only`，二次分发须单独完成许可证合规。
- 本轮全量自动测试：`215 passed`。

## VetResearch Workbench v0.4 公开数据库证据层

### 已完成能力

- [x] 用户主动查询 PubChem、UniProt、NCBI Gene/GenBank、RCSB PDB、
  STRING 和 DAVID，并统一记录 CID/InChIKey、UniProt accession、GeneID、
  GenBank accession.version、PDB ID 与 NCBI TaxID
- [x] 数据库页改为单库三步式检索：先选数据库，再输入名称或标准编号，
  最后联网检索或生成离线请求；需要物种时从常用兽医物种选择或填写 TaxID，
  不再静默默认成人类
- [x] RCSB PDB 同时支持直接按 PDB ID 获取结构，以及用 UniProt accession
  加 TaxID 搜索结构候选
- [x] NCBI 缺联系邮箱时不发请求；STRING/DAVID 未明确同意标识外发时只生成
  带参数和 SHA-256 的离线请求
- [x] 每个查询按运行 ID 和查询 ID 保存原始响应、规范化结果、解析器版本、
  ETag/Last-Modified、来源时间、清单和 `SHA256SUMS.txt`，拒绝覆盖已有归档
- [x] 联网有结果、联网无结果、未发送的离线请求、已返回但有警告四种
  连接器状态与本地归档成功分别显示，不再用“已归档”代替查询结论
- [x] 页面刷新或会话重建时扫描当前运行的本地归档；只有 manifest、结果
  与原始响应 SHA-256 全部通过校验的最近 100 条会恢复，坏项逐项警告
- [x] STRING 实验、人工整理、文本挖掘与计算预测通道分开显示，
  `combined_score` 只用于排序
- [x] DAVID 保留明确目标集与背景集、TaxID、映射比例、原始 P 值和上游
  BH 校正值；缺失校正值时标记未报告，不在筛选后的不完整集合上补算
- [x] 建立 DAVID 基因—条目注释边及 STRING 标识映射边；不同 TaxID、同层
  混合 TaxID 或无法证明的跨库身份不会被合并
- [x] Streamlit 工作台“数据库证据”提供状态、记录、标识映射、
  来源 URL、原始响应哈希、JSON/ZIP/离线请求下载和工具调用审计

### 验证证据

- v0.4 独立全量回归：`251 passed`；连同尚未提交的后续对接与 MD 核心测试
  预跑为 `289 passed, 1 skipped`。
- 真实 PubChem：`quercetin` 解析为 CID `5280343` 和 InChIKey
  `REFJWTPEDVJJIY-UHFFFAOYSA-N`。
- 真实 UniProt：`P69905` 返回 TaxID `9606`，release `2026_02`。
- 真实 RCSB 搜索：`P00533 / TaxID 9606` 返回 `1IVO`、`1M14`、`1M17`；
  单库界面的 UniProt→PDB 执行分支已通过 AppTest 调用契约验证。
- 真实 STRING：`P69905 + P68871 / TaxID 9606` 使用固定版本 `12.0`，
  返回 1 条关系、7 条分层证据边和 1 条仅排序关系。
- NCBI 与 DAVID 本轮未使用真实联系邮箱或注册邮箱；已实测安全离线导出，
  不把未执行的网络请求写成在线成功。
- 浏览器实测七个顶层标签；创建任务后完成真实 PubChem 查询、原始归档与
  JSON/ZIP 下载入口，并完成 NCBI 无邮箱离线请求与下载；控制台无错误。
- 本轮数据库连接器、归档、证据网络、单库 UI 与项目文档专项回归：
  `51 passed`；浏览器再次完成真实 PubChem 查询并核对结果优先页面。

## VetResearch Workbench v0.5 分子对接科研链（已完成）

### 本阶段交付契约

- [x] 受体审批绑定原始结构与准备后 PDBQT 哈希、RCSB PDB ID/UniProt
  accession、NCBI TaxID、模型/链、altloc、水、辅因子/金属、异源原子、
  准备工具与口袋依据；任一绑定字段改变即拒绝复用审批
- [x] 配体使用 PubChem CID + InChIKey，或带来源哈希的显式用户命名空间；
  名称不能代替稳定身份
- [x] 对多个配体和多个 seed 建立独立尝试；Vina manifest、二进制身份、
  seed、日志、pose、预测评分与 SHA-256 必须强绑定后才能汇总
- [x] 固定本地 3Dmol.js ES module、许可证和上游元数据；三维复合物只包含
  已选择受体内容与唯一配体 chain/resid，并可下载编辑 PML
- [x] Open-Source PyMOL 与 PLIP 仅在用户再次确认且任务/脚本/复合物哈希
  匹配后调用；PNG 完整解码验证，PSE 未经同一核验 PyMOL 重开时降级为
  `generated_unverified`
- [x] 所有 Vina 表、评分、三维图与 PLIP 结果固定标记为
  `computational_prediction`；不声称当前未计算的跨 seed RMSD 或构象簇
- [x] 明示 PDBQT→PDB 会损失部分键级、电荷和原子类型，PLIP/三维图只作
  启发式解释
- [x] 仓库不捆绑 Vina、Open Babel、Open-Source PyMOL 或 PLIP；只捆绑固定
  本地 3Dmol.js 资产及其许可证/上游元数据

### 阶段验证状态

- 官方 AutoDock Vina `1IEP` 公开示例已通过 v0.5 完整强绑定链：Vina 1.2.7、
  seed 42、最佳预测评分 `-8.663 kcal/mol`；PyMOL PNG 为 `available`，
  PSE 为 `generated_unverified`，PLIP XML/TXT 为 `available`。
- PLIP 3.0.0 使用已审计的 Open Babel runtime，XML 明确绑定
  `LIG:Z:9999`；首版不调用 PLIP 自带图片路径，图片由已验证的
  PyMOL/3Dmol 生成。
- 同步页面任务限制为最多 24 次 Vina 尝试、384 工作单位和 100 MB 配体
  总量；全失败、部分成功和完整成功分别写入不同审计状态。
- v0.5 阶段专项回归：`40 passed`；当时排除尚在开发的 v0.6 MD 文件后，
  既有能力回归为 `287 passed`。
- Streamlit AppTest 启动异常为 0；浏览器实测八个顶层标签、受体门禁、
  配体模板、多 seed 参数、Vina/Open Babel 身份与分子对接页面状态。
- 上述公开案例只证明工程与产物绑定链可运行，不作为兽医科研证据或结合结论。

## VetResearch Workbench v0.6 OpenMM 技术烟测（已完成）

### 本阶段交付契约

- [x] 协议固定为 OpenMM 8.5.2、单重复、单 seed、30 步
  `technical_smoke`；`scientific_interpretation_allowed=false`
- [x] 已选链、无 altloc 的单模型受体 PDB、单记录 V2000 配体 SDF、
  `System XML`、topology、实际力场文件、准备工具/命令和逐原子 canonical
  source→topology 映射均强绑定 SHA-256
- [x] OpenMM 反序列化后复核实际粒子数、force/constraint 类型与数量、
  topology 原子数；周期 System 或带 CRYST1 的 topology 在 v0.6 阻断
- [x] Streamlit 只提交和轮询独立 worker；job 状态用跨进程文件锁与 revision
  CAS，支持分块取消、进程句柄超时终止、300 秒 CLI 硬截止、checkpoint、
  恢复和遗留 worker 状态校正
- [x] checkpoint 绑定 manifest、System、topology、replica、seed、step、
  OpenMM 版本和实际 Context 平台/设备/精度指纹；成功结果与下载前均复核
  路径及工件 SHA-256
- [x] 记录实际 OpenMM 平台、设备、精度、平台属性、随机种子和力场哈希；
  驱动仅在后端报告时记录，`gpu_required=true` 时不允许静默回退 CPU
- [x] 只把真实温度与势能序列列入 `produced_metrics`；RMSD、RMSF、Rg、
  contact、Hbond、pressure、density 和自由能明确标为未生成

### 真实环境与执行验收

- VetEvidence 完成 Windows CUDA 依赖预加载后运行 OpenMM 8.5.2 官方
  `testInstallation`，Reference、CPU、CUDA 与 OpenCL 均通过 force 差异容差。
- 正式 `scripts/run_md_smoke.py` 使用完全公开的合成两原子 N+C 数值 fixture，
  通过与产品相同的 job store、准备输入、worker、QC 与工件复核链分别强制
  CPU 和 CUDA 执行。
- CPU：实际平台 `CPU`，30 steps，真实温度 6 个样本、势能 6 个样本，
  QC passed；不可变结果目录为
  `C:\Users\sunqi\AppData\Local\VetEvidence\md-smoke-results\v06-final-cpu-20260730T185000`。
- CUDA：实际平台 `CUDA`，设备
  `NVIDIA GeForce RTX 5070 Laptop GPU`，`DeviceIndex=0`、`mixed` 精度，
  30 steps，真实温度 6 个样本、势能 6 个样本，QC passed；不可变结果目录为
  `C:\Users\sunqi\AppData\Local\VetEvidence\md-smoke-results\v06-final-cuda-20260730T185000`。
- v0.6 文档、代码、测试与 smoke 脚本的项目制品契约：
  `tests/test_project_artifacts.py` 为 `4 passed`。
- 最终全量自动回归：`352 passed`；`pip check` 无依赖冲突，
  `git diff --check` 通过。
- 上述 fixture 不是蛋白—配体科研体系；通过只表示技术完整性和最小数值
  健康，不能解释构象稳定性、结合、抗菌活性、协同或自由能。

## 数据库许可接入与人工导入增量（已完成）

### 本阶段交付契约

- [x] 数据库证据页由 7 个公开入口扩展为 12 个混合访问入口，新增 OMIM、
  DrugBank、GeneCards、MalaCards 与 SwissTargetPrediction。
- [x] OMIM 仅在配置 `OMIM_API_KEY` 后调用官方 API；未配置时只生成可审计的
  离线请求，不伪装为在线命中。
- [x] DrugBank 同时要求 `DRUGBANK_API_KEY` 与用户明确确认机构许可；支持按
  DrugBank ID 或名称查询、分页药物—靶点关系，并批量补充 BioEntity 的
  UniProt、NCBI TaxID 和基因名称。分页被截断或详情补充失败时降级并警告，
  不伪造稳定标识。
- [x] GeneCards 与 MalaCards 只接收用户声明的授权 CSV/TSV/XLSX 导出；
  SwissTargetPrediction 只接收用户声明为本人手工生成且允许使用的预测结果；
  三者均不实施页面抓取或后台自动化。
- [x] 访问方式与证据等级分别记录：官方公开 API、凭证门控 API、授权人工
  导入和手工预测导入不混为一类；SwissTargetPrediction 始终标记为
  `computational_prediction`。
- [x] 导入结果记录用户声明的来源真实性、条款 URL、文件 SHA-256、访问/
  导入时间、解析器版本和原始材料归档；未核验来源不写成平台独立认证。
- [x] XLSX 导入强制启用 `defusedxml`，并拒绝外部实体、压缩包路径穿越、
  非清单原始材料和不安全文件名。
- [x] 当前机制证据网络仍只消费已定义语义的 STRING/DAVID 结果；新增五类
  来源先作为可追溯数据库证据展示，不静默进入网络排名。

### 验证证据

- 新增数据库与归档专项回归：`97 passed`。
- 最终全量自动回归：`413 passed`；`pip check`、Python 编译检查和
  `git diff --check` 均通过。
- 已确认运行时 `openpyxl.xml.DEFUSEDXML=True`，恶意 XML 实体 XLSX、
  缺少安全 XML 解析器和 ZIP 路径穿越均有失败关闭测试。
- 本轮未使用真实 OMIM/DrugBank 密钥或商业账号；在线请求契约使用模拟响应
  验证，缺凭证和缺许可门禁使用真实离线路径验证，不能据此声称商业服务
  账号已在线验收。

## 阻塞

- 本机未安装 Docker：Dockerfile 已写，镜像构建未验证。
- 本机已安装并核验 AutoDock Vina 1.2.7，Agent 执行链已用官方公开样例验收；尚未提供与当前兽医科研问题对应、来源许可清楚且人工完成结构准备的真实配体/受体，因此不能把冒烟结果当作科研机制证据。
- Open Babel 真实转换只证明配体准备执行链可运行；尚未由研究者完成与当前科研问题对应的互变异构体、立体化学、质子化、构象和受体准备复核，不能把生成结构或后续对接当作结合、抗菌或协同证明。
- v0.5 尚无与当前兽医科研问题匹配、来源许可明确且经研究者逐项审批的
  受体/配体案例；官方 `1IEP` 只能用于技术烟测，不能填补该科研案例空白。
- v0.6 尚无经研究者完成体系准备和参数化的真实兽医蛋白—配体 MD 案例；
  合成两原子 smoke 不能填补该空白。科研级 NVT/NPT、production、多重复、
  收敛/不确定性与轨迹分析仍不在当前实现范围。
- NCBI Gene/GenBank 和 DAVID 的在线路径仍需由研究者提供合规联系邮箱或已
  注册机构邮箱后做真实服务验收；当前离线门禁与导出已验证。
- OMIM 与 DrugBank 的真实在线路径仍需研究者提供合法 API 凭证，DrugBank
  还需确认机构许可；GeneCards、MalaCards 与 SwissTargetPrediction 的内容
  真实性依赖用户对导入材料来源的声明，系统当前不独立向上游验证。
- 尚未录制演示视频，也未获准进行公开部署。
- 孙奇本人从零启动、完整垂直案例讲解、核心流程小修改和排错验收已延期到 GitHub 工程状态稳定后，不作为 v0.7 当前工程阶段的阻塞项。

## 多数据库批次检索与下载（已完成）

### 本阶段交付契约

- [x] 数据库证据页保留单库模式，并新增一次选择 2—12 个数据库的批量模式；每个来源使用独立输入、凭证与许可门禁。
- [x] 提交前统一校验所有来源和最多 50 次实际操作上限；按用户选择顺序串行处理，一个来源失败不阻断其他来源。
- [x] 每次提交生成独立 `batch_id`，结果统计直接使用本次执行对象，不依赖可能截断的历史记录。
- [x] 批次清单冻结来源、查询 ID、状态与 SHA-256；刷新或会话重建后可恢复已校验批次，并按批次隔离 STRING/DAVID 网络结果。
- [x] 标准化 ZIP 包含批次清单、结果 JSON、汇总 CSV、分库 CSV 与校验和，不包含原始响应。
- [x] 原始审计 ZIP 与标准化 ZIP 分离，只有用户再次确认所有来源许可和仅限内部审计后才生成；单条原始归档下载采用同等门禁。
- [x] 批次恢复与导出拒绝路径穿越、重复条目、符号链接、Windows junction、校验和不一致、CSV 公式注入和敏感字段泄漏。

### 验证证据

- 批次归档与连接器归档专项：`16 passed`；数据库 UI 支持：`12 passed`；数据库页面：`7 passed`。
- 数据库专项回归：`120 passed`；最终全量回归：`422 passed, 3 skipped`。
- Python 编译检查、`pip check` 和 Git 差异检查通过。
- 浏览器实测 OMIM + DrugBank 双库批量离线流程：两项均独立配置并归档，页面显示 `2/2`，标准化 ZIP 直接可下载，原始审计 ZIP 仅在二次许可确认后出现。
- 本次没有使用 OMIM 或 DrugBank 的真实密钥，浏览器验收证明的是批量编排、离线门禁、归档与下载链，不代表商业数据库在线账号已验收。
