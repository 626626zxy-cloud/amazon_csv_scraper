# 亚马逊抓取工具使用说明与低成本选品思路

## 1. 文档目的

这份文档给你两个长期可复用的参考：

1. 现在这套 Amazon 抓取脚本怎么用（CLI + Web 界面）
2. 在预算有限、只能看到 Amazon 和 eBay 页面信息的情况下，应该如何做低成本选品

你的当前阶段不适合一上来做复杂的"智能选品平台"，更适合先做一个"页面信息驱动的选品助手"。核心目标不是精准预测销量，而是把大量候选商品快速筛到一个更值得人工复查的范围。

---

## 2. 脚本能做什么

当前脚本提供两种使用方式：**命令行 (CLI)** 和 **Web 界面 (Streamlit)**，用于按关键词抓取 Amazon 搜索结果页商品，并导出 CSV / HTML 报告。

### 2.1 支持的站点

19 个 Amazon 站点：

| 代码 | 站点 | 域名 | 货币 |
|------|------|------|------|
| US | 🇺🇸 美国 | amazon.com | USD |
| UK | 🇬🇧 英国 | amazon.co.uk | GBP |
| DE | 🇩🇪 德国 | amazon.de | EUR |
| FR | 🇫🇷 法国 | amazon.fr | EUR |
| IT | 🇮🇹 意大利 | amazon.it | EUR |
| ES | 🇪🇸 西班牙 | amazon.es | EUR |
| JP | 🇯🇵 日本 | amazon.co.jp | JPY |
| CA | 🇨🇦 加拿大 | amazon.ca | CAD |
| AU | 🇦🇺 澳大利亚 | amazon.com.au | AUD |
| MX | 🇲🇽 墨西哥 | amazon.com.mx | MXN |
| BR | 🇧🇷 巴西 | amazon.com.br | BRL |
| IN | 🇮🇳 印度 | amazon.in | INR |
| SG | 🇸🇬 新加坡 | amazon.sg | SGD |
| AE | 🇦🇪 阿联酋 | amazon.ae | AED |
| SA | 🇸🇦 沙特 | amazon.sa | SAR |
| NL | 🇳🇱 荷兰 | amazon.nl | EUR |
| SE | 🇸🇪 瑞典 | amazon.se | SEK |
| PL | 🇵🇱 波兰 | amazon.pl | PLN |
| BE | 🇧🇪 比利时 | amazon.com.be | EUR |

### 2.2 支持的搜索排序

| 排序代码 | 说明 | 适用场景 |
|----------|------|----------|
| `relevance` | 相关度（默认） | 通用浏览 |
| `price-asc` | 价格从低到高 | 找低价竞品 |
| `price-desc` | 价格从高到低 | 找高端产品 |
| `review-rank` | 评论数排序 | 找热卖品 |
| `date-desc` | 最新上架 | 找新品机会 |
| `avg-rating` | 平均评分 | 找高口碑品 |

### 2.3 搜索结果字段

- `keyword` — 搜索关键词
- `page_number` — 页码
- `position` — 页面排名
- `asin` — Amazon 标准识别号
- `is_sponsored` — 是否广告位
- `brand` — 品牌
- `title` — 商品标题
- `price` — 价格
- `currency` — 货币
- `rating` — 评分
- `review_count` — 评论数
- `badge` — 标签（Amazon's Choice 等）
- `bought_info` — 购买热度（如 "100+ bought in past month"）
- `url` — 商品链接
- `marketplace` — 站点代码

### 2.4 选品分析报告字段

在搜索数据基础上，Pipeline 还会抓取 ASIN 详情页和评论页，生成：

- **详情数据**：Prime 标识、Amazon's Choice、卖家类型、变体数量、BSR 排名等
- **评论数据**：评分分布、评论内容、评论日期等
- **选品评分**：综合竞争/需求/利润/风险给出 0-100 分和推荐等级

---

## 3. 如何使用

