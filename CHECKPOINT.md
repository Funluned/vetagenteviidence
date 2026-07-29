# 执行检查点

## 当前阶段

32—48 小时交付阶段：代码、文档和本地回归已完成。

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
- 目标论文：PMID `42250334`，DOI `10.1016/j.rvsc.2026.106289`。
- 目标论文提取：小鼠、样本量 25、Quercetin、`25, 50, 100 mg/kg`、腹腔注射、`24 h`、NF-κB、NLRP3、铁死亡及来源原句。
- 页面验收：文献列表、证据表、证据回答、评测、导出五个标签页均已实际检查。
- 异常验收：空白检索词会显示“检索词不能为空”，不会抛出未处理异常。
- 本地服务：`http://localhost:8501`。

## 阻塞

- 本机未安装 Docker：Dockerfile 已写，镜像构建未验证。
- Git 未配置用户名、邮箱和远端：当前代码没有正式提交或推送。
- 尚未录制演示视频，也未获准进行公开部署。
- 仍需孙奇亲自完成从零启动、讲解和一次核心流程小修改。
