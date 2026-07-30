# 数据库连接、授权导入与证据网络

VetResearch Workbench 提供 12 个界面入口，但它们不是 12 个“公开 API”。
系统只在用户主动提交后调用允许自动访问的官方接口；凭证、许可、人工文件
导入和计算预测分别显示。数据库关联和模型预测都不能写成实验事实。

## 数据源与接入边界

| 界面入口 | 接入方式 | 证据等级 | 物种范围与稳定标识 |
| --- | --- | --- | --- |
| PubChem | 公开官方 API | 数据库整理 | CID、InChIKey |
| UniProt | 公开官方 API | 数据库整理 | UniProt accession、NCBI TaxID |
| NCBI Gene | 公开官方 API | 数据库整理 | GeneID、NCBI TaxID |
| GenBank | 公开官方 API | 数据库整理 | accession.version、NCBI TaxID |
| RCSB PDB | 公开官方 API | 数据库整理 | PDB ID、entity/chain ID |
| STRING | 用户确认后调用官方 API | 分通道记录实验、整理、文本挖掘和预测 | STRING ID、映射 ID、NCBI TaxID |
| DAVID | 用户确认后调用官方 API | 统计富集结果 | 输入 ID、DAVID Gene ID、NCBI TaxID |
| OMIM | 凭证门控官方 API | 数据库整理 | MIM number；仅人类 TaxID 9606 |
| DrugBank | 许可与凭证双门控官方 API | 数据库整理 | DrugBank ID、BioEntity ID、可用时 UniProt ID/TaxID |
| GeneCards | 用户声明合法取得的授权文件导入 | 数据库整理（用户声明来源） | GeneCards/HGNC/GeneID/UniProt/OMIM；仅人类 9606 |
| MalaCards | 用户声明合法取得的授权文件导入 | 数据库整理（用户声明来源） | MCID、OMIM/Orphanet/UMLS 等；仅人类 9606 |
| SwissTargetPrediction | 用户手工生成的结果文件导入 | `computational_prediction` | 原查询 SMILES；仅人 9606、小鼠 10090、大鼠 10116 |

NCBI 请求必须带工具名和联系邮箱；DAVID 正式富集需要已注册邮箱。OMIM
需要 `OMIM_API_KEY`。DrugBank 需要 `DRUGBANK_API_KEY`，且用户必须确认
当前订阅许可允许本次内部研究查询。凭据只从环境变量或 Streamlit secrets
读取，不写入归档、日志或下载文件。

GeneCards Suite 条款不允许未获授权的自动访问或网页抓取，因此系统不访问
其网页接口；GeneCards 和 MalaCards 只读取用户本地上传的授权导出文件。
SwissTargetPrediction 同样不使用爬虫、自动表单或批量提取，只接受用户在
官网手工生成的结果。导入许可与数据再利用范围仍由用户和所在机构负责。

## 获取方式与证据等级

两个维度必须正交记录：

- `online_api`、`manual_import`、`offline_request` 描述数据怎样取得；
- `curated_database`、`computational_prediction` 描述记录属于什么证据层。

例如，GeneCards 文件是 `manual_import + curated_database`，但界面必须注明
“用户声明的授权导出”，不能声称系统验证了文件出处；SwissTargetPrediction
文件是 `manual_import + computational_prediction`，不能作为已验证靶点。

## 每次获取或导入保存什么

在线响应与人工导入都生成独立 provenance，至少保存：

- 数据源、接口或官方页面、UTC 获取/导入时间、来源版本或导出日期（如有）；
- 规范化且脱敏的查询背景、请求或导入方式、请求/文件 SHA-256；
- 在线请求的 HTTP 状态、响应类型、ETag/Last-Modified；人工导入使用
  `method=IMPORT` 且 `http_status=None`；
- 主标识、次标识、TaxID、映射候选、歧义和警告；
- 解析器版本、规范化记录 schema、记录 SHA-256、许可与引用 URL；
- 对受限文件的用户确认类型。哈希只能证明归档内容未变化，不能证明来源。

原始响应或原文件不会被新任务覆盖。名称命中多个 ID、跨物种冲突、失效
accession、分页截断或详情补全失败都必须提示，不能静默选择第一条或假装
结果完整。

## 文件导入安全

GeneCards、MalaCards 和 SwissTargetPrediction 只接受不超过 10 MiB 的
UTF-8 CSV/TSV 或 XLSX。解析器限制 50,000 行、256 列，并拒绝：

- 空列名、重复或歧义表头、缺少必要列和非法数值；
- XLSX 公式、错误单元格、宏、嵌入对象、外部工作簿链接；
- 加密成员、路径穿越、异常成员数、异常展开大小和压缩比；
- 未启用安全 XML 解析器的运行环境。

原始受限文件保存在 Git 忽略的 `.workbench/connectors/`。当前产品面向可信
单用户本机；共享部署前必须另行实现对象授权、保留期限与删除策略。

## STRING 与 DAVID 证据网络

STRING 的实验、人工整理、文本挖掘和预测通道分别保存；`combined_score`
只用于排序，不改变证据类型。DAVID 富集保存目标集、明确背景集、物种、
映射比例、原始 P 值和上游报告的多重校正 P 值。若上游未返回 BH 值，系统
标记 `not_reported`，不在不完整检验家族上自行补算。

当前自动证据网络只消费 STRING 与 DAVID 的标准化结果。新增的 OMIM、
DrugBank、GeneCards、MalaCards 和 SwissTargetPrediction 先作为可追溯的
数据库记录或预测记录展示；在完成跨库 ID 映射、物种一致性检查和证据边
定义前，不会被静默并入网络排名。

## 数据外发与离线模式

STRING、DAVID、OMIM 和 DrugBank 可能把查询标识发送给外部服务。缺少联系
信息、凭证、许可确认或外发确认时，系统只生成带 TaxID、ID 类型、参数和
哈希的离线请求，不执行网络请求。

零结果只表示“当前查询与数据库覆盖下没有返回记录”，不能解释成不存在真实
生物学关系。兽医物种覆盖不足、标识映射失败、预测记录缺少稳定 ID、接口
分页和外部服务不可用都作为单独风险显示。