### 3.1 Web 界面（推荐）

```powershell
cd D:\work\amazon_csv_scraper
streamlit run app.py --server.port 8501
```

浏览器打开 `http://localhost:8501`，左侧配置参数，右侧操作。

**侧边栏参数说明：**

| 参数 | 说明 | 默认值 | 建议 |
|------|------|--------|------|
| 搜索页数 | 翻几页搜索结果 | 2 | 多抓数据设 3-5 页 |
| **搜索商品上限** | 最多抓取多少条搜索结果 | 0（不限制） | 想多抓设 30-100，0 表示翻完所有页 |
| **进入详情的商品数** | 从搜索结果取前N个进详情+评论 | 5 | **想多抓设 10-20**，这是影响分析数据量的关键 |
| 评论页数 | 每个商品抓几页评论 | 3 | 0 = 跳过评论 |
| 排序方式 | 分析结果排序 | 销量代理 | — |
| 显示浏览器 | 有头/无头模式 | ✅ | **必须开启**，headless 会被 Amazon 拦截 |
| **Amazon 站点** | 选择站点 | US | 支持 19 个站点 |
| **搜索排序** | Amazon 页面上的排序 | 相关度 | 按评论排序找热卖 |
| **价格区间** | 最低/最高价筛选 | 不限制 | 筛掉太便宜/太贵的 |

### 3.2 CLI：简单搜索（main.py）

```powershell
cd D:\work\amazon_csv_scraper

# 最基础的用法
python .\main.py --keyword "lip balm" --headful

# 多抓数据：200 条，按销量排序
python .\main.py --keyword "lip balm" --limit 200 --sort-by sales_proxy --headful

# 指定站点、排序、价格区间
python .\main.py --keyword "dog car seat cover" --marketplace UK --search-sort price-asc --min-price 10 --max-price 50 --headful

# 日本站
python .\main.py --keyword "車用ホルダー" --marketplace JP --headful

# 对已有 CSV 重新筛选
python .\main.py --input-csv .\outputs\lip_balm_limit100.csv --sort-by sales_proxy

# 初始化可编辑规则表
python .\main.py --init-rules-only
```

**main.py 参数说明：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--keyword` | 搜索关键词 | 必填 |
| `--pages` | 抓取页数 | 1 |
| `--limit` | 最多抓取商品数 | 不限制 |
| `--top` | 最终保留前 N 条 | 不限制 |
| `--sort-by` | 分析排序（sales_proxy/review_count/default） | default |
| `--marketplace` | Amazon 站点 | US |
| `--search-sort` | 搜索页排序（relevance/price-asc/price-desc/review-rank/date-desc/avg-rating） | relevance |
| `--min-price` | 最低价格 | 不限制 |
| `--max-price` | 最高价格 | 不限制 |
| `--output` | 输出 CSV 路径 | 自动生成 |
| `--headful` | 显示浏览器窗口 | 否 |
| `--rules-xlsx` | 规则文件路径 | 默认 |
| `--init-rules-only` | 仅初始化规则 | — |
| `--input-csv` | 对已有 CSV 重新筛选 | — |

### 3.3 CLI：选品分析 Pipeline（product_analysis_pipeline.py）

Pipeline = 搜索 + ASIN详情 + 评论 + 分析评分 + HTML 报告，一步到位。

```powershell
cd D:\work\amazon_csv_scraper

# 单关键词，基础用法
python .\product_analysis_pipeline.py --keywords "lip balm" --headful

# 多关键词，多页，进详情 15 个
python .\product_analysis_pipeline.py --keywords "lip balm" "dog car seat cover" --pages 3 --top 15 --headful

# 英国站，按评论排序，价格区间
python .\product_analysis_pipeline.py --keywords "car phone mount" --marketplace UK --search-sort review-rank --min-price 10 --max-price 50 --top 10 --headful

# 从文件读取关键词
python .\product_analysis_pipeline.py --keywords-file keywords.txt --top 10 --headful

