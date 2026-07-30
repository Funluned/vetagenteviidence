# 分子对接科研链

VetResearch Workbench v0.5 在现有 AutoDock Vina 执行审计上增加受体质量检查、
批量配体、多随机种子、相互作用产物和可编辑可视化。所有结果仍属于
`computational_prediction`。

## 输入与人工门禁

- 受体同时保留原始 PDB/mmCIF 与准备后的 PDBQT；记录 PDB ID、NCBI TaxID、
  研究靶标、物种、来源 URL/修订、可选 UniProt ID 和两份文件哈希。当前
  PDB 文本入口不声称已自动获取 entity、label/auth chain mapping、实验方法
  或分辨率；需要这些字段时由研究者另行核查 RCSB 元数据。
- 研究者必须确认所选链、配体结合口袋、网格和需要保留的辅因子、金属与水。
- 配体同时保留原始 SDF/MOL2/SMILES 和准备后的 PDBQT；确认键级、立体化学、
  质子化、互变异构体和形式电荷。
- 系统自动检查坐标、聚合物、模型/链，并枚举 altloc、水、异源物和金属；
  当前不会自动识别缺失关键残基、label/auth 链映射、金属配位或共价配体。
  这些项目由研究者外部复核；存在未决问题时不得勾选人工审批或进入对接。

Open Babel 是基础准备路径；Meeko 是可选科研准备路径。两者都不能替代研究者
对具体体系的化学审查。

## 批量与稳定性

同一受体可对多个配体运行多个明确随机种子。每次 Vina 调用仍使用参数数组、
隔离目录、`shell=False`、超时和可执行文件哈希核验。稳定性摘要报告：

- 每个配体、每个 seed、每个 pose 的 Vina 预测评分，以及 Vina 在同一次
  运行内给出的相对最佳模式 RMSD 上下界；
- 最优分、均值、中位数、标准差、极差与成功/失败 seed 数；
- 每个失败或跳过的 seed 及其原因。

工作流没有跨 seed 的原子映射和结构对齐，因此不报告跨 seed 构象 RMSD，
也不声称不同 seed 的最佳 pose 已形成一致构象簇。稳定性摘要只描述评分的
离散程度。

Vina 输出统一标为“Vina 预测评分（kcal/mol）”。它不是实验结合能，也不是
严格的结合自由能；不同评分函数的数值不能直接混比。

## 可视化与相互作用

- 浏览器使用固定版本、本地资源的 3Dmol.js；蛋白、配体和口袋残基可分别
  显示，结构文本只通过组件 data 传入，不拼接进 JavaScript 源码。
- 浏览器可直接导出当前画面的 PNG。
- 系统始终生成可编辑 `view.pml`、原始结构、镜头和配色参数。
- 配置 Open-Source PyMOL 后，可由用户逐次明确确认无头渲染或点击打开；
  只执行本任务生成且哈希已绑定的 PML/PSE，命令采用参数数组和
  `shell=False`。远程部署不能在浏览器用户电脑上启动服务端 PyMOL。
- 配置 PLIP 后，可在用户逐次确认后生成 XML/TXT；首版不调用 PLIP 自带
  图片/PSE 路径，对接图由已验证的 PyMOL 和本地 3Dmol.js 生成。PLIP 作为
  外部程序适配，不把其源码或二进制捆绑进核心包。

PSE 是便利的二进制会话，不是唯一复现依据；正式交付必须同时包含 PML、
结构文件、参数与哈希。

PDBQT 转回 PDB 会丢失或猜测部分键级、形式电荷和扭转树信息，因此由该
复合物运行的 PLIP 结果标为“启发式计算相互作用”，不能替代使用原始化学
结构和实验数据的人工复核。

## 身份与产物绑定

- 受体身份把 PDB ID、NCBI TaxID、物种、来源 URL/修订、RCSB 原始记录
  SHA-256 和可选 UniProt ID 与原始及准备后结构哈希一起冻结；
- 受体批准把模型、链、altloc、水、辅因子、金属、其他异源物、准备工具、
  口袋依据和搜索框一起冻结，存在未决质量警告时不能运行；
- 配体使用 PubChem CID + InChIKey，或使用明确的用户命名空间、来源 URL
  与原始文件哈希，不能只填一个可任意改写的名称；
- 每次成功结果同时校验任务清单、seed、Vina 日志、输出 PDBQT、解析评分和
  执行审计；可视化只从这组已验证产物派生，界面不能另填一个评分；
- 复合物只保留人工批准的受体链与异源物，新配体使用唯一链和残基号；
  3Dmol.js、PyMOL 与 PLIP 均使用这组精确选择器。

## 任务包

每个可视化任务 ZIP 的核心文件及可用时加入的可选文件包括：

```text
receptor_original
receptor_prepared.pdbqt
receptor_selected.pdb
receptor_approval.json
ligand_original
ligand_prepared.pdbqt
ligand_identity.json
vina_manifest.json
vina_execution_audit.json
vina_bound.log
vina_out.pdbqt
selected_pose.pdbqt
selected_pose.csv
complex.pdb
view.pml
interaction.png
plip_report.xml
plip_report.txt
interaction.pse
visualization_manifest.json
SHA256SUMS.txt
```

未配置或未通过核验的可选程序对应文件不会伪造；清单会明确标为
`unavailable`。PLIP PNG/PSE 在首版固定为不可用，使用独立 PyMOL/3Dmol
产物。

PNG 必须能被图像解码器完整读取；只有文件头但内容损坏的“假 PNG”会被
拒绝。PSE 只有在同一已核验 PyMOL 能重新打开时才标为已验证，否则明确标为
“工具生成但未验证”。

## 本机工具

仓库内只固定并分发 3Dmol.js 2.5.5 的 ES 模块资产及许可证。AutoDock Vina、
Open Babel、Open-Source PyMOL 和 PLIP 是独立的本机程序：界面从显式环境
变量或受控路径发现它们，显示版本和可执行文件 SHA-256，并在每次执行前
复核。PLIP 另要求提供并哈希审计 `BABEL_LIBDIR`、`BABEL_DATADIR`，其子进程
使用派生的受控 `PATH`，不继承任意 PATH。项目不会下载或执行来源不明的
“破解版”。

页面同步执行最多 24 次 Vina 尝试、384 个
`尝试数 × exhaustiveness` 工作单位和 100 MB 配体总量；更大的计算应拆分
批次，不能用一次页面请求长期占用工作台。

本机验收使用 AutoDock Vina 官方 1IEP 基础教程公开输入；这只是技术完整性
案例，不是本项目兽医研究问题的生物学结论。官方教程同时提醒，小分子应优先
从保留键级的 SDF 准备，且 Vina 评分不能冒充实验结合自由能。
