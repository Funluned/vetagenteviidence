# VetEvidence AI

面向兽医与生物医药科研人员的可追溯证据整理工具。当前已实现：

- 使用 NCBI E-utilities 检索真实 PubMed 文献；
- 展示标题、作者、期刊、年份、PMID、DOI 和摘要；
- 按 ISSN 动态查询 LetPub，同时展示中科院 2025 年 3 月升级版与 WOS JIF 分区，并保留分类、排名和来源；
- 从摘要提取病原体、模型、样本量、药物、剂量、途径、结果和机制等结构化字段；
- 缺失字段保持为空，不让系统补造；
- 生成带 PMID、DOI 和来源原句的证据回答；
- 导出 Markdown 科研证据报告和 CSV 证据表；
- 对网络失败、空结果和解析异常给出明确提示，并重试临时 NCBI 故障；
- 提供 30 条定向评测及分类结果页面；
- 通过 Streamlit 提供完整的最小交互闭环。

> 仅用于科研证据整理，不构成医疗或兽医诊断建议。

## Windows 快速启动

项目要求 Python 3.11 或更高版本。在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

终端显示地址后，在浏览器打开 `http://localhost:8501`。

输入检索词并点击“检索 PubMed”后，可切换：

- `文献列表`：核查原始题录和摘要；
- `证据表`：查看结构化字段和两套期刊分区；
- `证据回答`：查看结论、引用和局限；
- `评测`：查看定向检查样本、分类通过率与适用边界；
- `导出`：下载 Markdown 或 CSV。

如需填写 NCBI 联系邮箱或 API Key，可在启动页面前为当前终端设置环境变量：

```powershell
$env:NCBI_EMAIL = "your_email@example.com"
$env:NCBI_API_KEY = "your_api_key"
```

`.env.example` 用于说明需要的变量名；不要提交真实 `.env` 或密钥。

项目默认按 PubMed 返回的 ISSN 动态查询 LetPub，并把结果缓存 7 天；LetPub
不可用时才使用本地 CSV 回退。可通过 `LETPUB_LOOKUP_ENABLED=false` 关闭动态查询，
或复制 [`data/journal_rankings.template.csv`](data/journal_rankings.template.csv)
并用 `JOURNAL_RANKINGS_CSV` 指向机构授权数据。未匹配期刊明确显示“未收录”，
系统不会猜测。字段说明、版本口径和授权边界见
[期刊分区数据说明](docs/JOURNAL_RANKINGS.md)。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

运行包含真实 PubMed 查询的定向评测：

```powershell
.\.venv\Scripts\python.exe scripts\run_evaluation.py
```

评测结果写入 `data/eval/latest_results.json` 和 `docs/EVALUATION.md`。

## 数据来源

检索与文献元数据来自 [NCBI Entrez Programming Utilities](https://www.ncbi.nlm.nih.gov/books/NBK25501/)；期刊分区参考 [LetPub](https://www.letpub.com.cn/index.php?page=journalapp)。页面中的 PMID、DOI 和分区来源链接均可用于回查。

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

- 当前阶段只使用 PubMed 元数据和摘要，不代表已读取论文全文。
- 分区来自 LetPub 公开页面而非中科院或 Clarivate 官方 API；LetPub 标注其 WOS 数据为众包数据，正式评价或投稿前仍需复核。
- LetPub 页面结构或访问策略变化时，动态解析可能失效；系统会改用缓存或本地演示回退表。
- 摘要未报告的信息不会由系统补造。
- 当前使用透明规则 `rules_v1` 提取和回答，不依赖 LLM API Key。
- 规则提取适合演示与可解释验证，但无法替代人工全文核查。
- 当前 30 条评测主要围绕一个示范查询和受控边界场景，不代表通用准确率。
- 尚未接入论文全文、人工语义复核或 LLM Provider。
