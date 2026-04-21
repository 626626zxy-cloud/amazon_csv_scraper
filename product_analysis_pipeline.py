#!/usr/bin/env python3
"""
Amazon 一键选品分析 Pipeline
==============================
流程：关键词搜索 → ASIN详情+评论抓取 → 选品分析报告(HTML)

用法：
    python product_analysis_pipeline.py --keywords "black stone grille" "mirror outlet cover plate"
    python product_analysis_pipeline.py --keywords-file keywords.txt
    python product_analysis_pipeline.py --keywords "front lip" --top 5 --pages 2
    python product_analysis_pipeline.py --keywords "front lip" --headful
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from playwright.sync_api import Browser, sync_playwright

# 导入已有模块
from amazon_scraper import AmazonPageError, MARKETPLACES, scrape_keyword
from amazon_rankings import scrape_by_url, classify_url
from asin_detail_scraper import (
    ProductDetail,
    ProductReview,
    extract_reviews_from_detail_page,
    scrape_product_detail,
    scrape_product_reviews,
)
from csv_exporter import export_rows
from ranking import add_ranking_fields, sort_rows
from rules_engine import DEFAULT_RULES_PATH, annotate_products, ensure_rules_workbook, load_rules


OUTPUT_DIR = Path("outputs")


# ═══════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════

@dataclass
class KeywordAnalysisResult:
    """单个关键词的分析结果"""
    keyword: str
    search_products: List[dict]          # 搜索结果（含规则筛选）
    details: List[dict]                  # ASIN详情
    reviews: List[dict]                  # 评论
    summary: Dict[str, Any] = None       # 分析摘要

    def __post_init__(self):
        if self.summary is None:
            self.summary = {}


# ═══════════════════════════════════════════════════════════════════
# Pipeline 主流程
# ═══════════════════════════════════════════════════════════════════

def run_pipeline(
    keywords: List[str],
    pages: int = 1,
    top_n: int = 10,
    max_review_pages: int = 3,
    headless: bool = True,
    skip_reviews: bool = False,
    rules_path: str = None,
    sort_by: str = "sales_proxy",
    progress_callback=None,
    marketplace: str = "US",
    search_sort: str = "relevance",
    min_price: float | None = None,
    max_price: float | None = None,
    max_search_items: int | None = None,
) -> List[KeywordAnalysisResult]:
    """
    完整选品分析 Pipeline

    Args:
        keywords: 关键词列表
        pages: 每个关键词抓取的搜索页数
        top_n: 每个关键词取搜索结果前N个进入详情+评论抓取
        max_review_pages: 每个商品最多抓取评论页数
        headless: 是否无头模式
        skip_reviews: 是否跳过评论抓取
        rules_path: 规则文件路径
        sort_by: 排序方式
        progress_callback: 进度回调
        max_search_items: 搜索时最多抓取商品数（None=不限制）

    Returns:
        List[KeywordAnalysisResult]
    """
    results: List[KeywordAnalysisResult] = []

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)
        print(msg)

    # 加载规则
    if rules_path is None:
        rules_path = str(DEFAULT_RULES_PATH)
    rules_path = ensure_rules_workbook(rules_path)
    rules = load_rules(rules_path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            for kw_idx, keyword in enumerate(keywords, 1):
                _log(f"\n{'='*60}")
                _log(f"[{kw_idx}/{len(keywords)}] 关键词: {keyword}")
                _log(f"{'='*60}")

                ka = KeywordAnalysisResult(
                    keyword=keyword,
                    search_products=[],
                    details=[],
                    reviews=[],
                )

                # ── Step 1: 关键词搜索 ──
                _log(f"Step 1: 搜索关键词 '{keyword}' ...")
                _log(f"  站点: {marketplace} ({MARKETPLACES.get(marketplace, MARKETPLACES['US'])['domain']}) | 排序: {search_sort}")
                if min_price is not None or max_price is not None:
                    _log(f"  价格区间: ${min_price or 0} - ${max_price or '∞'}")
                if max_search_items is not None:
                    _log(f"  搜索上限: {max_search_items} 条")
                try:
                    products = scrape_keyword(
                        browser, keyword, pages=pages,
                        max_items=max_search_items,
                        marketplace=marketplace,
                        sort_by=search_sort,
                        min_price=min_price,
                        max_price=max_price,
                    )
                    rows = [p.to_dict() for p in products]
                    _log(f"  搜索到 {len(rows)} 个商品")
                except AmazonPageError as e:
                    _log(f"  搜索失败: {e}")
                    ka.summary = {"error": str(e)}
                    results.append(ka)
                    continue

                # 规则筛选 + 排名
                screened_rows = annotate_products(rows, rules)
                screened_rows = add_ranking_fields(screened_rows)
                screened_rows = sort_rows(screened_rows, sort_by)

                # 取 Top N
                if top_n and top_n > 0:
                    screened_rows = screened_rows[:top_n]

                ka.search_products = screened_rows
                _log(f"  筛选后保留 {len(screened_rows)} 个商品（Top {top_n}）")

                # 提取 ASIN 列表
                asins = [r["asin"] for r in screened_rows if r.get("asin")]
                _log(f"  ASIN 列表: {asins}")

                # ── Step 2: ASIN 详情 + 评论抓取 ──
                all_details: List[ProductDetail] = []
                all_reviews: List[ProductReview] = []

                for i, asin in enumerate(asins, 1):
                    _log(f"  [{i}/{len(asins)}] 抓取 ASIN: {asin}")

                    # 详情页
                    try:
                        detail, detail_soup = scrape_product_detail(page, asin)
                        all_details.append(detail)
                        _log(f"    详情: {detail.title[:50]}... | ${detail.price} | *{detail.rating}")

                        # 从详情页底部提取评论
                        if not skip_reviews and detail_soup:
                            detail_reviews = extract_reviews_from_detail_page(detail_soup, asin)
                            if detail_reviews:
                                all_reviews.extend(detail_reviews)
                                _log(f"    详情页评论: {len(detail_reviews)} 条")
                    except Exception as e:
                        _log(f"    详情失败: {e}")
                        all_details.append(ProductDetail(
                            asin=asin,
                            product_url=f"https://www.amazon.com/dp/{asin}",
                            scrape_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            scrape_status="failed",
                        ))

                    _sleep_jitter(2, 4)

                    # 更多评论
                    if not skip_reviews and max_review_pages > 0:
                        try:
                            reviews = scrape_product_reviews(page, asin, max_review_pages)
                            existing_ids = {r.review_id for r in all_reviews if r.review_id}
                            new_reviews = [r for r in reviews if r.review_id not in existing_ids]
                            all_reviews.extend(new_reviews)
                            _log(f"    评论页: {len(reviews)} 条，新增 {len(new_reviews)} 条")
                        except Exception as e:
                            _log(f"    评论失败: {e}")

                        _sleep_jitter(2, 4)

                ka.details = [d.to_dict() for d in all_details]
                ka.reviews = [r.to_dict() for r in all_reviews]

                # ── Step 3: 数据分析 ──
                _log(f"Step 3: 数据分析 ...")
                ka.summary = analyze_keyword(ka)
                _log(f"  分析完成")

                results.append(ka)

                # 保存中间结果
                _save_keyword_results(ka, keyword)

        finally:
            page.close()
            context.close()
            browser.close()

    return results


def run_url_pipeline(
    urls: List[str],
    pages: int = 1,
    top_n: int = 10,
    max_review_pages: int = 3,
    headless: bool = True,
    skip_reviews: bool = False,
    rules_path: str = None,
    sort_by: str = "sales_proxy",
    max_items: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    progress_callback=None,
) -> List[KeywordAnalysisResult]:
    """
    URL 模式选品分析 Pipeline

    用户直接输入 Amazon Best Sellers / Movers & Shakers / 搜索结果页 URL，
    自动抓取商品 → 规则筛选 → 详情 + 评论 → 分析报告

    Args:
        urls: Amazon URL 列表
        pages: 每个 URL 再多抓几页（默认只抓当前页）
        top_n: 每组取前 N 个进入详情+评论抓取
        max_review_pages: 每个商品最多抓评论页数
        headless: 是否无头模式
        skip_reviews: 是否跳过评论
        rules_path: 规则文件路径
        sort_by: 排序方式 (sales_proxy | review_count | price_low | price_high)
        max_items: 每组最多抓取商品数
        min_price: 最低价格过滤（美元）
        max_price: 最高价格过滤（美元）
        progress_callback: 进度回调

    Returns:
        List[KeywordAnalysisResult]
    """
    results: List[KeywordAnalysisResult] = []

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)
        print(msg)

    # 加载规则
    if rules_path is None:
        rules_path = str(DEFAULT_RULES_PATH)
    rules_path = ensure_rules_workbook(rules_path)
    rules = load_rules(rules_path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        page = context.new_page()
        page.set_default_timeout(30_000)

        try:
            for url_idx, url in enumerate(urls, 1):
                page_type = classify_url(url)
                label = {
                    "best_sellers": "[Best-Sellers]",
                    "movers_shakers": "[Movers-Shakers]",
                    "search": "[Search]",
                    "unknown": "[Page]",
                }.get(page_type, "[Page]")

                _log(f"\n{'='*60}")
                _log(f"[{url_idx}/{len(urls)}] {label} URL: {url[:80]}...")
                _log(f"{'='*60}")

                ka = KeywordAnalysisResult(
                    keyword=f"{label} {url}",
                    search_products=[],
                    details=[],
                    reviews=[],
                )

                # ── Step 1: URL 抓取商品 ──
                _log(f"Step 1: 从 {label} 抓取商品 ...")
                try:
                    products = scrape_by_url(
                        browser, url,
                        pages=pages,
                        max_items=max_items,
                    )
                    rows = [p.to_dict() for p in products]
                    _log(f"  抓取到 {len(rows)} 个商品")
                except Exception as e:
                    _log(f"  抓取失败: {e}")
                    ka.summary = {"error": str(e)}
                    results.append(ka)
                    continue

                # 规则筛选 + 排名 + 价格过滤 + 排序
                screened_rows = annotate_products(rows, rules)
                screened_rows = add_ranking_fields(screened_rows)
                screened_rows = sort_rows(
                    screened_rows,
                    sort_by,
                    min_price=min_price,
                    max_price=max_price,
                )

                # 取 Top N
                if top_n and top_n > 0:
                    screened_rows = screened_rows[:top_n]

                ka.search_products = screened_rows
                _log(f"  筛选后保留 {len(screened_rows)} 个商品（Top {top_n}，价格区间 ${min_price or 0}-{max_price or '∞'}）")

                # 提取 ASIN 列表
                asins = [r["asin"] for r in screened_rows if r.get("asin")]
                _log(f"  ASIN 列表: {asins}")

                # ── Step 2: ASIN 详情 + 评论抓取 ──
                all_details: List[ProductDetail] = []
                all_reviews: List[ProductReview] = []

                for i, asin in enumerate(asins, 1):
                    _log(f"  [{i}/{len(asins)}] 抓取 ASIN: {asin}")

                    # 详情页
                    try:
                        detail, detail_soup = scrape_product_detail(page, asin)
                        all_details.append(detail)
                        _log(f"    详情: {detail.title[:50]}... | ${detail.price} | *{detail.rating}")

                        # 从详情页底部提取评论
                        if not skip_reviews and detail_soup:
                            detail_reviews = extract_reviews_from_detail_page(detail_soup, asin)
                            if detail_reviews:
                                all_reviews.extend(detail_reviews)
                                _log(f"    详情页评论: {len(detail_reviews)} 条")
                    except Exception as e:
                        _log(f"    详情失败: {e}")
                        all_details.append(ProductDetail(
                            asin=asin,
                            product_url=f"https://www.amazon.com/dp/{asin}",
                            scrape_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            scrape_status="failed",
                        ))

                    _sleep_jitter(2, 4)

                    # 更多评论
                    if not skip_reviews and max_review_pages > 0:
                        try:
                            reviews = scrape_product_reviews(page, asin, max_review_pages)
                            existing_ids = {r.review_id for r in all_reviews if r.review_id}
                            new_reviews = [r for r in reviews if r.review_id not in existing_ids]
                            all_reviews.extend(new_reviews)
                            _log(f"    评论页: {len(reviews)} 条，新增 {len(new_reviews)} 条")
                        except Exception as e:
                            _log(f"    评论失败: {e}")

                        _sleep_jitter(2, 4)

                ka.details = [d.to_dict() for d in all_details]
                ka.reviews = [r.to_dict() for r in all_reviews]

                # ── Step 3: 数据分析 ──
                _log(f"Step 3: 数据分析 ...")
                ka.summary = analyze_keyword(ka)
                _log(f"  分析完成")

                results.append(ka)

                # 保存中间结果
                _save_keyword_results(ka, f"url_{url_idx}")

        finally:
            page.close()
            context.close()
            browser.close()

    return results

def analyze_keyword(ka: KeywordAnalysisResult) -> Dict[str, Any]:
    """对单个关键词的数据进行选品分析"""
    summary: Dict[str, Any] = {
        "keyword": ka.keyword,
        "search_count": len(ka.search_products),
        "detail_count": len(ka.details),
        "review_count": len(ka.reviews),
    }

    # ── 搜索结果分析 ──
    if ka.search_products:
        prices = [r.get("price") for r in ka.search_products if r.get("price")]
        ratings = [r.get("rating") for r in ka.search_products if r.get("rating")]
        review_counts = [r.get("review_count") for r in ka.search_products if r.get("review_count")]
        bought_counts = [r.get("bought_count_est", 0) for r in ka.search_products]
        sponsored_count = sum(1 for r in ka.search_products if r.get("is_sponsored"))

        summary["search_analysis"] = {
            "price_range": f"${min(prices):.2f} - ${max(prices):.2f}" if prices else "N/A",
            "avg_price": f"${sum(prices)/len(prices):.2f}" if prices else "N/A",
            "median_price": f"${sorted(prices)[len(prices)//2]:.2f}" if prices else "N/A",
            "avg_rating": f"{sum(ratings)/len(ratings):.1f}" if ratings else "N/A",
            "avg_reviews": f"{sum(review_counts)/len(review_counts):.0f}" if review_counts else "N/A",
            "max_reviews": max(review_counts) if review_counts else 0,
            "total_bought": sum(bought_counts),
            "sponsored_ratio": f"{sponsored_count}/{len(ka.search_products)}",
            "brands": _top_brands(ka.search_products, 5),
            "decisions": _count_decisions(ka.search_products),
        }

    # ── 详情分析 ──
    if ka.details:
        successful = [d for d in ka.details if d.get("scrape_status") == "success"]
        if successful:
            det_prices = [d.get("price") for d in successful if d.get("price")]
            det_ratings = [d.get("rating") for d in successful if d.get("rating")]
            prime_count = sum(1 for d in successful if d.get("is_prime"))
            ac_count = sum(1 for d in successful if d.get("is_amazon_choice"))
            bs_count = sum(1 for d in successful if d.get("is_best_seller"))

            # 类目分布
            categories = Counter()
            for d in successful:
                cat = d.get("category_path", "")
                if cat:
                    top_cat = cat.split(" > ")[0] if " > " in cat else cat
                    categories[top_cat] += 1

            summary["detail_analysis"] = {
                "success_rate": f"{len(successful)}/{len(ka.details)}",
                "price_range": f"${min(det_prices):.2f} - ${max(det_prices):.2f}" if det_prices else "N/A",
                "avg_price": f"${sum(det_prices)/len(det_prices):.2f}" if det_prices else "N/A",
                "avg_rating": f"{sum(det_ratings)/len(det_ratings):.1f}" if det_ratings else "N/A",
                "prime_ratio": f"{prime_count}/{len(successful)}",
                "amazon_choice_count": ac_count,
                "best_seller_count": bs_count,
                "top_categories": dict(categories.most_common(5)),
            }

    # ── 评论分析 ──
    if ka.reviews:
        review_ratings = [r.get("rating") for r in ka.reviews if r.get("rating")]
        verified = sum(1 for r in ka.reviews if r.get("verified_purchase"))
        rating_dist = Counter()
        for r in review_ratings:
            if r:
                bucket = int(r)
                rating_dist[bucket] = rating_dist.get(bucket, 0) + 1

        # 关键词提取（评论高频词）
        word_freq = Counter()
        stop_words = {"the", "a", "an", "is", "it", "i", "this", "to", "and", "of", "for",
                      "in", "on", "with", "was", "but", "my", "that", "not", "very", "so",
                      "as", "at", "be", "had", "have", "has", "its", "just", "me", "or",
                      "than", "them", "they", "we", "you", "all", "are", "do", "from", "get",
                      "got", "if", "no", "out", "up", "what", "when", "which", "will", "would"}
        for r in ka.reviews:
            body = (r.get("body") or "") + " " + (r.get("title") or "")
            words = re.findall(r"[a-zA-Z]{3,}", body.lower())
            for w in words:
                if w not in stop_words:
                    word_freq[w] += 1

        # 负面评论痛点分析
        pain_points = []
        for r in ka.reviews:
            if r.get("rating") and r["rating"] <= 2:
                body = (r.get("body") or "").strip()
                if body:
                    pain_points.append({
                        "rating": r["rating"],
                        "title": r.get("title", ""),
                        "body": body[:300],
                    })

        summary["review_analysis"] = {
            "total_reviews": len(ka.reviews),
            "avg_rating": f"{sum(review_ratings)/len(review_ratings):.1f}" if review_ratings else "N/A",
            "verified_ratio": f"{verified}/{len(ka.reviews)}",
            "rating_distribution": dict(sorted(rating_dist.items())),
            "top_words": dict(word_freq.most_common(20)),
            "pain_points": pain_points[:10],
        }

    # ── 选品建议 ──
    summary["recommendation"] = _generate_recommendation(summary)

    return summary


def _top_brands(products: List[dict], n: int) -> Dict[str, int]:
    brands = Counter()
    for p in products:
        b = p.get("brand")
        if b:
            brands[b] += 1
    return dict(brands.most_common(n))


def _count_decisions(products: List[dict]) -> Dict[str, int]:
    decisions = Counter()
    for p in products:
        d = p.get("decision", "unknown")
        decisions[d] += 1
    return dict(decisions)


def _generate_recommendation(summary: Dict[str, Any]) -> Dict[str, Any]:
    """根据分析结果生成选品建议"""
    rec = {
        "score": 0,        # 总分 0-100
        "level": "观望",   # 推荐 / 适中 / 观望 / 避开
        "reasons": [],
        "risks": [],
        "opportunities": [],
    }

    sa = summary.get("search_analysis", {})
    da = summary.get("detail_analysis", {})
    ra = summary.get("review_analysis", {})
    decisions = sa.get("decisions", {})

    score = 50  # 基准分

    # 市场需求
    total_bought = sa.get("total_bought", 0)
    if total_bought > 500:
        score += 15
        rec["opportunities"].append(f"市场需求旺盛，近期购买量 {total_bought}")
    elif total_bought > 100:
        score += 8
        rec["opportunities"].append(f"有一定市场需求，近期购买量 {total_bought}")
    elif total_bought > 0:
        rec["risks"].append("市场需求偏小")

    # 竞争程度
    max_reviews = sa.get("max_reviews", 0)
    avg_reviews_str = sa.get("avg_reviews", "0")
    try:
        avg_reviews = int(avg_reviews_str.replace(",", ""))
    except (ValueError, AttributeError):
        avg_reviews = 0

    if avg_reviews < 200:
        score += 10
        rec["opportunities"].append(f"竞争较小，平均评论数 {avg_reviews}")
    elif avg_reviews > 2000:
        score -= 15
        rec["risks"].append(f"竞争激烈，平均评论数 {avg_reviews}")
    elif avg_reviews > 500:
        score -= 5
        rec["risks"].append(f"竞争较大，平均评论数 {avg_reviews}")

    # 价格区间
    try:
        price_str = sa.get("avg_price", "$0")
        avg_price = float(price_str.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        avg_price = 0

    if 15 <= avg_price <= 50:
        score += 10
        rec["opportunities"].append(f"价格甜点区间 ${avg_price:.0f}")
    elif avg_price < 15:
        score -= 5
        rec["risks"].append(f"价格偏低 ${avg_price:.0f}，利润空间小")
    elif avg_price > 100:
        rec["risks"].append(f"价格偏高 ${avg_price:.0f}，资金要求高")

    # 广告密度
    sponsored_ratio = sa.get("sponsored_ratio", "0/0")
    try:
        parts = sponsored_ratio.split("/")
        sp_ratio = int(parts[0]) / max(int(parts[1]), 1)
        if sp_ratio > 0.5:
            score -= 5
            rec["risks"].append("广告占比高，竞争激烈")
        elif sp_ratio < 0.2:
            score += 5
            rec["opportunities"].append("广告占比低，竞争较温和")
    except (ValueError, IndexError):
        pass

    # 评论评分
    if ra:
        try:
            avg_rev_rating = float(ra.get("avg_rating", "0"))
            if avg_rev_rating >= 4.0:
                score += 5
                rec["opportunities"].append(f"市场满意度高，均分 {avg_rev_rating}")
            elif avg_rev_rating < 3.5:
                score += 10  # 低分=改进空间大
                rec["opportunities"].append(f"市场满意度低（均分 {avg_rev_rating}），产品改进机会大")
        except (ValueError, TypeError):
            pass

        pain_count = len(ra.get("pain_points", []))
        if pain_count > 3:
            rec["opportunities"].append(f"差评痛点 {pain_count} 个，可针对性改进")

    # 选品决策比例
    recommend_count = decisions.get("recommend", 0)
    watch_count = decisions.get("watch", 0)
    total_screened = sum(decisions.values())
    if total_screened > 0:
        good_ratio = (recommend_count + watch_count) / total_screened
        if good_ratio > 0.5:
            score += 5
        elif good_ratio < 0.1:
            score -= 10
            rec["risks"].append("筛选后推荐商品少")

    # 限定分数范围
    score = max(0, min(100, score))
    rec["score"] = score

    if score >= 70:
        rec["level"] = "推荐"
    elif score >= 50:
        rec["level"] = "适中"
    elif score >= 30:
        rec["level"] = "观望"
    else:
        rec["level"] = "避开"

    return rec


# ═══════════════════════════════════════════════════════════════════
# 报告生成
# ═══════════════════════════════════════════════════════════════════

def generate_html_report(results: List[KeywordAnalysisResult], output_path: str) -> str:
    """生成选品分析 HTML 报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 按推荐分数排序
    sorted_results = sorted(
        results,
        key=lambda r: r.summary.get("recommendation", {}).get("score", 0),
        reverse=True,
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Amazon选品分析报告 - Pipeline</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f0f2f5; color: #1a1a2e; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #232f3e, #ff9900); color: white; padding: 40px; border-radius: 16px; margin-bottom: 30px; text-align: center; }}
        .header h1 {{ font-size: 28px; margin-bottom: 8px; }}
        .header p {{ opacity: 0.9; font-size: 14px; }}
        .stats-row {{ display: flex; gap: 16px; margin-bottom: 30px; flex-wrap: wrap; }}
        .stat-card {{ background: white; border-radius: 12px; padding: 20px; flex: 1; min-width: 150px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); text-align: center; }}
        .stat-card .value {{ font-size: 32px; font-weight: 700; color: #232f3e; }}
        .stat-card .label {{ font-size: 13px; color: #666; margin-top: 4px; }}
        .keyword-section {{ background: white; border-radius: 16px; margin-bottom: 24px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .keyword-header {{ padding: 24px 28px; border-bottom: 1px solid #f0f0f0; display: flex; justify-content: space-between; align-items: center; }}
        .keyword-header h2 {{ font-size: 20px; color: #232f3e; }}
        .score-badge {{ padding: 6px 16px; border-radius: 20px; font-weight: 700; font-size: 16px; }}
        .score-recommend {{ background: #d4edda; color: #155724; }}
        .score-moderate {{ background: #fff3cd; color: #856404; }}
        .score-watch {{ background: #f8d7da; color: #721c24; }}
        .score-avoid {{ background: #d6d8db; color: #383d41; }}
        .keyword-body {{ padding: 24px 28px; }}
        .analysis-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 20px; margin-bottom: 20px; }}
        .analysis-card {{ background: #fafbfc; border-radius: 10px; padding: 18px; border: 1px solid #e8e8e8; }}
        .analysis-card h3 {{ font-size: 14px; color: #232f3e; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 2px solid #ff9900; }}
        .metric-row {{ display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }}
        .metric-row .key {{ color: #666; }}
        .metric-row .val {{ color: #1a1a2e; font-weight: 500; }}
        .insight-box {{ margin-top: 16px; padding: 16px; border-radius: 10px; }}
        .opportunity {{ background: #d4edda; border-left: 4px solid #28a745; }}
        .risk {{ background: #f8d7da; border-left: 4px solid #dc3545; }}
        .insight-box h4 {{ font-size: 13px; margin-bottom: 8px; }}
        .insight-box li {{ font-size: 13px; margin-left: 16px; margin-bottom: 4px; }}
        .product-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 16px; }}
        .product-table th {{ background: #232f3e; color: white; padding: 10px 12px; text-align: left; }}
        .product-table td {{ padding: 10px 12px; border-bottom: 1px solid #eee; }}
        .product-table tr:hover {{ background: #f8f9fa; }}
        .decision-recommend {{ color: #28a745; font-weight: 600; }}
        .decision-watch {{ color: #ffc107; font-weight: 600; }}
        .decision-pass {{ color: #6c757d; }}
        .review-cloud {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
        .word-tag {{ padding: 4px 10px; border-radius: 12px; font-size: 12px; background: #e9ecef; color: #495057; }}
        .word-tag.hot {{ background: #ff9900; color: white; }}
        .pain-item {{ background: white; border-radius: 8px; padding: 12px; margin-top: 8px; border-left: 3px solid #dc3545; }}
        .pain-item .title {{ font-weight: 600; color: #dc3545; font-size: 13px; }}
        .pain-item .body {{ font-size: 12px; color: #666; margin-top: 4px; }}
        .rating-bar {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }}
        .rating-bar .bar {{ height: 16px; background: #ff9900; border-radius: 4px; min-width: 2px; }}
        .empty {{ text-align: center; color: #999; padding: 40px; }}
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Amazon 选品分析报告</h1>
        <p>{len(results)} 个关键词 | Pipeline 全自动分析 | 生成时间：{now}</p>
    </div>

    <div class="stats-row">
        <div class="stat-card">
            <div class="value">{len(results)}</div>
            <div class="label">分析关键词</div>
        </div>
        <div class="stat-card">
            <div class="value">{sum(len(r.search_products) for r in results)}</div>
            <div class="label">搜索商品数</div>
        </div>
        <div class="stat-card">
            <div class="value">{sum(len(r.details) for r in results)}</div>
            <div class="label">详情抓取数</div>
        </div>
        <div class="stat-card">
            <div class="value">{sum(len(r.reviews) for r in results)}</div>
            <div class="label">评论抓取数</div>
        </div>
    </div>
"""

    for ka in sorted_results:
        summary = ka.summary
        if "error" in summary:
            html += f"""
    <div class="keyword-section">
        <div class="keyword-header">
            <h2>{ka.keyword}</h2>
            <span class="score-badge score-avoid">失败</span>
        </div>
        <div class="keyword-body">
            <div class="empty">搜索失败: {summary['error']}</div>
        </div>
    </div>
"""
            continue

        rec = summary.get("recommendation", {})
        score = rec.get("score", 0)
        level = rec.get("level", "未知")
        level_class = {"推荐": "score-recommend", "适中": "score-moderate", "观望": "score-watch", "避开": "score-avoid"}.get(level, "score-watch")

        html += f"""
    <div class="keyword-section">
        <div class="keyword-header">
            <h2>{ka.keyword}</h2>
            <span class="score-badge {level_class}">{score}分 · {level}</span>
        </div>
        <div class="keyword-body">
            <div class="analysis-grid">
"""

        # 搜索分析
        sa = summary.get("search_analysis", {})
        if sa:
            html += f"""
                <div class="analysis-card">
                    <h3>📊 搜索结果分析</h3>
                    <div class="metric-row"><span class="key">价格区间</span><span class="val">{sa.get('price_range', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">均价</span><span class="val">{sa.get('avg_price', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">中位价</span><span class="val">{sa.get('median_price', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">平均评分</span><span class="val">{sa.get('avg_rating', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">平均评论数</span><span class="val">{sa.get('avg_reviews', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">近期购买量</span><span class="val">{sa.get('total_bought', 0)}</span></div>
                    <div class="metric-row"><span class="key">广告占比</span><span class="val">{sa.get('sponsored_ratio', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">品牌分布</span><span class="val">{', '.join(f'{k}({v})' for k,v in sa.get('brands', {}).items()) or 'N/A'}</span></div>
                    <div class="metric-row"><span class="key">筛选结果</span><span class="val">推荐{sa.get('decisions',{}).get('recommend',0)} / 关注{sa.get('decisions',{}).get('watch',0)} / 排除{sa.get('decisions',{}).get('exclude',0)}</span></div>
                </div>
"""

        # 详情分析
        da = summary.get("detail_analysis", {})
        if da:
            html += f"""
                <div class="analysis-card">
                    <h3>🔍 详情分析</h3>
                    <div class="metric-row"><span class="key">抓取成功率</span><span class="val">{da.get('success_rate', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">详情价格区间</span><span class="val">{da.get('price_range', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">详情均价</span><span class="val">{da.get('avg_price', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">详情均分</span><span class="val">{da.get('avg_rating', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">Prime比例</span><span class="val">{da.get('prime_ratio', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">Amazon's Choice</span><span class="val">{da.get('amazon_choice_count', 0)}</span></div>
                    <div class="metric-row"><span class="key">Best Seller</span><span class="val">{da.get('best_seller_count', 0)}</span></div>
                    <div class="metric-row"><span class="key">热门类目</span><span class="val">{', '.join(f'{k}({v})' for k,v in list(da.get('top_categories', {}).items())[:3]) or 'N/A'}</span></div>
                </div>
"""

        # 评论分析
        ra = summary.get("review_analysis", {})
        if ra:
            rating_dist = ra.get("rating_distribution", {})
            max_count = max(rating_dist.values()) if rating_dist else 1

            html += f"""
                <div class="analysis-card">
                    <h3>💬 评论分析</h3>
                    <div class="metric-row"><span class="key">评论总数</span><span class="val">{ra.get('total_reviews', 0)}</span></div>
                    <div class="metric-row"><span class="key">平均评分</span><span class="val">{ra.get('avg_rating', 'N/A')}</span></div>
                    <div class="metric-row"><span class="key">验证购买</span><span class="val">{ra.get('verified_ratio', 'N/A')}</span></div>
                    <div style="margin-top: 8px;">
"""

            for star in [5, 4, 3, 2, 1]:
                count = rating_dist.get(star, 0)
                bar_w = int(count / max_count * 120) if max_count > 0 else 0
                html += f"""<div class="rating-bar"><span>{star}★</span><div class="bar" style="width:{bar_w}px;"></div><span>{count}</span></div>\n"""

            # 高频词
            top_words = ra.get("top_words", {})
            if top_words:
                html += '<div class="review-cloud">'
                for w, c in list(top_words.items())[:15]:
                    cls = "hot" if c > 3 else ""
                    html += f'<span class="word-tag {cls}">{w} ({c})</span>'
                html += '</div>'

            html += """
                </div>
"""

            # 痛点
            pain_points = ra.get("pain_points", [])
            if pain_points:
                html += '<div style="margin-top: 12px;"><strong style="font-size:13px;color:#dc3545;">⚠️ 差评痛点</strong>'
                for pp in pain_points[:5]:
                    html += f"""
                    <div class="pain-item">
                        <div class="title">★{pp['rating']} - {pp['title']}</div>
                        <div class="body">{pp['body']}</div>
                    </div>
"""
                html += '</div>'

            html += """
                </div>
"""

        html += """
            </div>
"""

        # 机会与风险
        opportunities = rec.get("opportunities", [])
        risks = rec.get("risks", [])
        if opportunities:
            html += '<div class="insight-box opportunity"><h4>✅ 机会</h4><ul>'
            for o in opportunities:
                html += f'<li>{o}</li>'
            html += '</ul></div>'
        if risks:
            html += '<div class="insight-box risk"><h4>⚠️ 风险</h4><ul>'
            for r in risks:
                html += f'<li>{r}</li>'
            html += '</ul></div>'

        # 商品表格
        if ka.search_products:
            html += """
            <table class="product-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>ASIN</th>
                        <th>商品标题</th>
                        <th>品牌</th>
                        <th>价格</th>
                        <th>评分</th>
                        <th>评论</th>
                        <th>购买量</th>
                        <th>决策</th>
                    </tr>
                </thead>
                <tbody>
"""
            for idx, p in enumerate(ka.search_products, 1):
                decision = p.get("decision", "")
                dec_class = {"recommend": "decision-recommend", "watch": "decision-watch"}.get(decision, "decision-pass")
                dec_text = {"recommend": "推荐", "watch": "关注", "exclude": "排除"}.get(decision, decision)

                html += f"""
                    <tr>
                        <td>{idx}</td>
                        <td><a href="https://www.amazon.com/dp/{p.get('asin','')}" target="_blank" style="color:#007bff;">{p.get('asin','')}</a></td>
                        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{p.get('title','')[:80]}</td>
                        <td>{p.get('brand','') or '-'}</td>
                        <td>${p.get('price','') or '-'}</td>
                        <td>{p.get('rating','') or '-'}</td>
                        <td>{p.get('review_count','') or '-'}</td>
                        <td>{p.get('bought_count_est', 0)}</td>
                        <td class="{dec_class}">{dec_text}</td>
                    </tr>
"""
            html += """
                </tbody>
            </table>
"""

        html += """
        </div>
    </div>
"""

    html += """
</div>
</body>
</html>
"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


# ═══════════════════════════════════════════════════════════════════
# 保存中间结果
# ═══════════════════════════════════════════════════════════════════

def _save_keyword_results(ka: KeywordAnalysisResult, keyword: str):
    """保存每个关键词的原始数据"""
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", keyword).strip("_")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 搜索结果
    if ka.search_products:
        df = pd.DataFrame(ka.search_products)
        df.to_csv(OUTPUT_DIR / f"pipeline_{safe_name}_search.csv", index=False, encoding="utf-8-sig")

    # 详情
    if ka.details:
        df = pd.DataFrame(ka.details)
        df.to_csv(OUTPUT_DIR / f"pipeline_{safe_name}_details.csv", index=False, encoding="utf-8-sig")

    # 评论
    if ka.reviews:
        df = pd.DataFrame(ka.reviews)
        df.to_csv(OUTPUT_DIR / f"pipeline_{safe_name}_reviews.csv", index=False, encoding="utf-8-sig")

    # 摘要 JSON
    import json
    with open(OUTPUT_DIR / f"pipeline_{safe_name}_summary.json", "w", encoding="utf-8") as f:
        json.dump(ka.summary, f, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _sleep_jitter(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))


# ═══════════════════════════════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Amazon 一键选品分析 Pipeline：关键词搜索 → ASIN详情+评论 → 分析报告"
    )
    parser.add_argument(
        "--keywords", "-k",
        nargs="+",
        help="分析关键词列表",
    )
    parser.add_argument(
        "--keywords-file",
        help="关键词列表文件（每行一个）",
    )
    parser.add_argument(
        "--urls",
        nargs="+",
        help="Amazon URL 列表（热销榜/飙升榜/搜索页 URL）",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="每个关键词搜索页数（默认1）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="每个关键词取搜索结果前N个进入详情抓取（默认10）",
    )
    parser.add_argument(
        "--max-review-pages",
        type=int,
        default=3,
        help="每个商品最多抓取评论页数（默认3）",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="显示浏览器窗口",
    )
    parser.add_argument(
        "--skip-reviews",
        action="store_true",
        help="跳过评论抓取（加快速度）",
    )
    parser.add_argument(
        "--rules-xlsx",
        default=str(DEFAULT_RULES_PATH),
        help="规则文件路径",
    )
    parser.add_argument(
        "--sort-by",
        default="sales_proxy",
        choices=["default", "sales_proxy", "review_count", "price_low", "price_high"],
        help="排序方式: sales_proxy(销量综合) | review_count(评论数) | price_low(价格升序) | price_high(价格降序)",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="最低价格（美元）",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="最高价格（美元）",
    )
    parser.add_argument(
        "--marketplace",
        default="US",
        choices=list(MARKETPLACES.keys()),
        help="Amazon 站点（默认 US）",
    )
    parser.add_argument(
        "--search-sort",
        default="relevance",
        choices=["relevance", "sales-rank", "price-asc", "price-desc", "review-rank", "date-desc", "avg-rating"],
        help="Amazon 搜索页排序方式（默认 relevance）",
    )
    parser.add_argument(
        "--max-search-items",
        type=int,
        default=None,
        help="搜索时最多抓取商品数（默认不限制）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="报告输出路径（默认 outputs/pipeline_report_YYYYMMDD.html）",
    )

    args = parser.parse_args()

    # 解析关键词
    keywords = list(args.keywords or [])
    urls = list(args.urls or [])

    if args.keywords_file:
        path = Path(args.keywords_file)
        if not path.exists():
            print(f"关键词文件不存在: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, encoding="utf-8") as f:
            for line in f:
                kw = line.strip()
                if kw and not kw.startswith("#"):
                    keywords.append(kw)

    if not keywords and not urls:
        print("请提供关键词 (--keywords) 或 Amazon URL (--urls)", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    if keywords:
        print(f"共 {len(keywords)} 个关键词:")
        for kw in keywords:
            print(f"  - {kw}")
    if urls:
        print(f"共 {len(urls)} 个 URL:")
        for u in urls:
            print(f"  - {u}")

    # 输出路径
    output_path = args.output or str(OUTPUT_DIR / f"pipeline_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")

    # 运行 Pipeline
    if urls:
        results = run_url_pipeline(
            urls=urls,
            pages=args.pages,
            top_n=args.top,
            max_review_pages=args.max_review_pages,
            headless=not args.headful,
            skip_reviews=args.skip_reviews,
            rules_path=args.rules_xlsx,
            sort_by=args.sort_by,
            max_items=args.max_search_items,
            min_price=args.min_price,
            max_price=args.max_price,
        )
    else:
        results = run_pipeline(
            keywords=keywords,
            pages=args.pages,
            top_n=args.top,
            max_review_pages=args.max_review_pages,
            headless=not args.headful,
            skip_reviews=args.skip_reviews,
            rules_path=args.rules_xlsx,
            sort_by=args.sort_by,
            marketplace=args.marketplace,
            search_sort=args.search_sort,
            min_price=args.min_price,
            max_price=args.max_price,
            max_search_items=args.max_search_items,
    )

    # 生成报告
    print(f"\n生成选品分析报告...")
    report_path = generate_html_report(results, output_path)
    print(f"报告已保存: {report_path}")

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"选品分析摘要")
    print(f"{'='*60}")
    for ka in results:
        rec = ka.summary.get("recommendation", {})
        score = rec.get("score", 0)
        level = rec.get("level", "未知")
        print(f"  {ka.keyword}: {score}分 ({level}) | 搜索{ka.summary.get('search_count',0)} 详情{ka.summary.get('detail_count',0)} 评论{ka.summary.get('review_count',0)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
