# 分子动力学：v0.6 technical smoke 边界

分子动力学（MD）是在指定模型下积分原子运动方程。完整 MD 可用于研究构象波动
与接触变化，但 VetEvidence v0.6 **尚未实现科研级 NVT/NPT、生产模拟、收敛分析
或结合自由能计算**。当前真实执行范围只有一个极短的 OpenMM technical smoke，
用于核验参数化输入能否在本机完成最小化和短积分。

## 当前真实能力

technical smoke 会：

- 保存并复核已选链、无 altloc 的单模型受体 PDB，以及单记录 V2000 配体
  SDF 的字节、来源身份和 SHA-256；
- 要求研究者提供已审核的 OpenMM `System XML`、匹配 topology PDB、实际
  力场/参数文件、参数化工具与版本、命令参数及显式 source→topology 原子映射；
- 拒绝仅凭 PDBQT 或 SMILES 直接进入真实执行；
- 在独立 worker 中设置积分器和可设置随机种子的 System 随机源，执行最小化与
  30 步短积分；
- 分块积分并检查取消请求，按块原子保存 checkpoint 和 portable state；
  页面持有子进程句柄时可在协作取消超时后终止，CLI worker 有 300 秒硬截止；
- 复核 System 粒子、force、constraint 及 topology 原子数；周期 System 或
  带 `CRYST1` 的 topology 在 v0.6 直接拒绝；
- 设置 XML、粒子、force、constraint、步数、运行时间和输出大小上限。

协议固定为 OpenMM 8.5.2、单重复、单 seed 和 30 个积分步；版本、重复数或
分析边界不能通过界面改写成科研级方案。执行层可请求 CPU、CUDA、HIP 或
OpenCL，并记录实际选中的平台、设备、精度、平台属性和随机源；驱动只在
后端报告时记录。任务
声明 `gpu_required=true` 但没有可用 GPU 平台时直接失败，不静默降级到 CPU。

它不会把这 30 步称作 NVT 平衡、NPT 平衡或 production，也不能用于解释蛋白—
配体稳定性、真实结合、药效、抗菌效果或协同作用。

## 安装与本机验收

CPU/OpenCL 通用环境和 CUDA 12 环境分别使用项目固定的可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[molecular-dynamics]"
# 或
.\.venv\Scripts\python.exe -m pip install -e ".[molecular-dynamics-cuda12]"
.\.venv\Scripts\python.exe -m openmm.testInstallation
```

Windows 下直接运行官方命令可能在项目预加载 CUDA wheel DLL 前只列出
CPU/OpenCL；CUDA 的产品级验收必须再运行
`scripts/run_md_smoke.py --platform CUDA`，并核对 `actual_platform`、
`DeviceName`、`DeviceIndex` 与精度。

本机在 VetEvidence Windows CUDA 依赖预加载后运行 OpenMM 8.5.2 官方
`testInstallation`，Reference、CPU、CUDA 和 OpenCL 均通过 force 差异容差。
正式 `scripts/run_md_smoke.py` 使用完全公开的合成两原子 N+C 数值 fixture，
通过与产品相同的存储、准备输入、worker、QC 和产物复核链分别强制 CPU 与
CUDA 执行：两次均完成 30 steps，各记录 6 个真实温度样本和 6 个真实势能
样本并通过 QC。CUDA 实际设备为
`NVIDIA GeForce RTX 5070 Laptop GPU`，`DeviceIndex=0`、`mixed` 精度；
CPU 执行审计的实际平台为 `CPU`。

这个 fixture 不是蛋白—配体体系，验收只证明当前机器的 OpenMM 安装和最小
执行链可用；其他机器仍须单独运行官方安装测试，GPU 平台可见也不代表任意
科研体系都能运行或结果可信。

## 输入与参数化责任

首版真实执行只接受：

- 已经裁剪为人工选择链子集、无 altloc 的单模型原始受体 PDB；
- 恰好一个 V2000 记录的原始配体 SDF；
- 与原始输入存在逐原子 canonical 映射的非周期 topology PDB；受体映射必须
  同时匹配链、残基名、残基编号、插入码、原子名、altloc 和元素，配体映射
  必须匹配源原子索引和元素；
- 与 topology 粒子数一致的 OpenMM `System XML`；
- 实际使用的力场/小分子参数文件。

系统不自动猜测蛋白缺失残基、质子化、互变异构、立体化学、形式电荷、金属、
共价连接或小分子参数。存在未解决金属、共价连接或非标准残基时阻断。自动
OpenFF 参数化栈缺失时只报告缺口，不伪造 System 或轨迹。

System XML 禁止 DOCTYPE 和外部实体。所有准备产物均绑定 manifest、原始来源
哈希、参数化工具版本、命令参数、canonical 原子映射证据、用户提交的支持
证据哈希和实际文件哈希。上传的摘要会在 OpenMM 反序列化后重新计算；不一致
即失败。

## 当前真正产出的内容

成功 smoke 产出并哈希：

- `manifest.json`
- `system.xml` 与 `topology.pdb`
- `trajectory.dcd`
- `state.csv`
- `checkpoint.chk` 与 `portable-state.xml`
- `representative.pdb`
- `analysis.json`
- 可编辑的 `view_md.pml`

v0.6 实际分析字段只有：

- 温度时间序列；
- 势能时间序列。

温度或势能缺失、为空、含 NaN/无穷值或超过宽松的数值安全边界时，technical
smoke 失败。RMSD、RMSF、回转半径、配体 RMSD、接触、氢键、压力、密度等字段
只是后续版本的 reserved schema，当前不会生成、展示或暗示已经计算。当前也不
生成分析 CSV、科研图、MM/GBSA、MM/PBSA、FEP、ABFE 或“结合自由能”。

状态只使用：

- `qc_failed`
- `technical_smoke_passed`

`technical_smoke_passed` 只表示最小数值执行和规定产物通过，不是科研结论。

## 后台任务、取消与恢复

Streamlit 只能提交和轮询独立 worker，不能在网页请求内同步执行 OpenMM。
worker 以有限步块运行，每块检查取消状态并发布绑定 manifest、System、
topology、replica、seed、step、OpenMM 版本及实际平台、DeviceIndex、
DeviceName、精度和基础硬件组合指纹的 checkpoint。恢复前会在新 Context
创建后重新核验这些绑定和文件哈希。

每次尝试使用独立 `attempt-XXXX` 目录；任务 JSON 使用跨进程锁与 revision
compare-and-swap，启动恢复可把不存在 worker PID 的遗留任务转为明确失败或
取消。成功结果重新加载时会复核结果清单及全部产物哈希。

OpenMM 二进制 checkpoint 通常只适用于兼容的 OpenMM、平台和硬件环境；保存
portable state 不代表跨平台恢复一定产生逐位相同的结果。GPU 计算也不保证逐位
确定性，审计记录可获得的实际平台属性、设备、精度、驱动和随机种子设置。

## 后续路线，不属于 v0.6

探索性重复、科研级 NVT/NPT、长时间 production、RMSD/RMSF/接触分析、对照、
收敛和不确定性评估均为后续工作。实现这些能力前必须增加力场与体系准备验证、
阶段化协议、真实多重复测试、轨迹分析方法定义和独立科研复核。
