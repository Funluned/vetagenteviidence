# 期刊分区数据说明

## 当前口径

- 中科院分区读取 LetPub 公开页中的 **2025 年 3 月升级版**，不把 LetPub 的其他第三方榜单称为中科院分区。
- JCR 分区读取 LetPub 英文页 `Quartiles By JIF` 表中的 WOS 分类、收录集、Q1—Q4 和名次；不使用同页的 JCI 分区。
- 期刊属于多个分类时，系统逐项显示，不合并成单个“最好分区”。
- 两套分区是不同评价体系，系统并列展示，不做换算。

参考：

- [LetPub 中文期刊查询入口](https://www.letpub.com.cn/index.php?page=journalapp)
- [Research in Veterinary Science：LetPub 中科院分区页](https://www.letpub.com.cn/index.php?journalid=7153&page=journalapp&view=detail)
- [Research in Veterinary Science：LetPub JIF 分区页](https://www.letpub.com/journal-selector/journal/7153)

## 默认数据与授权边界

系统按 PubMed 返回的 `ISSN Linking`、ISSN、期刊名顺序查询 LetPub：

1. 先在 LetPub 搜索页定位期刊 ID；
2. 读取中文详情页的中科院 2025 年 3 月升级版；
3. 读取英文详情页的 WOS JIF 分区；
4. 写入 `data/cache/letpub_rankings.json`，默认有效期 7 天；
5. LetPub 查询失败时先用过期缓存，再用 `data/journal_rankings.csv` 回退。

动态查询可通过环境变量控制：

```powershell
$env:LETPUB_LOOKUP_ENABLED = "true"
$env:LETPUB_CACHE_TTL_DAYS = "7"
```

LetPub 标注 WOS 数据为众包数据，且公开页面结构可能改变。该集成用于科研整理和便捷核查，不是中科院或 Clarivate 官方授权数据库；正式评价、投稿或机构决策前仍应复核。

完整数据可从机构授权来源整理为 CSV，再通过环境变量替换：

```powershell
$env:JOURNAL_RANKINGS_CSV = "D:\authorized\journal_rankings.csv"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

模板位于 `data/journal_rankings.template.csv`。每一行代表同一期刊的一个“中科院小类 + JCR 分类”组合；同一期刊可以有多行。

| 字段 | 含义 |
|---|---|
| `journal_title` | 期刊全名 |
| `issn` / `eissn` | 印刷版与电子版 ISSN，匹配时优先使用 |
| `cas_edition` | 中科院分区版本 |
| `cas_large_category` / `cas_large_zone` | 中科院大类及分区 |
| `cas_small_category` / `cas_small_zone` | 中科院小类及分区 |
| `jcr_edition` | JCR 版本 |
| `jcr_category` / `jcr_quartile` | WOS 分类及 JIF Q1—Q4 |
| `jcr_collection` / `jcr_rank` | 收录集（如 SCIE）及名次 |
| `jcr_metric` | 分区指标，当前固定为 JIF |
| `cas_source_url` / `jcr_source_url` | 数据核查入口 |
| `source_note` | 授权、来源或复核备注 |

## 匹配和缺失规则

1. 优先用 PubMed 返回的 ISSN Linking 或 ISSN 精确查询；
2. ISSN 未命中时，使用期刊名查询；
3. 同一次检索的重复期刊会合并请求，最多并发查询 4 种期刊；
4. 找不到记录时，中科院和 JCR 都显示“未收录”；
5. 系统不会根据影响因子、期刊名或相似期刊推断分区。
