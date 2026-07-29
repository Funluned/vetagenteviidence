# VetResearch Workbench v0.2

VetResearch Workbench 基于 VetEvidence AI v0.1，把科研问题、可核查文献、实验条件与 CSV 数据串成一个可审计的科研决策闭环。当前首个垂直场景聚焦：

> 候选药物与抗生素对目标病原菌是否存在值得进一步验证的协同作用？

系统仅用于科研证据整理与实验设计支持，不构成医疗、兽医诊断、处方或临床建议。

## 当前已实现

### VetResearch Workbench v0.2

- 把科研问题拆成 2—4 条可检验假设，并允许人工修改；
- 自动生成并执行最多 3 轮可见 PubMed 检索式，保留各轮相关性顺序，以轮询方式公平合并并按 PMID 去重；
- 导入 RIS、EndNote、RefWorks 题录文件，按 DOI 或标题与年份去重；
- 把 PubMed 与用户导入文献整理为同一实验条件矩阵，缺失字段保持为空；
- 比较物种、模型、样本量、干预、剂量、时间、对照和指标，显示一致性、显式冲突与证据空白；
- 分析 FICI 与生长曲线 CSV，逐行保留原始值、校验错误和来源行号；
- 生成带文献引用，以及 CSV 文件名、SHA-256、原始行号与计算过程的 Markdown/JSON 决策报告；
- 要求人工选择“通过、要求修改、拒绝”后再结束任务；
- 把任务事件、工具调用、失败、重试关系和人工复核保存到本地 `.workbench/runs/*.json`，可凭完整运行 ID 恢复；
- 通过五步 Streamlit 界面完成完整闭环，无需 LLM API Key。

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

## 五步工作流

1. `问题与假设`：填写研究对象、候选干预、联合药物和预设指标，检查并修改透明规则生成的假设；
2. `文献证据`：执行最多 3 轮 PubMed 检索，或上传 RIS、EndNote、RefWorks 导出文件；
3. `实验数据`：核查实验条件矩阵、冲突和空白，再上传 FICI 或生长曲线 CSV；
4. `决策报告`：生成带来源、风险和下一步的报告，完成人工复核；
5. `运行记录`：查看事件、工具调用和失败记录，下载快照或凭完整运行 ID 恢复。

页面提供合成 RIS、FICI 和生长曲线演示数据。这些文件只用于验证工作流，页面与报告会明确标记，不能作为科研事实。

## 支持的输入

### 文献题录

- RIS：通常使用 `.ris`；
- EndNote：通常使用 `.enw` 或文本导出；
- RefWorks：使用其带标签的文本导出。

系统不会绕过知网等平台权限自动抓取，也不会把用户导入记录伪装成 PubMed 文献。非 PubMed 来源没有 PMID 时保持为空。

### FICI CSV

必须包含以下四列，其他列可作为原始记录一并保留：

```text
drug_a_mic_alone,drug_a_mic_combo,drug_b_mic_alone,drug_b_mic_combo
```

计算公式为：

```text
FICI = drug_a_mic_combo / drug_a_mic_alone
     + drug_b_mic_combo / drug_b_mic_alone
```

当前透明分类阈值为：`≤ 0.5` 协同、`≤ 1` 相加、`≤ 4` 无关、`> 4` 拮抗。该结果是描述性分类，不能替代独立重复与 time-kill 等正交验证。

### 生长曲线 CSV

必须包含：

```text
time,group,value
```

系统按组和时间点汇总重复值的均值、标准差与样本数，并用梯形法计算各组 AUC；不自动进行显著性检验或模型比较。可直接下载 `data/templates/` 中的 CSV 模板。

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

当前回归结果为 `72 passed`。继承自 v0.1 的定向评测仍为 `30/30`，这是受控工程检查，不代表通用模型准确率。

真实验收使用默认协同问题执行 3 轮 NCBI PubMed 检索，三轮分别返回 7、8、0 条；公平合并并去重后保留 8 个唯一 PMID，其中前两轮各贡献 4 条。PubMed 是实时外部数据源，未来复跑的数量和排序可能变化。

运行包含真实 PubMed 查询的 v0.1 定向评测：

```powershell
.\.venv\Scripts\python.exe scripts\run_evaluation.py
```

评测结果写入 `data/eval/latest_results.json` 和 `docs/EVALUATION.md`。

## 数据与审计

- PubMed 题录和摘要来自 [NCBI Entrez Programming Utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/)；
- 期刊分区参考 [LetPub](https://www.letpub.com.cn/index.php?page=journalapp)，正式评价或投稿前仍需复核；
- 用户导入题录和实验 CSV 只在本机处理；
- 每次运行保存为 `.workbench/runs/<run_id>.json`，该目录已被 Git 忽略；
- 报告中的文献结论关联 PMID、DOI、来源 URL 或原文片段；实验分析关联 CSV 文件名、SHA-256、原始行号和可复算过程。

## 项目文档

- [产品需求](docs/PRD.md)
- [架构说明](docs/ARCHITECTURE.md)
- [期刊分区数据说明](docs/JOURNAL_RANKINGS.md)
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

## 当前边界

- 首个垂直场景只覆盖候选药物与抗生素协同作用，不代表支持所有实验类型；
- 当前只解析 PubMed 摘要和 RIS、EndNote、RefWorks 题录导出，不读取未授权全文，也不支持扫描 PDF/OCR；
- 用户导入记录的正确性仍需人工核查，系统不会把缺失信息补造为事实；
- FICI 和生长曲线只做透明、可追溯的描述性分析；
- 合成演示数据只用于验证计算和页面流程，不得进入科研结论；
- 规则提取无法覆盖所有摘要表达方式，冲突检测也只识别显式方向差异；
- 系统不训练或微调模型，不使用多 Agent 框架、向量数据库或复杂权限系统；
- 当前仅面向可信的单用户本机运行；完整运行 ID 在本机恢复流程中相当于访问凭证，不适合直接用于共享部署；
- 决策报告必须人工复核，不能替代全文核查、原始数据审计或验证性实验。
