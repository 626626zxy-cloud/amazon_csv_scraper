# Amazon 选品分析工具 — 项目文档

> 最后更新：2026-04-20

---

## 一、项目概览

本项目是一套 Amazon 跨境电商选品分析工具，支持从关键词搜索、ASIN详情+评论抓取，到智能选品评分报告的完整流程。提供 **命令行** 和 **Web界面（Streamlit）** 两种使用方式。

```
关键词输入 → Amazon搜索 → 规则筛选 → ASIN详情抓取 → 评论抓取 → 选品评分 → HTML报告
```

---

## 二、文件清单

### 🔴 核心文件（App 直接依赖）

| 文件 | 大小 | 说明 | 被谁调用 |
|------|------|------|----------|
| `app.py` | 47.8 KB | **Web 主界面**（Streamlit），三Tab布局：关键词输入/分析结果/历史报告 | 用户启动 `streamlit run app.py` |
| `product_analysis_pipeline.py` | 40.3 KB | **选品分析 Pipeline**，串联搜索→详情→评论→分析全流程 | app.py (subprocess) / CLI直接运行 |
| `amazon_scraper.py` | 13.3 KB | **Amazon 搜索页爬虫**（sync），按关键词抓取搜索结果 | product_analysis_pipeline.py, main.py |
| `asin_detail_scraper.py` | 33.6 KB | **ASIN 详情+评论爬虫**（sync），抓取商品详情页和评论 | product_analysis_pipeline.py / CLI直接运行 |
| `rules_engine.py` | 9.2 KB | **选品规则引擎**，加载 Excel 规则并对商品打标签（recommend/watch/exclude） | product_analysis_pipeline.py, main.py, rankings_main.py |
| `ranking.py` | 2.2 KB | **排序与评分**，计算销量代理分数、解析购买量 | product_analysis_pipeline.py, main.py, rankings_main.py |
| `csv_exporter.py` | 638 B | **CSV 导出工具**，将结果保存为CSV | product_analysis_pipeline.py, main.py, rankings_main.py |
| `models.py` | 523 B | **数据模型**，定义 `AmazonSearchProduct` dataclass | amazon_scraper.py, csv_exporter.py |
| `selection_rules.xlsx` | 7.1 KB | **选品规则配置文件**（Excel），定义价格/评论数/品牌等筛选阈值 | rules_engine.py 读取 |

### 🟡 辅助入口（独立功能，App 不直接使用）

| 文件 | 大小 | 说明 | 独立用法 |
|------|------|------|----------|
| `main.py` | 5.4 KB | **关键词搜索 CLI 入口**，搜关键词+规则筛选+导出CSV | `python main.py --keyword "xxx" --headful` |
| `rankings_main.py` | 5.3 KB | **榜单搜索 CLI 入口**，抓Best Sellers/Movers&Shakers/搜索 | `python rankings_main.py --type best_sellers --category automotive` |
| `asin_detail_scraper.py` | 33.6 KB | **ASIN 详情 CLI 入口**（同文件），直接传ASIN抓取 | `python asin_detail_scraper.py B0XXXXX --headful` |
| `amazon_rankings.py` | 19.7 KB | **Amazon 榜单爬虫**（sync），抓取Best Sellers/Movers&Shakers | rankings_main.py |
| `main_bestsellers.py` | 11.6 KB | **Best Sellers 异步爬虫**（async），独立异步方案 | `python main_bestsellers.py --category automotive` |
| `amazon_search_scraper.py` | 19.0 KB | **Amazon 搜索异步爬虫**（async），独立异步方案 | 独立脚本，含反检测措施 |

### 🟢 配置与文档

| 文件 | 说明 |
|------|------|
| `requirements.txt` | Python依赖：playwright, beautifulsoup4, pandas |
| `AMAZON_USAGE_AND_SELECTION_GUIDE.md` | 抓取工具使用说明与低成本选品思路 |
| `README.md` | 项目说明 |

---

## 三、App 调用链路

```
streamlit run app.py
        │
        ├── [用户输入关键词 + 配置参数]
        │
        ├── subprocess 调用 → product_analysis_pipeline.py --keywords ... --headful ...
        │                           │
        │                           ├── amazon_scraper.scrape_keyword()     ← 搜索结果
        │                           ├── rules_engine.annotate_products()    ← 规则筛选
        │                           ├── ranking.add_ranking_fields()        ← 评分排序
        │                           ├── asin_detail_scraper.scrape_product_detail()  ← ASIN详情
        │                           ├── asin_detail_scraper.scrape_product_reviews() ← 评论
        │                           ├── analyze_keyword()                   ← 选品分析
        │                           ├── _save_keyword_results()             ← 保存CSV/JSON
        │                           └── generate_html_report()              ← 生成HTML报告
        │
        ├── [实时读取子进程 stdout，显示进度日志]
        │
        └── [读取 outputs/*.json，展示分析结果 + 提供下载]
```