# 搜索上限 50 条，跳过评论加快速度
python .\product_analysis_pipeline.py --keywords "grille" --max-search-items 50 --skip-reviews --headful
```

**pipeline 参数说明：**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--keywords` / `-k` | 关键词列表（空格分隔） | 必填 |
| `--keywords-file` | 关键词文件（每行一个） | — |
| `--pages` | 搜索页数 | 1 |
| `--top` | 进入详情的商品数 | 10 |
| `--max-search-items` | 搜索商品上限（0或不传=不限制） | 不限制 |
| `--max-review-pages` | 评论页数 | 3 |
| `--headful` | 显示浏览器 | 否 |
| `--skip-reviews` | 跳过评论 | 否 |
| `--sort-by` | 分析排序 | sales_proxy |
| `--marketplace` | 站点 | US |
| `--search-sort` | 搜索页排序 | relevance |
| `--min-price` | 最低价格 | 不限制 |
| `--max-price` | 最高价格 | 不限制 |
| `--rules-xlsx` | 规则文件 | 默认 |

---

## 4. 为什么有时候只能抓到很少的数据？

这是最常见的疑问，原因通常有三种：

### 4.1 Amazon 搜索结果本身就少

有些关键词在 Amazon 上的搜索结果只有几个到几十个。特别是：
- **加了价格区间后**，符合条件的商品更少（如 $10-$100 可能只有 4 个）
- **小众关键词**本身搜索结果就少
- **非英文站**（如 JP/DE）某些词结果更少

**解决方法**：去掉价格区间、换更宽泛的关键词、增加搜索页数。

### 4.2 "搜索商品上限"设得太小

Web 界面中 **搜索商品上限 = 0** 表示不限制（翻完所有页），设了值（如 5）就只抓 5 条搜索结果。

**解决方法**：设为 0 或更大的值（30-100）。

### 4.3 "进入详情的商品数"设得太小

这个参数决定了从搜索结果中取几个 ASIN 进详情页深度抓取。**这是影响最终分析数据量最关键的参数。**

- 设 5 = 只有 5 个商品有详情+评论数据
- 设 20 = 20 个商品有详情+评论数据

**解决方法**：调大到 10-20，更多商品会进入详情抓取（但耗时更长）。

### 参数组合建议

| 场景 | 搜索页数 | 搜索上限 | 进详情数 | 评论页数 |
|------|----------|----------|----------|----------|
| 快速试探 | 1 | 0 | 3 | 0 |
| 标准分析 | 2 | 0 | 5-10 | 2 |
| 深度分析 | 3-5 | 0 | 15-20 | 3 |
| 大规模扫描 | 5 | 100 | 20-30 | 1 |

---

## 5. 输出结果怎么看

### 5.1 CSV 输出（main.py）

在 `outputs` 目录生成 CSV，用 Excel 打开。建议优先看：

- `price`：价格带是否健康
- `review_count`：评论门槛高不高
- `brand`：是否被品牌强占
- `is_sponsored`：前排广告位是否很多
- `bought_info`：是否有购买热度信号
- `badge`：是否有 Amazon's Choice 等标签

### 5.2 选品分析报告（pipeline / Web）

生成 HTML 报告，包含：

- **评分总览**：每个关键词 0-100 分 + 推荐等级（推荐/适中/观望）
- **机会与风险**：绿色/红色卡片
- **搜索分析**：价格区间、均价、评分、品牌分布、广告占比
- **详情分析**：成功率、Prime 比例、Amazon's Choice 占比
- **评论分析**：评分分布图、高频词、差评痛点
- **数据表格**：完整搜索/详情/评论数据

Web 界面支持在线查看 + 下载 HTML 报告 + 下载 CSV 数据。

---

## 6. 常见报错与处理方式

### 6.1 `Sorry! Something went wrong!`

Amazon 返回异常页，常见于 headless 模式或访问过快。

