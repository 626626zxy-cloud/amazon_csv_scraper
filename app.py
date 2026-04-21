#!/usr/bin/env python3
"""
Amazon 选品分析 - Web 界面
========================
Streamlit 应用，用户可手动输入关键词，自定义参数，一键运行选品分析流程。
每一步实时显示在页面上，分析结果可视化展示+HTML报告下载。

启动方式：
    cd amazon_csv_scraper
    streamlit run app.py
"""

import base64
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st


# ═══════════════════════════════════════════════════════════════════
# 路径
# ═══════════════════════════════════════════════════════════════════

PROJECT_DIR = Path(__file__).parent
PIPELINE_SCRIPT = PROJECT_DIR / "product_analysis_pipeline.py"
OUTPUTS_DIR = PROJECT_DIR / "outputs"


# ═══════════════════════════════════════════════════════════════════
# 页面配置
# ═══════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Amazon 选品分析",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .stAlert { padding-top: 0.5rem; padding-bottom: 0.5rem; }
    div[data-testid="stSidebar"] { background: linear-gradient(180deg, #232f3e 0%, #37475a 100%); }
    div[data-testid="stSidebar"] * { color: white !important; }
    div[data-testid="stSidebar"] .stTextInput > div > div > input { color: #1a1a2e !important; }
    div[data-testid="stSidebar"] .stNumberInput input { color: #1a1a2e !important; }
    div[data-testid="stSidebar"] .stSelectbox > div > div > div { color: #1a1a2e !important; }
    .keyword-tag {
        display: inline-block; padding: 4px 12px; margin: 4px;
        border-radius: 16px; font-size: 13px;
        background: #ff9900; color: white; font-weight: 500;
    }
    .score-card {
        background: white; border-radius: 12px; padding: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align: center;
        border: 2px solid #f0f0f0; transition: all 0.3s;
    }
    .score-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.12); transform: translateY(-2px); }
    .score-big { font-size: 48px; font-weight: 800; line-height: 1.1; }
    .log-line { font-size: 12px; font-family: 'Consolas', monospace; color: #555; line-height: 1.6; }
    .metric-highlight { font-size: 24px; font-weight: 700; color: #ff9900; }
    .pain-card { background: #fff3f3; border-left: 4px solid #dc3545; padding: 10px 14px; margin: 6px 0; border-radius: 4px; }
    .opp-card { background: #f0fff4; border-left: 4px solid #28a745; padding: 10px 14px; margin: 6px 0; border-radius: 4px; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════
# 初始化 Session State
# ═══════════════════════════════════════════════════════════════════

def init_state():
    defaults = {
        "keywords": [],
        "running": False,
        "results": [],
        "report_path": "",
        "log_lines": [],
        "process_returncode": None,
        "active_tab": "input",  # input | running | results
        "url_mode": False,
        "url_list": [],
        "url_results": [],  # 独立的URL分析结果
        "url_report_path": "",
        "url_log_lines": [],
        "url_process_returncode": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()


# ═══════════════════════════════════════════════════════════════════
# 数据加载函数
# ═══════════════════════════════════════════════════════════════════

def find_keyword_files(keyword: str) -> Dict[str, str]:
    """根据关键词找到对应的 CSV/JSON 文件"""
    safe_kw = re.sub(r'[^\w\s-]', '', keyword).replace(' ', '_')
    pattern_prefix = str(OUTPUTS_DIR / f"pipeline_{safe_kw}")
    files = {
        "summary": None,
        "search_csv": None,
        "details_csv": None,
        "reviews_csv": None,
    }
    for f in glob.glob(f"{pattern_prefix}_summary.json"):
        if files["summary"] is None or f > files["summary"]:
            files["summary"] = f
    for f in glob.glob(f"{pattern_prefix}_search.csv"):
        if files["search_csv"] is None or f > files["search_csv"]:
            files["search_csv"] = f
    for f in glob.glob(f"{pattern_prefix}_details.csv"):
        if files["details_csv"] is None or f > files["details_csv"]:
            files["details_csv"] = f
    for f in glob.glob(f"{pattern_prefix}_reviews.csv"):
        if files["reviews_csv"] is None or f > files["reviews_csv"]:
            files["reviews_csv"] = f
    return files


def load_keyword_data(keyword: str) -> Optional[Dict]:
    """加载某个关键词的全部数据"""
    files = find_keyword_files(keyword)
    if not files["summary"]:
        return None

    try:
        with open(files["summary"], "r", encoding="utf-8") as f:
            summary = json.load(f)
    except Exception:
        return None

    data = {
        "keyword": keyword,
        "summary": summary,
        "search_df": None,
        "details_df": None,
        "reviews_df": None,
        "files": files,
    }

    if files["search_csv"] and Path(files["search_csv"]).exists():
        try:
            data["search_df"] = pd.read_csv(files["search_csv"], encoding="utf-8")
        except Exception:
            pass

    if files["details_csv"] and Path(files["details_csv"]).exists():
        try:
            data["details_df"] = pd.read_csv(files["details_csv"], encoding="utf-8")
        except Exception:
            pass

    if files["reviews_csv"] and Path(files["reviews_csv"]).exists():
        try:
            data["reviews_df"] = pd.read_csv(files["reviews_csv"], encoding="utf-8")
        except Exception:
            pass

    return data


def get_all_analyzed_keywords() -> List[str]:
    """扫描 outputs/ 中所有已分析的关键词"""
    keywords = []
    for f in glob.glob(str(OUTPUTS_DIR / "pipeline_*_summary.json")):
        name = Path(f).stem  # pipeline_xxx_summary
        kw_part = name.replace("pipeline_", "").replace("_summary", "")
        kw = kw_part.replace("_", " ")
        keywords.append(kw)
    return keywords


def generate_standalone_html_report(results_data: List[Dict]) -> str:
    """生成独立美观的 HTML 报告，包含完整分析结果"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    html_parts = [f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Amazon 选品分析报告 - {now}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
    .header {{ background: linear-gradient(135deg, #232f3e, #ff9900); color: white; padding: 40px; border-radius: 16px; margin-bottom: 30px; }}
    .header h1 {{ font-size: 28px; margin-bottom: 8px; }}
    .header p {{ opacity: 0.85; font-size: 14px; }}
    .score-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 30px; }}
    .score-card {{ background: white; border-radius: 12px; padding: 24px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
    .score-card .kw {{ font-size: 13px; color: #636e72; margin-bottom: 8px; }}
    .score-card .num {{ font-size: 52px; font-weight: 800; line-height: 1.1; }}
    .score-card .level {{ font-size: 14px; font-weight: 600; margin-top: 4px; }}
    .section {{ background: white; border-radius: 12px; padding: 28px; margin-bottom: 20px; box-shadow: 0 2px 12px rgba(0,0,0,0.04); }}
    .section h2 {{ font-size: 20px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 2px solid #f0f0f0; }}
    .metrics {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin: 16px 0; }}
    .metric {{ background: #f8f9fa; border-radius: 8px; padding: 14px; text-align: center; }}
    .metric .label {{ font-size: 12px; color: #636e72; }}
    .metric .value {{ font-size: 20px; font-weight: 700; color: #2d3436; }}
    .opp-list {{ list-style: none; }}
    .opp-list li {{ background: #f0fff4; border-left: 4px solid #28a745; padding: 10px 14px; margin: 6px 0; border-radius: 4px; }}
    .risk-list {{ list-style: none; }}
    .risk-list li {{ background: #fff3f3; border-left: 4px solid #dc3545; padding: 10px 14px; margin: 6px 0; border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
    th {{ background: #232f3e; color: white; padding: 10px 12px; text-align: left; font-weight: 600; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #eee; }}
    tr:hover {{ background: #f8f9fa; }}
    .tag-recommend {{ background: #28a745; color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
    .tag-watch {{ background: #ffc107; color: #333; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
    .tag-skip {{ background: #dc3545; color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
    .rating-bar {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
    .rating-bar .stars {{ width: 70px; font-size: 12px; }}
    .rating-bar .bar-bg {{ flex: 1; height: 16px; background: #eee; border-radius: 8px; overflow: hidden; }}
    .rating-bar .bar-fill {{ height: 100%; border-radius: 8px; }}
    .word-cloud {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
    .word-tag {{ background: #e8f4fd; padding: 4px 12px; border-radius: 16px; font-size: 13px; }}
    .word-tag.hot {{ background: #fff3e0; font-weight: 600; }}
    .pain-card {{ background: #fff3f3; border-left: 4px solid #dc3545; padding: 12px 16px; margin: 8px 0; border-radius: 4px; }}
    .pain-card .title {{ font-weight: 600; color: #dc3545; }}
    .pain-card .body {{ color: #555; font-size: 13px; margin-top: 4px; }}
    a {{ color: #007bff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .footer {{ text-align: center; padding: 20px; color: #999; font-size: 12px; }}
    @media print {{ body {{ background: white; }} .section {{ box-shadow: none; border: 1px solid #eee; }} }}
</style>
</head>
<body>
<div class="container">
<div class="header">
    <h1>🔍 Amazon 选品分析报告</h1>
    <p>关键词搜索 → ASIN详情+评论 → 智能选品分析 | 生成时间: {now}</p>
</div>
"""]

    # 评分卡片
    html_parts.append('<div class="score-cards">')
    for r in results_data:
        kw = r.get("keyword", "?")
        summary = r.get("summary", {})
        if "error" in summary:
            score, level, color = 0, "失败", "#dc3545"
        else:
            rec = summary.get("recommendation", {})
            score = rec.get("score", 0)
            level = rec.get("level", "未知")
            color = {"推荐": "#28a745", "适中": "#ffc107", "观望": "#dc3545", "避开": "#6c757d"}.get(level, "#6c757d")
        html_parts.append(f"""
        <div class="score-card">
            <div class="kw">{kw}</div>
            <div class="num" style="color:{color}">{score}</div>
            <div class="level" style="color:{color}">{level}</div>
        </div>""")
    html_parts.append('</div>')

    # 每个关键词详情
    for r in results_data:
        kw = r.get("keyword", "?")
        summary = r.get("summary", {})

        if "error" in summary:
            html_parts.append(f'<div class="section"><h2>❌ {kw}</h2><p>搜索失败: {summary["error"]}</p></div>')
            continue

        rec = summary.get("recommendation", {})
        score = rec.get("score", 0)
        level = rec.get("level", "未知")
        color = {"推荐": "#28a745", "适中": "#ffc107", "观望": "#dc3545", "避开": "#6c757d"}.get(level, "#6c757d")

        html_parts.append(f'<div class="section">')
        html_parts.append(f'<h2>{"✅" if score >= 70 else "⚠️" if score >= 50 else "⛔"} {kw} — <span style="color:{color}">{score}分 ({level})</span></h2>')

        # 机会与风险
        opps = rec.get("opportunities", [])
        risks = rec.get("risks", [])
        if opps or risks:
            html_parts.append('<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0">')
            html_parts.append('<div><h4 style="color:#28a745;margin-bottom:8px">✅ 机会</h4><ul class="opp-list">')
            for o in opps:
                html_parts.append(f'<li>{o}</li>')
            html_parts.append('</ul></div>')
            html_parts.append('<div><h4 style="color:#dc3545;margin-bottom:8px">⚠️ 风险</h4><ul class="risk-list">')
            for rk in risks:
                html_parts.append(f'<li>{rk}</li>')
            html_parts.append('</ul></div></div>')

        # 搜索分析
        sa = summary.get("search_analysis", {})
        if sa:
            html_parts.append('<h3 style="margin:20px 0 12px">📊 搜索结果分析</h3>')
            html_parts.append('<div class="metrics">')
            for label, key in [("价格区间", "price_range"), ("均价", "avg_price"), ("中位价", "median_price"),
                               ("平均评分", "avg_rating"), ("平均评论数", "avg_reviews"), ("近期购买量", "total_bought"),
                               ("广告占比", "sponsored_ratio")]:
                val = sa.get(key, "N/A")
                html_parts.append(f'<div class="metric"><div class="label">{label}</div><div class="value">{val}</div></div>')
            html_parts.append('</div>')

            decisions = sa.get("decisions", {})
            if decisions:
                html_parts.append(f'<p>筛选结果: <span class="tag-recommend">推荐 {decisions.get("recommend", 0)}</span> <span class="tag-watch">关注 {decisions.get("watch", 0)}</span> <span class="tag-skip">排除 {decisions.get("skip", 0)}</span></p>')

            brands = sa.get("brands", {})
            if brands:
                html_parts.append('<p style="margin-top:8px">品牌分布: ')
                for b, c in list(brands.items())[:5]:
                    html_parts.append(f'<span class="word-tag">{b} ({c})</span>')
                html_parts.append('</p>')

        # 详情分析
        da = summary.get("detail_analysis", {})
        if da:
            html_parts.append('<h3 style="margin:20px 0 12px">🔍 详情分析</h3>')
            html_parts.append('<div class="metrics">')
            for label, key in [("抓取成功率", "success_rate"), ("详情价格区间", "price_range"), ("详情均价", "avg_price"),
                               ("详情平均评分", "avg_rating"), ("Prime比例", "prime_ratio"),
                               ("Amazon's Choice", "amazon_choice_count"), ("Best Seller", "best_seller_count")]:
                val = da.get(key, "N/A")
                html_parts.append(f'<div class="metric"><div class="label">{label}</div><div class="value">{val}</div></div>')
            html_parts.append('</div>')

        # 评论分析
        ra = summary.get("review_analysis", {})
        if ra:
            html_parts.append('<h3 style="margin:20px 0 12px">💬 评论分析</h3>')
            html_parts.append('<div class="metrics">')
            for label, key in [("评论总数", "total_reviews"), ("平均评分", "avg_rating"), ("验证购买比", "verified_ratio")]:
                val = ra.get(key, "N/A")
                html_parts.append(f'<div class="metric"><div class="label">{label}</div><div class="value">{val}</div></div>')
            html_parts.append('</div>')

            # 评分分布
            rating_dist = ra.get("rating_distribution", {})
            if rating_dist:
                total_r = sum(int(v) for v in rating_dist.values()) or 1
                html_parts.append('<div style="margin:12px 0">')
                for star in [5, 4, 3, 2, 1]:
                    count = int(rating_dist.get(str(star), 0))
                    pct = count / total_r * 100
                    bar_color = ["#dc3545", "#ff9900", "#ffc107", "#8bc34a", "#28a745"][star - 1]
                    html_parts.append(f'''<div class="rating-bar">
                        <div class="stars">{"⭐" * star}</div>
                        <div class="bar-bg"><div class="bar-fill" style="width:{pct}%;background:{bar_color}"></div></div>
                        <div style="width:80px;font-size:12px;color:#666">{count} ({pct:.0f}%)</div>
                    </div>''')
                html_parts.append('</div>')

            # 高频词
            top_words = ra.get("top_words", {})
            if top_words:
                html_parts.append('<div class="word-cloud">')
                for w, c in list(top_words.items())[:20]:
                    cls = "hot" if c > 5 else ""
                    html_parts.append(f'<span class="word-tag {cls}">{w} ({c})</span>')
                html_parts.append('</div>')

            # 痛点
            pain_points = ra.get("pain_points", [])
            if pain_points:
                html_parts.append('<h4 style="color:#dc3545;margin:16px 0 8px">⚠️ 差评痛点</h4>')
                for pp in pain_points[:5]:
                    html_parts.append(f'''<div class="pain-card">
                        <div class="title">⭐{pp["rating"]} {pp["title"]}</div>
                        <div class="body">{pp["body"][:200]}</div>
                    </div>''')

        # 搜索结果表格
        search_df = r.get("search_df")
        if search_df is not None and len(search_df) > 0:
            html_parts.append('<h3 style="margin:20px 0 12px">📋 搜索结果商品</h3>')
            html_parts.append('<table><thead><tr>')
            html_parts.append('<th>ASIN</th><th>标题</th><th>品牌</th><th>价格</th><th>评分</th><th>评论</th><th>月销</th><th>决策</th>')
            html_parts.append('</tr></thead><tbody>')
            for _, row in search_df.iterrows():
                decision = row.get("decision", "")
                tag_cls = {"recommend": "tag-recommend", "watch": "tag-watch", "skip": "tag-skip"}.get(str(decision), "")
                decision_text = {"recommend": "推荐", "watch": "关注", "skip": "排除"}.get(str(decision), str(decision))
                asin = row.get("asin", "")
                link = f'https://www.amazon.com/dp/{asin}'
                html_parts.append(f'''<tr>
                    <td><a href="{link}" target="_blank">{asin}</a></td>
                    <td>{str(row.get("title",""))[:80]}</td>
                    <td>{row.get("brand","")}</td>
                    <td>${row.get("price","-")}</td>
                    <td>{row.get("rating","-")}</td>
                    <td>{row.get("review_count","-")}</td>
                    <td>{row.get("bought_count_est","-")}</td>
                    <td><span class="{tag_cls}">{decision_text}</span></td>
                </tr>''')
            html_parts.append('</tbody></table>')

        # 详情表格
        details_df = r.get("details_df")
        if details_df is not None and len(details_df) > 0:
            html_parts.append('<h3 style="margin:20px 0 12px">🔍 详情抓取结果</h3>')
            html_parts.append('<table><thead><tr>')
            html_parts.append('<th>ASIN</th><th>标题</th><th>品牌</th><th>价格</th><th>评分</th><th>评论</th><th>Prime</th><th>A Choice</th><th>类目</th>')
            html_parts.append('</tr></thead><tbody>')
            for _, row in details_df.iterrows():
                if row.get("scrape_status") != "success":
                    continue
                asin = row.get("asin", "")
                link = f'https://www.amazon.com/dp/{asin}'
                html_parts.append(f'''<tr>
                    <td><a href="{link}" target="_blank">{asin}</a></td>
                    <td>{str(row.get("title",""))[:80]}</td>
                    <td>{row.get("brand","")}</td>
                    <td>${row.get("price","-")}</td>
                    <td>{row.get("rating","-")}</td>
                    <td>{row.get("review_count","-")}</td>
                    <td>{"✅" if row.get("is_prime") else "-"}</td>
                    <td>{"✅" if row.get("is_amazon_choice") else "-"}</td>
                    <td>{str(row.get("category_path",""))[:50]}</td>
                </tr>''')
            html_parts.append('</tbody></table>')

        # 评论表格
        reviews_df = r.get("reviews_df")
        if reviews_df is not None and len(reviews_df) > 0:
            html_parts.append('<h3 style="margin:20px 0 12px">💬 评论数据（前20条）</h3>')
            html_parts.append('<table><thead><tr>')
            html_parts.append('<th>ASIN</th><th>评分</th><th>标题</th><th>作者</th><th>日期</th><th>验证</th><th>内容</th>')
            html_parts.append('</tr></thead><tbody>')
            for _, row in reviews_df.head(20).iterrows():
                html_parts.append(f'''<tr>
                    <td>{row.get("asin","")}</td>
                    <td>⭐{row.get("rating","-")}</td>
                    <td>{str(row.get("title",""))[:50]}</td>
                    <td>{row.get("author","")}</td>
                    <td>{str(row.get("date",""))[:20]}</td>
                    <td>{"✅" if row.get("verified_purchase") else "-"}</td>
                    <td>{str(row.get("body",""))[:120]}</td>
                </tr>''')
            html_parts.append('</tbody></table>')

        html_parts.append('</div>')

    html_parts.append(f"""
<div class="footer">
    Amazon 选品分析报告 | 生成时间: {now} | Powered by Playwright + Streamlit
</div>
</div>
</body>
</html>""")

    return "\n".join(html_parts)


# ═══════════════════════════════════════════════════════════════════
# 侧边栏
# ═══════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## 🔧 参数配置")

    pages = st.number_input("搜索页数", min_value=1, max_value=10, value=2)
    max_search_items = st.number_input(
        "搜索商品上限", min_value=1, max_value=500, value=50,
        help="搜索时最多抓取多少条商品（默认50条）"
    )
    top_n = st.number_input("进入详情的商品数", min_value=1, max_value=30, value=5,
                            help="从搜索结果中取前N个进入详情页抓取")
    max_review_pages = st.number_input("评论页数", min_value=0, max_value=20, value=3)
    sort_by = st.selectbox(
        "排序方式", ["sales_proxy", "review_count", "default"],
        format_func=lambda x: {"sales_proxy": "销量代理排序", "review_count": "评论数排序", "default": "默认排序"}[x],
    )
    headful = st.checkbox("显示浏览器（推荐）", value=True)

    st.markdown("---")
    st.markdown("### 🌐 站点与筛选")

    # Amazon 站点选择
    marketplace_options = {
        "US": "🇺🇸 美国 (amazon.com)",
        "UK": "🇬🇧 英国 (amazon.co.uk)",
        "DE": "🇩🇪 德国 (amazon.de)",
        "FR": "🇫🇷 法国 (amazon.fr)",
        "IT": "🇮🇹 意大利 (amazon.it)",
        "ES": "🇪🇸 西班牙 (amazon.es)",
        "JP": "🇯🇵 日本 (amazon.co.jp)",
        "CA": "🇨🇦 加拿大 (amazon.ca)",
        "AU": "🇦🇺 澳大利亚 (amazon.com.au)",
        "MX": "🇲🇽 墨西哥 (amazon.com.mx)",
        "BR": "🇧🇷 巴西 (amazon.com.br)",
        "IN": "🇮🇳 印度 (amazon.in)",
        "SG": "🇸🇬 新加坡 (amazon.sg)",
        "AE": "🇦🇪 阿联酋 (amazon.ae)",
        "SA": "🇸🇦 沙特 (amazon.sa)",
        "NL": "🇳🇱 荷兰 (amazon.nl)",
        "SE": "🇸🇪 瑞典 (amazon.se)",
        "PL": "🇵🇱 波兰 (amazon.pl)",
        "BE": "🇧🇪 比利时 (amazon.com.be)",
    }
    marketplace = st.selectbox(
        "Amazon 站点",
        options=list(marketplace_options.keys()),
        format_func=lambda x: marketplace_options[x],
        help="选择要搜索的亚马逊站点",
    )

    # 搜索排序
    search_sort_options = {
        "relevance": "相关度（默认）",
        "sales-rank": "🔥 销量排序",
        "price-asc": "价格从低到高",
        "price-desc": "价格从高到低",
        "review-rank": "评论数排序",
        "date-desc": "最新上架",
        "avg-rating": "平均评分",
    }
    search_sort = st.selectbox(
        "搜索排序",
        options=list(search_sort_options.keys()),
        format_func=lambda x: search_sort_options[x],
        help="Amazon 搜索页上的排序方式",
    )

    # 价格区间
    price_col1, price_col2 = st.columns(2)
    with price_col1:
        min_price_input = st.text_input(
            "最低价格", value="", placeholder="例如: 10",
            help="筛选价格不低于此值的商品，留空不限制"
        )
        try:
            min_price = float(min_price_input) if min_price_input.strip() else None
        except ValueError:
            min_price = None
            if min_price_input.strip():
                st.sidebar.warning("最低价格格式无效，已忽略")
    with price_col2:
        max_price_input = st.text_input(
            "最高价格", value="", placeholder="例如: 100",
            help="筛选价格不高于此值的商品，留空不限制"
        )
        try:
            max_price = float(max_price_input) if max_price_input.strip() else None
        except ValueError:
            max_price = None
            if max_price_input.strip():
                st.sidebar.warning("最高价格格式无效，已忽略")

    st.markdown("---")
    st.markdown("### 📋 快捷关键词")
    preset_keywords = [
        "black stone grille", "mirror outlet cover plate", "window grille inserts",
        "trd grille badge", "billet grille 99-06 silverado", "universal front lip",
        "dog car seat cover", "car phone mount",
    ]
    selected_presets = st.multiselect("选择预设关键词", preset_keywords)
    if st.button("➕ 添加选中关键词", use_container_width=True):
        for kw in selected_presets:
            if kw not in st.session_state.keywords:
                st.session_state.keywords.append(kw)
        st.rerun()

    st.markdown("---")
    st.markdown("### 📂 已有数据")
    existing_kws = get_all_analyzed_keywords()
    if existing_kws:
        for ekw in existing_kws[:8]:
            st.markdown(f"- {ekw}")
    else:
        st.caption("暂无已分析数据")


# ═══════════════════════════════════════════════════════════════════
# 导航 Tabs
# ═══════════════════════════════════════════════════════════════════

tab_input, tab_url, tab_results, tab_history = st.tabs(["📝 输入关键词", "🔥 榜单URL", "📊 分析结果", "📂 历史报告"])


# ═══════════════════════════════════════════════════════════════════
# Tab 1: 输入关键词 + 运行
# ═══════════════════════════════════════════════════════════════════

with tab_input:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #232f3e, #ff9900); padding: 30px; border-radius: 16px; margin-bottom: 24px;">
        <h1 style="color: white; margin: 0; font-size: 28px;">🔍 Amazon 选品分析</h1>
        <p style="color: rgba(255,255,255,0.85); margin-top: 8px; font-size: 14px;">
            关键词搜索 → ASIN详情+评论 → 智能选品分析 — 每一步实时可见
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("### 📝 关键词管理")

    col_input1, col_add1 = st.columns([4, 1])
    with col_input1:
        new_keyword = st.text_input("输入关键词", placeholder="例如: black stone grille", label_visibility="collapsed")
    with col_add1:
        st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
        if st.button("➕ 添加", key="add_kw_btn", use_container_width=True):
            if new_keyword.strip() and new_keyword.strip() not in st.session_state.keywords:
                st.session_state.keywords.append(new_keyword.strip())
                st.rerun()

    with st.expander("📋 批量输入关键词（每行一个）"):
        batch_input = st.text_area("batch", placeholder="black stone grille\nmirror outlet cover plate", label_visibility="collapsed", height=120)
        if st.button("批量添加", key="batch_add_btn"):
            for line in batch_input.strip().split("\n"):
                kw = line.strip()
                if kw and kw not in st.session_state.keywords:
                    st.session_state.keywords.append(kw)
            st.rerun()

    if st.session_state.keywords:
        st.markdown("**当前关键词：**")
        cols = st.columns(4)
        for i, kw in enumerate(st.session_state.keywords):
            with cols[i % 4]:
                col_tag, col_del = st.columns([5, 1])
                with col_tag:
                    st.markdown(f'<span class="keyword-tag">{kw}</span>', unsafe_allow_html=True)
                with col_del:
                    if st.button("✕", key=f"del_{i}"):
                        st.session_state.keywords.remove(kw)
                        st.rerun()
        if st.button("🗑️ 清空所有关键词", key="clear_all_btn"):
            st.session_state.keywords = []
            st.rerun()
    else:
        st.info("请在上方输入关键词，或从左侧选择预设关键词")

    # 运行按钮
    st.markdown("---")
    col_run1, col_run2 = st.columns([3, 1])
    with col_run1:
        run_disabled = len(st.session_state.keywords) == 0 or st.session_state.running
        if st.button("🚀 开始选品分析", disabled=run_disabled, use_container_width=True, type="primary"):
            st.session_state.running = True
            st.session_state.log_lines = []
            st.session_state.process_returncode = None
            st.session_state.results = []
            st.rerun()
    with col_run2:
        if st.button("🔄 重置", use_container_width=True):
            st.session_state.running = False
            st.session_state.log_lines = []
            st.session_state.results = []
            st.session_state.process_returncode = None
            st.session_state.pipeline_log_lines = []
            st.rerun()

    # ── 执行 pipeline ──
    if st.session_state.running and st.session_state.keywords:
        st.markdown("---")
        st.markdown("### ⏳ 正在分析...")

        keywords_str = ",".join(st.session_state.keywords)

        # 初始化日志
        if "pipeline_log_lines" not in st.session_state:
            st.session_state.pipeline_log_lines = []
        log_lines = st.session_state.pipeline_log_lines

        def append_log(msg):
            ts = time.strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            log_lines.insert(0, entry)
            st.session_state.pipeline_log_lines = log_lines[:500]

        append_log(f"开始分析关键词: {keywords_str}")
        append_log(f"配置: 搜索页数={pages} | 搜索上限={max_search_items} | Top={top_n} | 评论={max_review_pages}页 | 站点={marketplace}")

        # 进度条 + 日志占位
        progress_bar = st.progress(0)
        log_placeholder = st.empty()

        def update_progress(pct, msg=""):
            progress_bar.progress(pct)
            if msg:
                append_log(msg)
            log_placeholder.code("\n".join(st.session_state.pipeline_log_lines[:200]), language=None)

        update_progress(0.05, f"执行命令: python product_analysis_pipeline.py ...")

        # 构造命令（所有参数从侧边栏直接取）
        cmd = [
            "C:\\Python312\\python.exe",
            str(PIPELINE_SCRIPT),
            "--keywords", keywords_str,
            "--pages", str(pages),
            "--top", str(top_n),
            "--max-search-items", str(max_search_items),
            "--max-review-pages", str(max_review_pages),
            "--sort-by", sort_by,
            "--marketplace", marketplace,
            "--search-sort", search_sort,
        ]
        if min_price is not None:
            cmd += ["--min-price", str(min_price)]
        if max_price is not None:
            cmd += ["--max-price", str(max_price)]
        if headful:
            cmd.append("--headful")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        update_progress(0.1, "Pipeline 进程已启动，等待输出...")

        # 实时读取 stdout
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                append_log(line)
                # 更新日志显示
                log_placeholder.code("\n".join(st.session_state.pipeline_log_lines[:200]), language=None)

        _, stderr_output = process.communicate()
        if stderr_output and stderr_output.strip():
            append_log(f"[stderr] {stderr_output.strip()[:500]}")

        returncode = process.returncode
        append_log(f"进程结束，返回码: {returncode}")
        log_placeholder.code("\n".join(st.session_state.pipeline_log_lines[:200]), language=None)
        progress_bar.progress(1.0)

        if returncode == 0:
            append_log("分析完成！正在加载结果...")
            results = []
            for kw in st.session_state.keywords:
                rdata = load_keyword_data(kw)
                if rdata:
                    results.append(rdata)
            st.session_state.results = results
            st.session_state.process_returncode = 0
            append_log(f"✅ 加载了 {len(results)} 个关键词的结果")
            append_log("👉 查看「分析结果」标签页")
        else:
            st.session_state.process_returncode = returncode
            append_log(f"❌ 分析失败，返回码: {returncode}，请检查日志")

        st.session_state.running = False
        # 留在当前页显示最终日志，不再 rerun


# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# Tab 2: 榜单 URL 分析（独立于关键词搜索）
# ═══════════════════════════════════════════════════════════════════

with tab_url:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #232f3e, #ff6600); padding: 30px; border-radius: 16px; margin-bottom: 24px;">
        <h1 style="color: white; margin: 0; font-size: 28px;">🔥 Amazon 榜单选品</h1>
        <p style="color: rgba(255,255,255,0.85); margin-top: 8px; font-size: 14px;">
            选择大类 + 榜单类型 → 自动获取 URL → 抓取商品数据 → 导出 Excel
        </p>
    </div>
    """, unsafe_allow_html=True)

    # ── 1. 加载类别数据 ──
    @st.cache_data
    def load_category_csv():
        csv_path = os.path.join(os.path.dirname(__file__), "类别url.csv")
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        return df

    cat_df = load_category_csv()

    # ── 2. 选择区域：左侧选择 + 右侧URL预览 ──
    st.markdown("### 📂 类别选择")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        # 站点
        site_options = sorted(cat_df["站点"].dropna().unique().tolist())
        selected_site = st.selectbox("🌐 选择站点", site_options, key="rank_site")

        site_df = cat_df[cat_df["站点"] == selected_site]

        # 榜单类型
        type_options = sorted(site_df["榜单类型"].dropna().unique().tolist())
        selected_type = st.selectbox("📊 榜单类型", type_options, key="rank_type")

        type_df = site_df[site_df["榜单类型"] == selected_type]

        # 大类
        big_options = sorted(type_df["大类名称"].dropna().unique().tolist())
        selected_big = st.selectbox("🏷️ 大类", big_options, key="rank_big")

        big_df = type_df[type_df["大类名称"] == selected_big]

        # 分类层级（总入口 / 大类 / 小类）
        level_options = sorted(big_df["分类层级"].dropna().unique().tolist())
        selected_level = st.selectbox("📁 分类层级", level_options, key="rank_level")

        level_df = big_df[big_df["分类层级"] == selected_level]

        # 小类
        if selected_level == "小类":
            sub_options = sorted(level_df["分类名称"].dropna().unique().tolist())
            selected_sub = st.selectbox("🔸 小类", sub_options, key="rank_sub")
            level_df = level_df[level_df["分类名称"] == selected_sub]

    with col_right:
        st.markdown("**🔗 对应 URL**")
        if len(level_df) > 0:
            row = level_df.iloc[0]
            url_type_icon = "🔥" if selected_type == "热销榜" else "📈"
            st.code(row["URL"], language=None)
            selected_url = row["URL"]
            col_ic, col_lb = st.columns([1, 4])
            with col_ic:
                st.markdown(f"{url_type_icon} **{selected_type}**")
            with col_lb:
                st.caption(f"站点：{selected_site} | 大类：{selected_big} | {selected_level}：{row['分类名称']}")
        else:
            st.info("请先选择站点、大类和分类层级")
            selected_url = None

    # ── 3. 抓取参数 ──
    st.markdown("### ⚙️ 抓取参数")
    param_col1, param_col2, param_col3, param_col4 = st.columns(4)
    with param_col1:
        max_products = st.number_input(
            "商品上限", min_value=0, max_value=500, value=30,
            help="最多抓取多少个商品（0=不限）", key="rank_max_products"
        )
    with param_col2:
        min_price = st.number_input(
            "最低价 ($)", min_value=0.0, max_value=9999.0, value=0.0,
            step=1.0, key="rank_min_price"
        )
    with param_col3:
        max_price = st.number_input(
            "最高价 ($)", min_value=0.0, max_value=9999.0, value=9999.0,
            step=1.0, key="rank_max_price"
        )
    with param_col4:
        min_rating = st.number_input(
            "最低评分", min_value=0.0, max_value=5.0, value=0.0,
            step=0.1, key="rank_min_rating"
        )

    show_browser = st.checkbox("🖥️ 显示浏览器（推荐勾选）", value=True, key="rank_show_browser")

    # ── 4. 执行按钮 ──
    st.markdown("---")
    col_exec, col_reset, col_dl = st.columns([2, 1, 2])

    with col_exec:
        run_clicked = st.button(
            "🚀 开始抓取",
            disabled=not selected_url or st.session_state.get("rank_running", False),
            use_container_width=True,
            type="primary",
            key="rank_run_btn",
        )

    with col_reset:
        if st.button("🔄 重置", use_container_width=True, key="rank_reset_btn"):
            for k in list(st.session_state.keys()):
                if k.startswith("rank_"):
                    del st.session_state[k]
            st.session_state.rank_results = None
            st.session_state.rank_running = False
            st.rerun()

    # ── 5. 执行爬虫逻辑 ──
    if run_clicked and selected_url:
        st.session_state.rank_running = True
        st.session_state.rank_results = None
        st.rerun()

    if st.session_state.get("rank_running") and selected_url:
        st.markdown("---")
        st.markdown("### ⏳ 正在抓取商品数据...")

        # 日志区域
        log_area = st.empty()
        progress_bar = st.progress(0)
        rows = []
        browser = None
        p = None
        error_msg = None

        def log(msg: str, pct: int = None):
            ts = time.strftime("%H:%M:%S")
            if "rank_logs" not in st.session_state:
                st.session_state.rank_logs = []
            st.session_state.rank_logs.append(f"[{ts}] {msg}")
            log_area.text("\n".join(st.session_state.rank_logs[-50:]))
            if pct is not None:
                progress_bar.progress(pct)

        try:
            from playwright.sync_api import sync_playwright

            log("初始化 Playwright...", 5)
            p = sync_playwright().start()
            log("启动 Chromium 浏览器...", 10)

            browser = p.chromium.launch(
                headless=not show_browser,
                args=["--disable-blink-features=AutomationControlled"]
            )
            log("浏览器已就绪，准备访问页面...", 20)

            log(f"访问: {selected_url[:80]}...", 30)
            rank_rows = scrape_by_url(browser, selected_url, pages=1, max_items=max_products)
            log(f"榜单页面加载完成，抓取到 {len(rank_rows)} 个商品", 50)

            log("正在过滤（价格 / 评分）...", 60)
            for r in rank_rows:
                row = r.to_dict()
                price = row.get("price") or 0
                if price > 0 and (price < min_price or price > max_price):
                    continue
                rating = row.get("rating") or 0
                if rating > 0 and rating < min_rating:
                    continue
                rows.append(row)
            log(f"过滤完成，保留 {len(rows)} 个商品，正在排序...", 75)

            rows = sort_rows(rows, sort_by="sales_proxy")
            if max_products > 0:
                rows = rows[:max_products]
            log(f"排序完成，最终保留 {len(rows)} 个商品", 85)

            browser.close()
            p.stop()
            log("浏览器已关闭，数据处理完毕", 100)

            time.sleep(0.5)
            df_result = pd.DataFrame(rows) if rows else pd.DataFrame()
            st.session_state.rank_results = {
                "url": selected_url,
                "type": selected_type,
                "category": selected_type,
                "rows": rows,
                "df": df_result,
            }
            st.session_state.rank_running = False
            st.session_state.rank_logs = []
            st.rerun()

        except Exception as e:
            import traceback
            error_msg = traceback.format_exc()
            log(f"错误: {e}", 0)
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if p:
                    p.stop()
            except Exception:
                pass
            st.session_state.rank_running = False

        if error_msg:
            with st.expander("查看完整错误信息"):
                st.code(error_msg[-2000:])
    result_data = st.session_state.get("rank_results")
    if result_data:
        st.markdown("---")
        st.markdown("### 📊 抓取结果")

        rows = result_data.get("rows", [])
        df = result_data.get("df")

        # 统计
        if rows:
            prices = [r.get("price") for r in rows if r.get("price")]
            ratings = [r.get("rating") for r in rows if r.get("rating")]

            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("商品数量", len(rows))
            with m2:
                st.metric("价格区间", f"${min(prices):.2f}-{max(prices):.2f}" if prices else "N/A")
            with m3:
                st.metric("平均评分", f"{sum(ratings)/len(ratings):.2f}" if ratings else "N/A")

        # 下载按钮
        col_dl1, col_dl2 = st.columns([1, 1])
        with col_dl1:
            if df is not None and len(df) > 0:
                csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "📥 下载 CSV",
                    data=csv_bytes,
                    file_name=f"榜单_{result_data['category']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                    type="primary",
                )
        with col_dl2:
            if df is not None and len(df) > 0:
                import io
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False, sheet_name="榜单数据")
                output.seek(0)
                st.download_button(
                    "📥 下载 Excel",
                    data=output,
                    file_name=f"榜单_{result_data['category']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )

        # 表格展示
        if df is not None and len(df) > 0:
            show_cols = [c for c in [
                "position", "asin", "title", "brand", "price",
                "rating", "review_count", "bought_count_est"
            ] if c in df.columns]

            show_df = df[show_cols].copy()

            # 格式化
            if "title" in show_df.columns:
                show_df["title"] = show_df["title"].apply(
                    lambda x: str(x)[:70] + "..." if len(str(x)) > 70 else x
                )
            if "price" in show_df.columns:
                show_df["price"] = show_df["price"].apply(
                    lambda x: f"${x:.2f}" if pd.notna(x) and x > 0 else "-"
                )
            if "rating" in show_df.columns:
                show_df["rating"] = show_df["rating"].apply(
                    lambda x: f"{x:.1f}" if pd.notna(x) and x > 0 else "-"
                )
            if "review_count" in show_df.columns:
                show_df["review_count"] = show_df["review_count"].apply(
                    lambda x: f"{x:,}" if pd.notna(x) else "-"
                )
            if "bought_count_est" in show_df.columns:
                show_df["bought_count_est"] = show_df["bought_count_est"].apply(
                    lambda x: f"{x:,}" if pd.notna(x) else "-"
                )

            st.dataframe(show_df, use_container_width=True, hide_index=True, height=600)
            st.caption(f"共 {len(df)} 个商品，已按销量排序")
        else:
            st.info("未抓取到商品数据")



with tab_results:
    results = st.session_state.results

    # 如果没有结果，尝试加载已有数据
    if not results and not st.session_state.running:
        existing_kws = get_all_analyzed_keywords()
        if existing_kws:
            st.markdown("### 📊 加载已有分析数据")
            selected_load = st.multiselect("选择要查看的关键词", existing_kws, default=existing_kws[:5])
            if selected_load:
                results = []
                for kw in selected_load:
                    rdata = load_keyword_data(kw)
                    if rdata:
                        results.append(rdata)
                st.session_state.results = results

    if results:
        # ── 总览卡片 ──
        st.markdown("### 📊 评分总览")
        cols = st.columns(min(len(results), 5))
        for i, res in enumerate(results):
            with cols[i % len(cols)]:
                kw = res.get("keyword", "?")
                summary = res.get("summary", {})
                if "error" in summary:
                    score, level, color = 0, "失败", "#dc3545"
                else:
                    rec = summary.get("recommendation", {})
                    score = rec.get("score", 0)
                    level = rec.get("level", "未知")
                    color = {"推荐": "#28a745", "适中": "#ffc107", "观望": "#dc3545", "避开": "#6c757d"}.get(level, "#6c757d")
                st.markdown(f"""
                <div class="score-card">
                    <div style="font-size:13px; color:#666; margin-bottom:4px;">{kw}</div>
                    <div class="score-big" style="color:{color};">{score}</div>
                    <div style="font-size:14px; font-weight:600; color:{color};">{level}</div>
                </div>
                """, unsafe_allow_html=True)

        # ── 下载按钮区 ──
        st.markdown("---")
        dl_col1, dl_col2 = st.columns([1, 1])
        with dl_col1:
            # 生成 HTML 报告
            html_report = generate_standalone_html_report(results)
            st.download_button(
                "📥 下载 HTML 报告",
                data=html_report,
                file_name=f"选品分析报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
                mime="text/html",
                use_container_width=True,
                type="primary",
            )
        with dl_col2:
            # 下载 CSV 数据
            csv_data = ""
            for res in results:
                kw = res.get("keyword", "?")
                search_df = res.get("search_df")
                if search_df is not None and len(search_df) > 0:
                    csv_data += f"\n\n=== {kw} 搜索结果 ===\n"
                    csv_data += search_df.to_csv(index=False)
            if csv_data:
                st.download_button(
                    "📥 下载 CSV 数据",
                    data=csv_data,
                    file_name=f"选品数据_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

        # ── 每个关键词详细分析 ──
        st.markdown("---")
        st.markdown("### 📋 关键词详细分析")

        for res in results:
            kw = res.get("keyword", "?")
            summary = res.get("summary", {})

            if "error" in summary:
                st.error(f"❌ **{kw}** 搜索失败: {summary['error']}")
                continue

            rec = summary.get("recommendation", {})
            score = rec.get("score", 0)
            level = rec.get("level", "未知")
            icon = "✅" if score >= 70 else "⚠️" if score >= 50 else "⛔"

            with st.expander(f"{icon} {kw} — {score}分 ({level})", expanded=(score >= 50)):
                # 机会与风险
                col_opp, col_risk = st.columns(2)
                with col_opp:
                    st.markdown("**✅ 机会**")
                    for o in rec.get("opportunities", []):
                        st.markdown(f'<div class="opp-card">{o}</div>', unsafe_allow_html=True)
                    if not rec.get("opportunities"):
                        st.markdown("暂无")

                with col_risk:
                    st.markdown("**⚠️ 风险**")
                    for r in rec.get("risks", []):
                        st.markdown(f'<div class="pain-card">{r}</div>', unsafe_allow_html=True)
                    if not rec.get("risks"):
                        st.markdown("暂无")

                # 搜索分析
                sa = summary.get("search_analysis", {})
                if sa:
                    st.markdown("#### 📊 搜索结果分析")
                    c1, c2, c3, c4 = st.columns(4)
                    with c1: st.metric("价格区间", sa.get("price_range", "N/A"))
                    with c2: st.metric("均价", sa.get("avg_price", "N/A"))
                    with c3: st.metric("平均评分", sa.get("avg_rating", "N/A"))
                    with c4: st.metric("近期购买量", sa.get("total_bought", 0))

                    c5, c6, c7, c8 = st.columns(4)
                    with c5: st.metric("平均评论数", sa.get("avg_reviews", "N/A"))
                    with c6: st.metric("中位价", sa.get("median_price", "N/A"))
                    with c7: st.metric("广告占比", sa.get("sponsored_ratio", "N/A"))
                    with c8:
                        decisions = sa.get("decisions", {})
                        st.metric("筛选结果", f"推{decisions.get('recommend',0)}/关{decisions.get('watch',0)}/排{decisions.get('skip',0)}")

                    brands = sa.get("brands", {})
                    if brands:
                        st.markdown("**品牌分布（Top 5）**")
                        brand_text = " ".join([f"`{b}` ({c})" for b, c in list(brands.items())[:5]])
                        st.markdown(brand_text)

                # 详情分析
                da = summary.get("detail_analysis", {})
                if da:
                    st.markdown("#### 🔍 详情分析")
                    c1, c2, c3, c4 = st.columns(4)
                    with c1: st.metric("抓取成功率", da.get("success_rate", "N/A"))
                    with c2: st.metric("详情均价", da.get("avg_price", "N/A"))
                    with c3: st.metric("Prime比例", da.get("prime_ratio", "N/A"))
                    with c4: st.metric("Amazon's Choice", da.get("amazon_choice_count", 0))

                # 评论分析
                ra = summary.get("review_analysis", {})
                if ra:
                    st.markdown("#### 💬 评论分析")
                    c1, c2, c3 = st.columns(3)
                    with c1: st.metric("评论总数", ra.get("total_reviews", 0))
                    with c2: st.metric("平均评分", ra.get("avg_rating", "N/A"))
                    with c3: st.metric("验证购买比", ra.get("verified_ratio", "N/A"))

                    # 评分分布
                    rating_dist = ra.get("rating_distribution", {})
                    if rating_dist:
                        st.markdown("**评分分布**")
                        for star in [5, 4, 3, 2, 1]:
                            count = int(rating_dist.get(str(star), 0))
                            total_r = sum(int(v) for v in rating_dist.values()) or 1
                            pct = count / total_r * 100
                            st.progress(pct / 100, text=f"{'⭐' * star} {count} ({pct:.0f}%)")

                    # 高频词
                    top_words = ra.get("top_words", {})
                    if top_words:
                        st.markdown("**评论高频词**")
                        word_cols = st.columns(8)
                        for j, (w, c) in enumerate(list(top_words.items())[:16]):
                            with word_cols[j % 8]:
                                badge = "🔥" if c > 5 else "📌"
                                st.markdown(f"{badge} **{w}** ({c})")

                    # 痛点
                    pain_points = ra.get("pain_points", [])
                    if pain_points:
                        st.markdown("**⚠️ 差评痛点**")
                        for pp in pain_points[:5]:
                            st.markdown(f'<div class="pain-card"><strong>⭐{pp["rating"]} {pp["title"]}</strong><br><span style="color:#555;font-size:13px">{pp["body"][:200]}</span></div>', unsafe_allow_html=True)

                # ── 数据表格 ──
                st.markdown("---")

                search_df = res.get("search_df")
                details_df = res.get("details_df")
                reviews_df = res.get("reviews_df")

                data_tabs = st.tabs(["📋 搜索结果", "🔍 详情", "💬 评论"])

                with data_tabs[0]:
                    if search_df is not None and len(search_df) > 0:
                        show_cols = [c for c in ["asin", "title", "brand", "price", "rating", "review_count", "bought_count_est", "decision", "decision_reason"] if c in search_df.columns]
                        show_df = search_df[show_cols].copy()
                        if "title" in show_df.columns:
                            show_df["title"] = show_df["title"].apply(lambda x: str(x)[:60] + "..." if len(str(x)) > 60 else x)
                        if "price" in show_df.columns:
                            show_df["price"] = show_df["price"].apply(lambda x: f"${x}" if pd.notna(x) else "-")
                        if "decision" in show_df.columns:
                            show_df["decision"] = show_df["decision"].apply(
                                lambda x: {"recommend": "✅推荐", "watch": "⚠️关注", "skip": "⛔排除"}.get(str(x), str(x))
                            )
                        if "asin" in show_df.columns:
                            show_df["link"] = show_df["asin"].apply(lambda x: f"https://www.amazon.com/dp/{x}")
                        st.dataframe(show_df, use_container_width=True, hide_index=True)
                    else:
                        st.info("无搜索结果数据")

                with data_tabs[1]:
                    if details_df is not None and len(details_df) > 0:
                        det_list = []
                        for _, row in details_df.iterrows():
                            det_list.append({
                                "ASIN": row.get("asin", ""),
                                "状态": "✅" if row.get("scrape_status") == "success" else "❌",
                                "标题": str(row.get("title", ""))[:60],
                                "品牌": row.get("brand", ""),
                                "价格": f"${row.get('price', '-')}" if pd.notna(row.get("price")) else "-",
                                "评分": row.get("rating", "-"),
                                "评论数": row.get("review_count", "-"),
                                "Prime": "✅" if row.get("is_prime") else "-",
                                "A's Choice": "✅" if row.get("is_amazon_choice") else "-",
                                "Best Seller": "✅" if row.get("is_best_seller") else "-",
                                "类目": str(row.get("category_path", ""))[:50],
                            })
                        st.dataframe(pd.DataFrame(det_list), use_container_width=True, hide_index=True)
                    else:
                        st.info("无详情数据")

                with data_tabs[2]:
                    if reviews_df is not None and len(reviews_df) > 0:
                        rev_list = []
                        for _, row in reviews_df.head(50).iterrows():
                            rev_list.append({
                                "ASIN": row.get("asin", ""),
                                "评分": f"⭐{row.get('rating', '-')}",
                                "标题": str(row.get("title", ""))[:50],
                                "作者": row.get("author", ""),
                                "日期": str(row.get("date", ""))[:20],
                                "验证": "✅" if row.get("verified_purchase") else "-",
                                "有用数": row.get("helpful_count", ""),
                                "内容": str(row.get("body", ""))[:150],
                            })
                        st.dataframe(pd.DataFrame(rev_list), use_container_width=True, hide_index=True)
                    else:
                        st.info("无评论数据")

    elif not st.session_state.running:
        st.info("👈 请先在「输入关键词」标签中添加关键词并运行分析")


# ═══════════════════════════════════════════════════════════════════
# Tab 3: 历史报告
# ═══════════════════════════════════════════════════════════════════

with tab_history:
    all_reports = sorted(glob.glob(str(OUTPUTS_DIR / "pipeline_report_*.html")), reverse=True)
    if all_reports:
        st.markdown("### 📂 历史报告")
        for rp in all_reports[:20]:
            name = Path(rp).name
            size = Path(rp).stat().st_size
            size_str = f"{size / 1024:.1f}KB"
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"📄 **{name}** ({size_str})")
            with col2:
                with open(rp, "r", encoding="utf-8") as f:
                    html_content = f.read()
                st.download_button(
                    "📥 下载", data=html_content, file_name=name, mime="text/html",
                    key=f"dl_{name}", use_container_width=True,
                )
    else:
        st.info("暂无历史报告")


# ═══════════════════════════════════════════════════════════════════
# 页脚
# ═══════════════════════════════════════════════════════════════════

st.markdown("---")
st.markdown(
    "<div style='text-align:center;color:#999;font-size:12px;'>"
    "Amazon 选品分析工具 | Powered by Playwright + Streamlit"
    "</div>", unsafe_allow_html=True,
)