### 文件依赖关系图

```
app.py
  └─→ product_analysis_pipeline.py (subprocess)
        ├─→ amazon_scraper.py          (from ... import scrape_keyword, AmazonPageError)
        │     └─→ models.py            (from ... import AmazonSearchProduct)
        ├─→ asin_detail_scraper.py     (from ... import scrape_product_detail, scrape_product_reviews, ...)
        ├─→ rules_engine.py            (from ... import annotate_products, load_rules, ...)
        │     └─→ selection_rules.xlsx (读取规则配置)
        ├─→ ranking.py                 (from ... import add_ranking_fields, sort_rows)
        └─→ csv_exporter.py            (from ... import export_rows)
              └─→ models.py            (from ... import AmazonSearchProduct)
```

---

## 四、各文件详细说明

### 1. `app.py` — Web 主界面

- **功能**：Streamlit 应用，用户可手动输入/批量输入/预设选择关键词，自定义参数，一键运行选品分析
- **三个 Tab**：
  - 📝 输入关键词：添加关键词、配置参数（页数/TopN/评论页数/排序/有头模式）、启动分析、实时进度日志
  - 📊 分析结果：评分总览卡片、搜索/详情/评论分析、数据表格、HTML报告下载、CSV数据下载
  - 📂 历史报告：查看和下载历史HTML报告
- **启动方式**：`streamlit run app.py --server.port 8501`
- **关键依赖**：streamlit, pandas, subprocess (调用 product_analysis_pipeline.py)
- **注意事项**：
  - 使用 subprocess 而非直接调用 Playwright，避免 Windows asyncio 冲突
  - 子进程需设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1` 防止中文乱码

### 2. `product_analysis_pipeline.py` — 选品分析 Pipeline

- **功能**：串联关键词搜索 → 规则筛选 → ASIN详情 → 评论 → 选品分析 → HTML报告
- **CLI 参数**：
  - `--keywords "kw1" "kw2"`：关键词列表
  - `--keywords-file file.txt`：从文件读取关键词
  - `--pages N`：搜索页数（默认1）
  - `--top N`：取前N个商品进详情（默认10）
  - `--max-review-pages N`：评论页数（默认3，0=跳过）
  - `--headful`：显示浏览器（推荐，headless易被拦截）
  - `--skip-reviews`：跳过评论抓取
  - `--sort-by`：排序方式（default/sales_proxy/review_count）
- **输出**：
  - `outputs/pipeline_<keyword>_search.csv`：搜索结果
  - `outputs/pipeline_<keyword>_details.csv`：详情数据
  - `outputs/pipeline_<keyword>_reviews.csv`：评论数据
  - `outputs/pipeline_<keyword>_summary.json`：分析摘要
  - `outputs/pipeline_report_<timestamp>.html`：HTML报告
- **核心类**：`KeywordAnalysisResult`（keyword, search_products, details, reviews, summary）
- **核心函数**：
  - `run_pipeline()`：主流程
  - `analyze_keyword()`：单关键词分析（搜索/详情/评论统计 + 评分）
  - `generate_html_report()`：生成HTML报告

### 3. `amazon_scraper.py` — Amazon 搜索页爬虫

- **功能**：按关键词抓取 Amazon 搜索结果页（sync Playwright）
- **核心函数**：
  - `scrape_keyword(browser, keyword, pages, max_items)`：搜索关键词返回商品列表
  - `build_search_url(keyword, page_number)`：构建搜索URL
  - `fetch_search_page_html(page, url, page_number)`：抓取搜索页HTML
  - `parse_search_results(html, keyword, page_number)`：解析搜索结果
- **核心类**：`AmazonPageError`（页面异常）
- **依赖**：models.py (AmazonSearchProduct), playwright, beautifulsoup4

### 4. `asin_detail_scraper.py` — ASIN 详情+评论爬虫

- **功能**：根据 ASIN 列表抓取商品详情页和评论页（sync Playwright）
- **核心数据模型**：
  - `ProductDetail`：商品详情（20+字段：标题/品牌/价格/评分/评论数/排名/分类/特点/描述/参数/图片/变体/Prime/Amazon's Choice/Best Seller等）
  - `ProductReview`：商品评论（评分/标题/作者/日期/验证购买/有用数/正文）
- **核心函数**：
  - `scrape_product_detail(page, asin)`：抓取详情页
  - `extract_reviews_from_detail_page(page, soup, asin)`：从详情页底部提取评论（约8条）
  - `scrape_product_reviews(browser, asin, max_pages, star_filter)`：从独立评论页抓取
- **CLI 用法**：`python asin_detail_scraper.py B0XXXXX --headful`
- **注意事项**：评论独立页在 headless 模式下易被拦截，详情页底部评论稳定可获取

### 5. `rules_engine.py` — 选品规则引擎

- **功能**：从 Excel 文件加载选品规则，对商品打标签（recommend/watch/exclude）
- **核心类**：`SelectionRules`（min_price, preferred_price_min/max, max_review_count, blocked_brands, risky_title_keywords等）
- **核心函数**：
  - `ensure_rules_workbook(path)`：确保规则Excel存在（不存在则创建默认）
  - `load_rules(path)`：加载规则
  - `annotate_products(rows, rules)`：对商品列表打标签
- **规则配置文件**：`selection_rules.xlsx`（含 Rules/BlockedBrands/RiskKeywords/Instructions 四个Sheet）

### 6. `ranking.py` — 排序与评分

- **功能**：计算销量代理分数、解析购买量文本、排序
- **核心函数**：
  - `add_ranking_fields(rows)`：添加 bought_count_est 和 sales_proxy_score 字段
  - `sort_rows(rows, sort_by)`：排序（default/sales_proxy/review_count）
  - `parse_bought_count(value)`：解析 "1K+ bought" 等文本为数字
  - `build_sales_proxy_score(row)`：综合评分（购买量×10 + 评论数 + 评分奖励 + 徽章奖励）

### 7. `csv_exporter.py` — CSV 导出

- **功能**：将商品数据导出为 CSV 文件（UTF-8-BOM 编码）
- **核心函数**：
  - `export_rows(rows, output_path)`：导出字典列表为CSV
  - `export_products(products, output_path)`：导出 AmazonSearchProduct 对象列表

### 8. `models.py` — 数据模型

- **功能**：定义 `AmazonSearchProduct` dataclass
- **字段**：keyword, page_number, position, asin, is_sponsored, brand, title, price, currency, rating, review_count, badge, bought_info, url

---

## 五、输出文件说明

所有输出保存在 `outputs/` 目录：

| 文件模式 | 说明 |
|----------|------|
| `pipeline_<keyword>_search.csv` | 搜索结果（含规则筛选标签） |
| `pipeline_<keyword>_details.csv` | ASIN详情数据 |
| `pipeline_<keyword>_reviews.csv` | 评论数据 |
| `pipeline_<keyword>_summary.json` | 分析摘要（评分/机会/风险/统计） |
| `pipeline_report_<timestamp>.html` | 完整HTML选品分析报告 |
| `<keyword>.csv` | main.py 搜索结果 |
| `<keyword>_selected.csv` | main.py 筛选后推荐+关注商品 |
| `best_sellers_<category>.csv` | Best Sellers榜单 |
| `debug/` | 搜索页调试HTML |
| `debug_asin/` | 详情页调试HTML |

---

## 六、依赖安装

```bash
# 基础依赖（requirements.txt）
pip install playwright beautifulsoup4 pandas