**处理**：
- **必须加 `--headful`**（headless 被拦截）
- 过一会儿重试
- 先降 `--pages` 到 1
- 用更具体的关键词

### 6.2 搜索结果很少（只有几个）

通常不是 bug，而是关键词本身结果少或价格区间太窄。

**处理**：
- 去掉价格区间
- 用更宽泛的关键词
- 换个站点试试
- 增加 `--pages`

### 6.3 为什么有时能跑，有时不能跑

Amazon 返回页面不稳定，同一关键词不同时间可能结果不同。

### 6.4 Playwright 报 `NotImplementedError`

在 Streamlit 内不能直接用 Playwright sync_api，已改为 subprocess 调用，Web 界面不受影响。

### 6.5 中文乱码

Windows 下 Python 子进程默认 GBK 编码，已在代码中设置 `PYTHONIOENCODING=utf-8`。

---

## 7. 适合你的低成本选品思路

结合你现在的条件，你最适合的不是"全自动智能选品系统"，而是"搜索结果驱动的选品研究流程"。

你现在能稳定利用的信息主要来自：

- Amazon 搜索结果页
- Amazon 商品详情页可见信息
- eBay 搜索结果页

在这个基础上，选品的目标应该是：

1. 快速筛掉明显不适合做的品
2. 找出值得进一步查看的候选商品
3. 给出结构化判断理由，而不是只凭感觉翻页

---

## 8. 你应该重点看哪些信号

### 8.1 需求信号

说明这个市场可能有人买：

- 搜索结果页商品很多
- 有 `bought in past month`
- 有一定评论数，且不是全部为 0
- eBay 有成交或已售提示

### 8.2 竞争信号

说明这个词是否难打：

- 首页头部商品评论数是否非常高
- 前排是否大量是 Sponsored
- 是否被少数品牌反复占位
- 标签是否过于集中在头部强品

### 8.3 利润空间信号

你暂时拿不到真实采购成本，就先看代理信号：

- 售价不要太低
- 同类价格是否有明显层次
- 是否有升级、捆绑、改款空间

### 8.4 风险信号

这一类特别要注意：

- 明显品牌词
- 医疗、儿童安全、认证类商品
- 易碎、大件、强季节性产品
- 明显容易侵权的类目

---

## 9. 推荐的实际操作流程

### 方式 A：Web 界面（推荐新手）

1. 启动：`streamlit run app.py --server.port 8501`
2. 输入关键词（手动输入或选择预设）
3. 侧边栏调参数（站点、排序、价格区间、商品数）
4. 点"开始选品分析"，实时看进度
5. 分析完成后在"分析结果"页查看评分和详情
6. 下载 HTML 报告或 CSV 数据

### 方式 B：CLI 快速搜索

1. 输入关键词抓 Amazon 前 1-2 页
2. 用 CSV 看价格、评论、品牌、广告和购买热度
3. 把候选词分成"可做 / 观察 / 放弃"
4. 只对值得看的词再跑 Pipeline 深度分析

### 方式 C：CLI 一键分析

1. 直接跑 Pipeline，搜索+详情+评论+评分一步到位
2. 查看 HTML 报告中的选品建议
3. 交叉验证 eBay 数据

---

## 10. 现阶段最重要的原则

- 先做筛选，不追求预测绝对销量
- 先抓搜索结果页，不急着做全站抓取
- 先建立自己的判断框架，再谈智能化
- 规则先跑通，AI 只做总结和辅助解释
- 目标是减少人工翻页时间，而不是幻想一次找出爆款
- **必须用 `--headful` 模式**，headless 被 Amazon 拦截
- **想多抓数据**：增大 `--top`（进详情数）和 `--pages`（搜索页数），去掉价格区间

如果后续继续升级，这套脚本最值得补的方向是：

- eBay 搜索页导出
- 链接标准化
- 风险词库
- 简单打分模型
- 自动生成候选商品报告

这条路线对你现在最现实，也最容易做出真正有用的结果。
