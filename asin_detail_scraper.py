#!/usr/bin/env python3
"""
Amazon ASIN 商品详情+评论爬虫
根据 ASIN 列表，抓取商品详情页内容以及对应评论页评价
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page, sync_playwright


AMAZON_BASE_URL = "https://www.amazon.com"
DEBUG_DIR = Path("outputs") / "debug_asin"
OUTPUT_DIR = Path("outputs")
MAX_RETRIES = 3


# ─── 数据模型 ──────────────────────────────────────────────────

@dataclass
class ProductDetail:
    """商品详情"""
    asin: str = ""
    title: str = ""
    brand: str = ""
    price: Optional[float] = None
    currency: str = "USD"
    rating: Optional[float] = None
    review_count: Optional[int] = None
    sales_rank: Optional[int] = None
    sales_rank_category: str = ""
    category_path: str = ""
    feature_bullets: str = ""          # 商品特点，用 || 分隔
    description: str = ""              # 商品描述
    technical_details: str = ""        # 技术参数 JSON
    image_urls: str = ""               # 图片URL列表，用 || 分隔
    is_prime: bool = False
    is_amazon_choice: bool = False
    is_best_seller: bool = False
    variant_asins: str = ""            # 变体ASIN列表，用 || 分隔
    availability: str = ""             # 库存状态
    product_url: str = ""
    scrape_time: str = ""
    scrape_status: str = "pending"     # pending / success / failed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProductReview:
    """商品评论"""
    asin: str = ""
    review_id: str = ""
    rating: Optional[float] = None
    title: str = ""
    author: str = ""
    date: str = ""
    verified_purchase: bool = False
    helpful_count: Optional[int] = None
    body: str = ""
    review_url: str = ""
    scrape_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── 详情页抓取 ─────────────────────────────────────────────────

def scrape_product_detail(page: Page, asin: str) -> tuple:
    """
    抓取商品详情页
    
    Returns:
        (ProductDetail, BeautifulSoup) - 详情数据和页面对象（供评论抓取复用）
    """
    url = f"{AMAZON_BASE_URL}/dp/{asin}"
    detail = ProductDetail(
        asin=asin,
        product_url=url,
        scrape_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            _sleep_jitter(2, 5)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            if attempt == MAX_RETRIES:
                detail.scrape_status = "failed"
                _save_debug(page, f"detail_{asin}")
                return detail, None
            continue

        html = page.content()

        # 检测是否被拦截
        if _is_blocked(html):
            if attempt == MAX_RETRIES:
                detail.scrape_status = "failed"
                _save_debug(page, f"blocked_{asin}")
                return detail, None
            continue

        break

    soup = BeautifulSoup(html, "html.parser")

    # ── 标题 ──
    title_el = soup.select_one("#productTitle") or soup.select_one("#title span")
    if title_el:
        detail.title = _clean(title_el.get_text())

    # ── 品牌 ──
    brand_el = (
        soup.select_one("#bylineInfo a")
        or soup.select_one("a#bylineInfo")
        or soup.select_one(".po-brand .po-break-word")
    )
    if brand_el:
        brand = _clean(brand_el.get_text())
        brand = re.sub(r"^(Visit the |Brand:\s*|by\s+)", "", brand, flags=re.IGNORECASE)
        brand = re.sub(r"\s+Store$", "", brand, flags=re.IGNORECASE)
        detail.brand = brand

    # ── 价格 ──
    price_el = (
        soup.select_one("span.a-price > span.a-offscreen")
        or soup.select_one("#priceblock_ourprice")
        or soup.select_one("#priceblock_dealprice")
        or soup.select_one("#priceblock_saleprice")
        or soup.select_one(".apexPriceToPay .a-offscreen")
    )
    if price_el:
        price_text = _clean(price_el.get_text())
        detail.price = _parse_price(price_text)
        if "$" in price_text:
            detail.currency = "USD"

    # ── 评分 ──
    rating_el = soup.select_one("#acrPopover span.a-icon-alt") or soup.select_one("span[data-hook='average-stars-rating'] span.a-icon-alt")
    if rating_el:
        detail.rating = _parse_float(rating_el.get_text())

    # ── 评论数 ──
    review_el = soup.select_one("#acrCustomerReviewText")
    if review_el:
        detail.review_count = _parse_int(review_el.get_text())

    # ── 销量排名 ──
    detail.sales_rank, detail.sales_rank_category = _extract_sales_rank(soup)

    # ── 分类路径 ──
    breadcrumbs = soup.select("#wayfinding-breadcrumbs_container ul li a, #wayfinding-breadcrumbs_feature_div ul li a")
    if breadcrumbs:
        detail.category_path = " > ".join(_clean(b.get_text()) for b in breadcrumbs)

    # ── 特点列表 ──
    bullets = soup.select("#feature-bullets ul li span.a-list-item")
    if bullets:
        features = [_clean(b.get_text()) for b in bullets if _clean(b.get_text()) and "Make sure this fits" not in _clean(b.get_text())]
        detail.feature_bullets = " || ".join(features)

    # ── 商品描述 ──
    desc_el = soup.select_one("#productDescription") or soup.select_one("#aplus")
    if desc_el:
        detail.description = _clean(desc_el.get_text())[:3000]

    # ── 技术参数 ──
    tech_details = {}
    for row in soup.select("#productDetails_techSpec_section_1 tr, #technicalSpecifications_section_1 tr, #productDetails_detailBullets_sections1 tr"):
        key_el = row.select_one("th, td:first-child")
        val_el = row.select_one("td:last-child, td:nth-child(2)")
        if key_el and val_el:
            tech_details[_clean(key_el.get_text())] = _clean(val_el.get_text())
    # 也从 detailBullets 提取
    for li in soup.select("#detailBullets_feature_div li"):
        text = _clean(li.get_text())
        match = re.match(r"([^:：]+)[：:]\s*(.+)", text)
        if match:
            tech_details[match.group(1).strip()] = match.group(2).strip()
    if tech_details:
        detail.technical_details = json.dumps(tech_details, ensure_ascii=False)

    # ── 图片URL ──
    image_urls = []
    for img in soup.select("#imageBlock img, #landingImage, #imgBlkFront"):
        src = img.get("data-old-hires") or img.get("src") or ""
        if src and "grey-pixel" not in src:
            image_urls.append(src)
    # 从 JS 变量中提取更多图片
    color_images = re.findall(r'"hiRes"\s*:\s*"([^"]+)"', html)
    for ci in color_images:
        if ci not in image_urls:
            image_urls.append(ci)
    if image_urls:
        detail.image_urls = " || ".join(dict.fromkeys(image_urls))  # 去重保序

    # ── Prime ──
    detail.is_prime = bool(soup.select_one(".a-icon-prime, [aria-label*='Prime']"))

    # ── Amazon's Choice / Best Seller ──
    for badge in soup.select(".ac-badge-text, .a-badge-text, #acBadge_feature_div span"):
        text = _clean(badge.get_text()).lower()
        if "amazon's choice" in text:
            detail.is_amazon_choice = True
        if "best seller" in text:
            detail.is_best_seller = True

    # ── 变体 ASIN ──
    variant_asins = []
    for opt in soup.select("#twister .a-button-inner input, #variation_id input, [data-asin]"):
        va = (opt.get("value") or opt.get("data-asin") or "").strip()
        if va and re.fullmatch(r"B[A-Z0-9]{9}", va) and va != asin:
            variant_asins.append(va)
    # 从 JS 变量中提取
    twister_matches = re.findall(r'"asin"\s*:\s*"(B[A-Z0-9]{9})"', html)
    for ta in twister_matches:
        if ta != asin and ta not in variant_asins:
            variant_asins.append(ta)
    if variant_asins:
        detail.variant_asins = " || ".join(dict.fromkeys(variant_asins))

    # ── 库存状态 ──
    avail_el = soup.select_one("#availability span")
    if avail_el:
        detail.availability = _clean(avail_el.get_text())

    detail.scrape_status = "success"
    return detail, soup


# ─── 评论页抓取 ─────────────────────────────────────────────────

def extract_reviews_from_detail_page(
    soup: BeautifulSoup,
    asin: str,
) -> List[ProductReview]:
    """从详情页底部的评论区提取评论"""
    reviews = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 详情页底部的评论区域
    review_els = soup.select("#cm-cr-dp-review-list [data-hook='review'], #cm-cr-dp-review-list .review")
    if not review_els:
        # 备用选择器
        review_els = soup.select("#reviews-medley-footer [data-hook='review'], .cr-widget [data-hook='review']")

    for el in review_els:
        try:
            review = ProductReview(asin=asin, scrape_time=now)
            review.review_id = el.get("id", "")

            # 评分
            rating_el = el.select_one("[data-hook='review-star-rating'] span.a-icon-alt, i[data-hook='review-star-rating'] span.a-icon-alt")
            if not rating_el:
                rating_el = el.select_one("i.a-icon-star span.a-icon-alt, .a-icon-star-small span.a-icon-alt")
            if rating_el:
                review.rating = _parse_float(rating_el.get_text())

            # 标题
            title_el = el.select_one("[data-hook='review-title'] span:last-child, [data-hook='review-title']")
            if title_el:
                title_text = _clean(title_el.get_text())
                # 清理 "5.0 out of 5 stars" 前缀
                title_text = re.sub(r"^\d+\.?\d*\s+out\s+of\s+5\s+stars\s*", "", title_text)
                review.title = title_text

            # 作者
            author_el = el.select_one(".a-profile-name")
            if author_el:
                review.author = _clean(author_el.get_text())

            # 日期
            date_el = el.select_one("[data-hook='review-date']")
            if date_el:
                review.date = _clean(date_el.get_text())

            # 验证购买
            vp_el = el.select_one("[data-hook='avp-badge']")
            review.verified_purchase = vp_el is not None

            # 有用数
            helpful_el = el.select_one("[data-hook='helpful-vote-statement']")
            if helpful_el:
                review.helpful_count = _parse_int(helpful_el.get_text())

            # 评论正文
            body_el = el.select_one("[data-hook='review-body'] span, [data-hook='review-body']")
            if body_el:
                body_text = _clean(body_el.get_text())
                # 清理 "Read more" 后缀
                body_text = re.sub(r"\s*Read more\s*$", "", body_text)
                review.body = body_text[:5000]

            # 评论链接
            link_el = el.select_one("a[data-hook='review-title']")
            if link_el:
                href = link_el.get("href", "")
                review.review_url = f"{AMAZON_BASE_URL}{href}" if href.startswith("/") else href

            reviews.append(review)
        except Exception:
            continue

    return reviews


def scrape_product_reviews_via_see_all(
    page: Page,
    asin: str,
    max_pages: int = 5,
    star_filter: Optional[int] = None,
) -> List[ProductReview]:
    """
    通过详情页底部的 'See all reviews' 链接进入评论页，
    而不是直接访问评论URL（减少被拦截概率）
    """
    reviews: List[ProductReview] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 先导航到详情页
    detail_url = f"{AMAZON_BASE_URL}/dp/{asin}"
    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        return reviews

    # 滚动到底部评论区
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # 点击 "See all reviews" 链接
    try:
        see_all_link = page.query_selector("a[data-hook='see-all-reviews-link-foot'], #reviews-medley-footer a[data-hook='see-all-reviews-link-foot']")
        if not see_all_link:
            see_all_link = page.query_selector("a:has-text('See all reviews')")
        if not see_all_link:
            # 尝试直接访问评论页
            reviews_url = f"{AMAZON_BASE_URL}/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_btm?ie=UTF8&reviewerType=all_reviews&pageNumber=1"
            if star_filter:
                reviews_url += f"&filterByStar={star_filter}_star"
            page.goto(reviews_url, wait_until="domcontentloaded", timeout=30_000)
        else:
            see_all_link.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        return reviews

    # 检查是否被拦截
    html = page.content()
    if _is_blocked(html):
        _save_debug(page, f"blocked_review_via_see_all_{asin}")
        return reviews

    # 解析第一页评论
    soup = BeautifulSoup(html, "html.parser")
    page_reviews = _parse_review_page(soup, asin, now)
    reviews.extend(page_reviews)

    # 翻页获取更多评论
    for page_num in range(2, max_pages + 1):
        # 构建下一页URL
        current_url = page.url
        # 替换页码
        next_url = re.sub(r'pageNumber=\d+', f'pageNumber={page_num}', current_url)
        if 'pageNumber=' not in next_url:
            next_url += f'&pageNumber={page_num}'
        if star_filter and 'filterByStar' not in next_url:
            next_url += f'&filterByStar={star_filter}_star'

        _sleep_jitter(1.5, 3.5)

        try:
            page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            break

        html = page.content()
        if _is_blocked(html):
            break

        soup = BeautifulSoup(html, "html.parser")
        page_reviews = _parse_review_page(soup, asin, now)

        if not page_reviews:
            break

        reviews.extend(page_reviews)

    return reviews


def scrape_product_reviews(
    page: Page,
    asin: str,
    max_pages: int = 5,
    star_filter: Optional[int] = None,
) -> List[ProductReview]:
    """抓取商品评论（组合策略）"""
    # 策略1: 先尝试通过 'See all reviews' 链接进入
    reviews = scrape_product_reviews_via_see_all(page, asin, max_pages, star_filter)
    if reviews:
        return reviews

    # 策略2: 直接访问评论URL
    reviews_list: List[ProductReview] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for page_num in range(1, max_pages + 1):
        url = f"{AMAZON_BASE_URL}/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_btm?ie=UTF8&reviewerType=all_reviews&pageNumber={page_num}"
        if star_filter:
            url += f"&filterByStar={star_filter}_star"

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                _sleep_jitter(2, 4)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                if attempt == MAX_RETRIES:
                    _save_debug(page, f"review_{asin}_p{page_num}")
                    return reviews_list
                continue

            html = page.content()
            if _is_blocked(html):
                if attempt == MAX_RETRIES:
                    _save_debug(page, f"blocked_review_{asin}")
                    return reviews_list
                continue

            break

        soup = BeautifulSoup(html, "html.parser")
        page_reviews = _parse_review_page(soup, asin, now)

        if not page_reviews:
            break

        reviews_list.extend(page_reviews)

        next_btn = soup.select_one("li.a-last a")
        if not next_btn:
            break

        _sleep_jitter(1.5, 3.5)

    return reviews_list


def _parse_review_page(soup: BeautifulSoup, asin: str, now: str) -> List[ProductReview]:
    """解析评论页"""
    reviews = []

    review_els = soup.select("[data-hook='review']")

    for el in review_els:
        try:
            review = ProductReview(asin=asin, scrape_time=now)

            # review ID
            review_id = el.get("id", "")
            review.review_id = review_id

            # 评分
            rating_el = el.select_one("[data-hook='review-star-rating'] span.a-icon-alt, i[data-hook='review-star-rating'] span.a-icon-alt")
            if not rating_el:
                rating_el = el.select_one("i.a-icon-star span.a-icon-alt")
            if rating_el:
                review.rating = _parse_float(rating_el.get_text())

            # 标题
            title_el = el.select_one("[data-hook='review-title'] span:last-child, [data-hook='review-title']")
            if title_el:
                title_text = _clean(title_el.get_text())
                # 清理 "5.0 out of 5 stars" 前缀
                title_text = re.sub(r"^\d+\.?\d*\s+out\s+of\s+5\s+stars\s*", "", title_text)
                review.title = title_text

            # 作者
            author_el = el.select_one(".a-profile-name")
            if author_el:
                review.author = _clean(author_el.get_text())

            # 日期
            date_el = el.select_one("[data-hook='review-date']")
            if date_el:
                review.date = _clean(date_el.get_text())

            # 验证购买
            vp_el = el.select_one("[data-hook='avp-badge']")
            review.verified_purchase = vp_el is not None

            # 有用数
            helpful_el = el.select_one("[data-hook='helpful-vote-statement']")
            if helpful_el:
                review.helpful_count = _parse_int(helpful_el.get_text())

            # 评论正文
            body_el = el.select_one("[data-hook='review-body'] span, [data-hook='review-body']")
            if body_el:
                body_text = _clean(body_el.get_text())
                body_text = re.sub(r"\s*Read more\s*$", "", body_text)
                review.body = body_text[:5000]

            # 评论链接
            link_el = el.select_one("a[data-hook='review-title']")
            if link_el:
                href = link_el.get("href", "")
                review.review_url = f"{AMAZON_BASE_URL}{href}" if href.startswith("/") else href

            reviews.append(review)

        except Exception:
            continue

    return reviews


# ─── 批量抓取主流程 ──────────────────────────────────────────────

def scrape_asins(
    asins: List[str],
    headless: bool = True,
    max_review_pages: int = 5,
    star_filter: Optional[int] = None,
    delay_range: tuple = (2, 5),
    skip_details: bool = False,
    skip_reviews: bool = False,
    progress_callback=None,
) -> tuple:
    """
    批量抓取 ASIN 列表

    Returns:
        (details: List[ProductDetail], reviews: List[ProductReview])
    """
    all_details: List[ProductDetail] = []
    all_reviews: List[ProductReview] = []

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)
        print(msg)

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
            for i, asin in enumerate(asins, 1):
                _log(f"[{i}/{len(asins)}] ASIN: {asin}")

                # ── 详情页 ──
                if not skip_details:
                    _log(f"  抓取详情页...")
                    try:
                        detail, detail_soup = scrape_product_detail(page, asin)
                        all_details.append(detail)
                        _log(f"  详情: {detail.title[:60]}... | ${detail.price} | ★{detail.rating}")

                        # 从详情页底部提取评论（免费获取，不额外请求）
                        if not skip_reviews and detail_soup:
                            detail_reviews = extract_reviews_from_detail_page(detail_soup, asin)
                            if detail_reviews:
                                all_reviews.extend(detail_reviews)
                                _log(f"  详情页底部评论: {len(detail_reviews)} 条")
                    except Exception as e:
                        _log(f"  详情抓取失败: {e}")
                        all_details.append(ProductDetail(
                            asin=asin,
                            product_url=f"{AMAZON_BASE_URL}/dp/{asin}",
                            scrape_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            scrape_status="failed",
                        ))

                    _sleep_jitter(*delay_range)

                # ── 评论页（获取更多评论）──
                if not skip_reviews and max_review_pages > 0:
                    _log(f"  抓取更多评论 (最多 {max_review_pages} 页)...")
                    try:
                        reviews = scrape_product_reviews(page, asin, max_review_pages, star_filter)
                        # 去重：按 review_id 去重
                        existing_ids = {r.review_id for r in all_reviews if r.review_id}
                        new_reviews = [r for r in reviews if r.review_id not in existing_ids]
                        all_reviews.extend(new_reviews)
                        _log(f"  评论页获取 {len(reviews)} 条，新增 {len(new_reviews)} 条")
                    except Exception as e:
                        _log(f"  评论抓取失败: {e}")

                    _sleep_jitter(*delay_range)

        finally:
            page.close()
            context.close()
            browser.close()

    return all_details, all_reviews


# ─── 输出保存 ────────────────────────────────────────────────────

def save_results(
    details: List[ProductDetail],
    reviews: List[ProductReview],
    output_prefix: str,
) -> tuple:
    """
    保存结果到 CSV + Excel

    Returns:
        (details_path, reviews_path)
    """
    import pandas as pd

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 详情
    if details:
        df_detail = pd.DataFrame([d.to_dict() for d in details])
        detail_csv = OUTPUT_DIR / f"{output_prefix}_details.csv"
        detail_xlsx = OUTPUT_DIR / f"{output_prefix}_details.xlsx"
        df_detail.to_csv(detail_csv, index=False, encoding="utf-8-sig")
        df_detail.to_excel(detail_xlsx, index=False, engine="openpyxl")
        print(f"商品详情已保存: {detail_csv} / {detail_xlsx}")
    else:
        detail_csv = detail_xlsx = None

    # 评论
    if reviews:
        df_review = pd.DataFrame([r.to_dict() for r in reviews])
        review_csv = OUTPUT_DIR / f"{output_prefix}_reviews.csv"
        review_xlsx = OUTPUT_DIR / f"{output_prefix}_reviews.xlsx"
        df_review.to_csv(review_csv, index=False, encoding="utf-8-sig")
        df_review.to_excel(review_xlsx, index=False, engine="openpyxl")
        print(f"商品评论已保存: {review_csv} / {review_xlsx}")
    else:
        review_csv = review_xlsx = None

    return detail_csv, review_csv


# ─── 工具函数 ────────────────────────────────────────────────────

def _clean(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_float(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d+\.?\d*)", text)
    return float(match.group(1)) if match else None


def _parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d[\d,]*\.?\d*)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _parse_int(text: str) -> Optional[int]:
    if not text:
        return None
    # 处理 K / M 后缀
    match = re.search(r"([\d.]+)\s*([KM])?", text, re.IGNORECASE)
    if not match:
        return None
    num = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").upper()
    if suffix == "K":
        num *= 1_000
    elif suffix == "M":
        num *= 1_000_000
    return int(num)


def _extract_sales_rank(soup: BeautifulSoup) -> tuple:
    """提取销量排名"""
    # 方式1: detailBullets
    for li in soup.select("#detailBullets_feature_div li, #detailBullets li"):
        text = _clean(li.get_text())
        match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\s*\(|$)", text)
        if match:
            rank = int(match.group(1).replace(",", ""))
            category = match.group(2).strip()
            return rank, category

    # 方式2: productDetails table
    for row in soup.select("#productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr"):
        key = _clean(row.select_one("th, td:first-child").get_text()) if row.select_one("th, td:first-child") else ""
        if "best sellers rank" in key.lower():
            val_el = row.select_one("td:last-child, td:nth-child(2)")
            if val_el:
                text = _clean(val_el.get_text())
                match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\s*\(|$)", text)
                if match:
                    rank = int(match.group(1).replace(",", ""))
                    category = match.group(2).strip()
                    return rank, category

    # 方式3: 直接搜索文本
    rank_section = soup.select_one("#productDetails_db_sections")
    if rank_section:
        text = _clean(rank_section.get_text())
        match = re.search(r"#([\d,]+)\s+in\s+(.+?)(?:\s*\(|$)", text)
        if match:
            rank = int(match.group(1).replace(",", ""))
            category = match.group(2).strip()
            return rank, category

    return None, ""


def _is_blocked(html: str) -> bool:
    lower = html.lower()
    tokens = [
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "to discuss automated access to amazon data",
        "captcha",
        "api-services-support@amazon.com",
    ]
    return any(t in lower for t in tokens)


def _save_debug(page: Page, prefix: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", prefix)
        path = DEBUG_DIR / f"{slug}.html"
        path.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _sleep_jitter(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))


def parse_asin_list(input_str: str) -> List[str]:
    """从字符串、文件路径解析 ASIN 列表"""
    asins = []

    # 尝试作为文件读取
    path = Path(input_str)
    if path.exists():
        content = path.read_text(encoding="utf-8")
        # CSV/TSV 格式 - 读取第一列或名为 ASIN 的列
        if path.suffix.lower() in (".csv", ".tsv", ".txt"):
            import pandas as pd
            try:
                sep = "\t" if path.suffix == ".tsv" else ","
                df = pd.read_csv(path, sep=sep, dtype=str)
                # 优先找 ASIN 列
                for col_name in ["ASIN", "asin", "Asin", "asin1"]:
                    if col_name in df.columns:
                        asins.extend(df[col_name].dropna().str.strip().tolist())
                        break
                else:
                    # 用第一列
                    asins.extend(df.iloc[:, 0].dropna().str.strip().tolist())
            except Exception:
                # 纯文本，每行一个
                asins.extend(line.strip() for line in content.splitlines() if line.strip())
        else:
            asins.extend(line.strip() for line in content.splitlines() if line.strip())
    else:
        # 直接输入 ASIN，逗号/空格/换行分隔
        asins.extend(re.split(r"[,\s]+", input_str.strip()))

    # 去重并验证格式
    valid = []
    seen = set()
    for a in asins:
        a = a.strip().upper()
        if a and re.fullmatch(r"B[A-Z0-9]{9}", a) and a not in seen:
            valid.append(a)
            seen.add(a)

    return valid


# ─── 命令行入口 ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="根据 ASIN 列表抓取 Amazon 商品详情和评论"
    )
    parser.add_argument(
        "asins",
        nargs="*",
        help="ASIN 列表（空格分隔），或用 --input-file 指定文件",
    )
    parser.add_argument(
        "--input-file", "-f",
        help="包含 ASIN 列表的文件路径（CSV/TSV/TXT）",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="输出文件前缀，默认 outputs/asin_details_YYYYMMDD",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="显示浏览器窗口（便于调试/处理验证码）",
    )
    parser.add_argument(
        "--max-review-pages",
        type=int,
        default=5,
        help="每个商品最多抓取的评论页数（默认5）",
    )
    parser.add_argument(
        "--star-filter",
        type=int,
        default=None,
        choices=[1, 2, 3, 4, 5],
        help="只抓取指定星级的评论",
    )
    parser.add_argument(
        "--skip-details",
        action="store_true",
        help="跳过详情页抓取",
    )
    parser.add_argument(
        "--skip-reviews",
        action="store_true",
        help="跳过评论抓取",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=2,
        help="请求间最小延迟秒数（默认2）",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=5,
        help="请求间最大延迟秒数（默认5）",
    )

    args = parser.parse_args()

    # 解析 ASIN
    all_asins = list(args.asins) if args.asins else []
    if args.input_file:
        file_asins = parse_asin_list(args.input_file)
        all_asins.extend(file_asins)

    if not all_asins:
        print("请提供 ASIN 列表或使用 --input-file 指定文件", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    # 去重验证
    asins = parse_asin_list(",".join(all_asins))

    if not asins:
        print("未找到有效的 ASIN（格式应为 B + 9位字母数字，如 B0D66LLY1T）", file=sys.stderr)
        sys.exit(1)

    print(f"共 {len(asins)} 个有效 ASIN:")
    for a in asins:
        print(f"  {a}")

    # 输出前缀
    output_prefix = args.output or f"asin_details_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 开始抓取
    details, reviews = scrape_asins(
        asins=asins,
        headless=not args.headful,
        max_review_pages=args.max_review_pages,
        star_filter=args.star_filter,
        delay_range=(args.delay_min, args.delay_max),
        skip_details=args.skip_details,
        skip_reviews=args.skip_reviews,
    )

    # 保存结果
    save_results(details, reviews, output_prefix)

    # 统计
    print(f"\n{'='*60}")
    print(f"抓取完成!")
    print(f"  商品详情: {len(details)} 条 (成功 {sum(1 for d in details if d.scrape_status == 'success')} / 失败 {sum(1 for d in details if d.scrape_status == 'failed')})")
    print(f"  商品评论: {len(reviews)} 条")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