# 浏览器驱动
playwright install chromium

# Web界面额外依赖
pip install streamlit openpyxl

# 隐式依赖（代码中使用但未在requirements.txt列出）
# - streamlit      （app.py）
# - openpyxl       （rules_engine.py）
# - playwright_stealth （main_bestsellers.py，可选）
```

---

## 七、快速启动

```bash
# 1. Web界面（推荐）
cd amazon_csv_scraper
streamlit run app.py --server.port 8501

# 2. CLI一键选品分析
python product_analysis_pipeline.py --keywords "trd grille badge" "window grille inserts" --top 5 --pages 2 --headful

# 3. 单独搜索关键词
python main.py --keyword "dog car seat cover" --pages 2 --headful

# 4. 单独抓取ASIN详情
python asin_detail_scraper.py B0D66LLY1T --headful

# 5. 抓取Best Sellers榜单
python rankings_main.py --type best_sellers --category automotive --headful
```

---

## 八、已知问题与注意事项

1. **必须用 `--headful` 模式**：headless 模式被 Amazon 拦截，返回 "Sorry! Something went wrong!"
2. **评论独立页不稳定**：headful 下也经常抓取0条，建议使用详情页底部评论（约8条，稳定）
3. **Windows 编码问题**：子进程需设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，否则中文乱码
4. **Playwright + Streamlit 冲突**：Windows 下 Playwright sync_api 在 Streamlit asyncio 事件循环中报 `NotImplementedError`，需用 subprocess 调用
5. **变体商品价格缺失**：部分 ASIN 详情页价格返回 None（变体选择器导致），需人工核实
6. **请求频率**：已内置随机延迟（2-5秒），大量抓取时注意控制频率避免被封
