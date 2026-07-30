# 数据库连接与证据网络

VetResearch Workbench v0.4 提供小规模、用户主动触发、可审计的公开数据库
连接层。它不镜像数据库，也不把数据库关联或模型预测写成实验事实。

## 支持的数据源

| 数据源 | 主要用途 | 稳定标识 |
| --- | --- | --- |
| PubChem PUG-REST | 化合物身份、结构属性与 CID 解析 | CID、InChIKey |
| UniProt REST | 蛋白、基因、物种、序列版本与交叉引用 | UniProt accession、TaxID |
| NCBI Gene / GenBank | Gene 记录、核酸记录与 accession.version | GeneID、GenBank accession.version、TaxID |
| RCSB PDB | 实验结构元数据、实体、链与结构文件 | PDB ID、entity ID、label/auth chain ID |
| STRING | 物种限定的功能关联与分通道分数 | STRING ID、映射后的 UniProt/Gene ID、TaxID |
| DAVID | 目标集相对明确背景集的功能富集 | DAVID Gene ID、输入 ID、TaxID |

NCBI 请求必须带工具名和联系邮箱；DAVID 正式富集需要已注册的机构邮箱。
凭据只从环境变量或 Streamlit secrets 读取，不写入请求留痕、日志或下载文件。

## 每次请求保存什么

每个连接器都生成独立 provenance 记录，至少包括：

- 数据源、接口、运行时版本或 release date、UTC 访问时间；
- 原始查询、规范化查询、脱敏 URL、请求体 SHA-256；
- HTTP 状态、响应类型、ETag/Last-Modified、原始响应 SHA-256；
- 主标识、次标识、TaxID、记录状态、替代或合并标识；
- 解析器版本、规范化记录 schema 与 SHA-256；
- 许可证、引用要求和上游归属；
- 输入映射、候选项、映射状态、歧义与警告。

原始响应不会被新请求覆盖。跨物种名称冲突、一个名称命中多个 CID、失效
accession 或多条结构候选都必须显示给研究者确认，不能静默选择第一条。

## 证据网络分层

证据边按来源而不是按“看起来可信”分层：

- `experimental`：实验数据通道；
- `curated_database`：人工整理数据库；
- `text_mined`：文本挖掘关联；
- `computational_prediction`：邻域、融合、共现、模型或网络预测。

STRING 的 `escore`、`dscore`、`tscore` 以及预测通道分别保存；`score`
只用于排序，不改变证据类型。DAVID 富集必须保存目标集、背景集、物种、
映射比例、原始 P 值和上游报告的多重校正后 P 值。若上游没有返回 BH
校正值，系统明确标记 `not_reported`；因为 DAVID chart 可能已经按阈值筛选，
不能在不完整检验家族上自行补算。富集是统计关联，不是靶点验证。

证据网络为 DAVID 命中基因建立到条目的注释关系，并使用 STRING 返回的
标识映射连接蛋白层。只有两端 TaxID 相同、输入标识完全相同才连接；不猜测
跨数据库身份。不同 TaxID 或单层混合多个 TaxID 会被拒绝。

## 数据外发与离线模式

STRING 和 DAVID 请求会把标识列表发送给外部服务。界面在调用前要求用户
主动确认；未公开或敏感的基因列表应选择离线模式。离线模式只生成带 TaxID、
ID 类型、目标集、背景集、参数和哈希的待提交文件，不执行网络请求。

零结果表示“当前查询和数据库覆盖下没有返回记录”，不能解释成不存在真实
生物学关系。兽医物种覆盖不足、标识映射失败和外部服务不可用会作为单独风险
显示。
