# 执行检查点

## 当前阶段

VetResearch Workbench v0.2 增量阶段：首个协同研究垂直闭环已完成本地实现与验收。

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
- [x] 以 PMID 去重多轮检索结果，并继承 LetPub 中科院/JCR 双分区核查
- [x] 支持 RIS、EndNote、RefWorks 题录导入及 DOI/标题年份去重
- [x] 非 PubMed 来源使用通用来源 ID，不伪造 PMID
- [x] 形成物种、模型、样本量、干预、剂量、时间、对照和指标的实验条件矩阵
- [x] 只在同一主题存在显式相反方向时标记冲突，并逐项展示证据空白
- [x] 校验并分析 FICI CSV，保留原始行、错误、FIC、FICI 和阈值分类
- [x] 校验并分析生长曲线 CSV，计算时间点均值、标准差、重复数和梯形 AUC
- [x] 生成带 PMID、DOI 或 CSV 文件名、SHA-256、原始行号与计算过程的结论、风险和下一步实验建议
- [x] 保存任务事件、工具调用、失败、重试关系和人工复核到 `.workbench/runs`
- [x] 提供历史运行恢复和 Markdown/JSON/完整快照下载
- [x] 合成演示数据始终标记为不可作为科研证据

### 新阶段验证证据

- 全量自动测试：`69 passed`；其中 v0.1 原有 21 项保持通过。
- 真实三轮 NCBI 检索：
  - `quercetin Streptococcus agalactiae`
  - `amoxicillin Streptococcus agalactiae`
  - `quercetin amoxicillin Streptococcus agalactiae (synergy OR interaction OR combination)`
- 三轮检索去重后返回 5 个唯一 PMID：`42250334`、`34828017`、`38540051`、`37663139`、`40505823`。
- 浏览器实际验收五步页面：问题与假设、文献证据、实验数据、决策报告、运行记录。
- 浏览器实际运行合成 RIS 导入、FICI、生长曲线、报告、人工复核和历史恢复。
- FICI 演示数据保留 3 个有效行；生长曲线演示计算 `combination AUC=0.93`、`control AUC=2.92`。
- 本地 JSON 快照成功恢复已完成任务，恢复后仍保留事件、工具调用和人工复核决定。
- 来源模型、无效 CSV 隔离、时区校验、快照迁移及报告内容哈希均有回归测试。
- Windows 下快照原子替换的短暂文件锁已增加有限重试，并完成浏览器与磁盘落盘验收。

## 阻塞

- 本机未安装 Docker：Dockerfile 已写，镜像构建未验证。
- 尚未录制演示视频，也未获准进行公开部署。
- 仍需孙奇亲自完成一次从零启动、完整垂直案例讲解和一次核心流程小修改。
