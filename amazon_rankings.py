from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page

from models import AmazonSearchProduct


AMAZON_BASE_URL = "https://www.amazon.com"
DEBUG_DIR = Path("outputs") / "debug"
MAX_PAGE_RETRIES = 3


class AmazonPageError(RuntimeError):
    pass


def build_best_sellers_url(category: str = "automotive", page_number: int = 1) -> str:
    encoded = category.strip().replace(" ", "-")
    if page_number > 1:
        return f"{AMAZON_BASE_URL}/Best-Sellers-{encoded}/zgbs/{encoded}/ref=zg_bs_pg_{page_number}?_encoding=UTF8&pg={page_number}"
    return f"{AMAZON_BASE_URL}/Best-Sellers-{encoded}/zgbs/{encoded}"


def build_movers_shakers_url(category: str = "automotive") -> str:
    encoded = category.strip().replace(" ", "-")
    return f"{AMAZON_BASE_URL}/gp/movers-and-shakers/{encoded}"


def fetch_page_html(page: Page, url: str, page_number: int) -> str:
    last_error: AmazonPageError | None = None

    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        if attempt > 1:
            sleep_with_jitter(2.5, 5.5)

        page.goto(url, wait_until="domcontentloaded")
        try:
            wait_for_results(page)
            html = page.content()
            if has_results(html) or classify_page(html) == "empty":
                return html
        except AmazonPageError as exc:
            last_error = exc
            if attempt == MAX_PAGE_RETRIES:
                break

        page.wait_for_timeout(random.randint(1200, 2600))

    raise AmazonPageError(
        f"Page {page_number} failed after {MAX_PAGE_RETRIES} attempts. {last_error}"
    )


def sleep_with_jitter(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def wait_for_results(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded")
    for _ in range(12):
        html = page.content()
        state = classify_page(html)
        if state == "results":
            return
        if state == "blocked":
            raise AmazonPageError(build_page_error(page, html, "Amazon returned a verification or blocked page."))
        if state == "empty":
            return
        page.wait_for_timeout(2_000)

    html = page.content()
    raise AmazonPageError(
        build_page_error(
            page,
            html,
            "Amazon page results did not load in time.",
        )
    )


def classify_page(html: str) -> str:
    lower_html = html.lower()

    blocked_tokens = [
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "to discuss automated access to amazon data",
        "captcha",
        "api-services-support@amazon.com",
    ]
    if any(token in lower_html for token in blocked_tokens):
        return "blocked"

    if has_results(html):
        return "results"

    if "no results" in lower_html:
        return "empty"

    return "loading"


def has_results(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select("[data-asin]"):
        return True
    if soup.select("div.p13n-sc-uncoverable-faceout"):
        return True
    if soup.select("[data-client-recs-list]"):
        return True
    # 支持搜索页面
    if soup.select("[data-component-type='s-search-result']"):
        return True
    if soup.select("div.s-result-item"):
        return True
    return False


def build_page_error(page: Page, html: str, message: str) -> str:
    debug_path = save_debug_html(page, html)
    title = page.title()
    return f"{message} title={title!r} debug_html={debug_path}"


def save_debug_html(page: Page, html: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    slug = sanitize_filename(page.title() or "amazon_page")
    path = DEBUG_DIR / f"{slug}.html"
    path.write_text(html, encoding="utf-8")
    return path.resolve()


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "amazon_page"


def scrape_best_sellers(
    browser: Browser,
    category: str = "automotive",
    pages: int = 1,
    max_items: int | None = None,
) -> List[AmazonSearchProduct]:
    page = browser.new_page()
    page.set_default_timeout(30_000)
    products: List[AmazonSearchProduct] = []

    try:
        for page_number in range(1, pages + 1):
            url = build_best_sellers_url(category, page_number)
            try:
                html = fetch_page_html(page, url, page_number)
            except AmazonPageError:
                if products:
                    return products[:max_items] if max_items is not None else products
                raise
            page_products = parse_best_sellers_results(html, category, page_number)
            products.extend(page_products)
            if max_items is not None and len(products) >= max_items:
                return products[:max_items]
    finally:
        page.close()

    return products


def scrape_movers_shakers(
    browser: Browser,
    category: str = "automotive",
) -> List[AmazonSearchProduct]:
    page = browser.new_page()
    page.set_default_timeout(30_000)

    try:
        url = build_movers_shakers_url(category)
        html = fetch_page_html(page, url, 1)
        return parse_movers_shakers_results(html, category)
    finally:
        page.close()


def parse_best_sellers_results(html: str, category: str, page_number: int) -> List[AmazonSearchProduct]:
    soup = BeautifulSoup(html, "html.parser")
    results: List[AmazonSearchProduct] = []

    # 获取排名信息
    products_data = extract_products_from_json(soup)
    rank_map = {}
    for idx, product_info in enumerate(products_data):
        asin = product_info.get("id", "")
        if asin:
            metadata = product_info.get("metadataMap", {})
            rank = metadata.get("render.zg.rank", str(idx + 1))
            rank_map[asin] = rank

    # 优先用新的 [data-asin] 结构（2024年后页面格式）
    cards = soup.select("[data-asin]")

    # 如果没有，尝试旧结构
    if not cards:
        cards = soup.select("div.p13n-sc-uncoverable-faceout")

    for idx, card in enumerate(cards):
        # 获取 ASIN
        asin = card.get("data-asin", "").strip()
        if not asin or len(asin) != 10:
            # 旧结构：从链接提取
            link = card.select_one("a[href*='/dp/']")
            if link:
                href = link.get("href", "")
                match = re.search(r"/dp/([A-Z0-9]{10})", href)
                if match:
                    asin = match.group(1)

        if not asin or len(asin) != 10:
            continue

        # 获取卡片完整文本（包含标题、评分、评论数、价格）
        full_text = card.get_text(" ", strip=True)

        # 提取标题 - 从完整文本中解析
        title = extract_title_from_bs_card(full_text)

        # 提取价格
        price = None
        currency = None
        price_elem = (
            card.select_one("[data-a-color='price'] span") or
            card.select_one("._cDEzb_p13n-sc-price_3mJ9Z") or
            card.select_one("[class*='p13n-sc-price']") or
            card.select_one("span.a-color-price") or
            card.select_one("span.a-offscreen")
        )
        if price_elem:
            price_text = price_elem.get_text(" ", strip=True)
            price = parse_price(price_text)
            currency = detect_currency(price_text)

        # 提取评分
        rating = None
        rating_elem = card.select_one("i span") or card.select_one("span.a-icon-alt")
        if rating_elem:
            rating_text = rating_elem.get_text(" ", strip=True)
            rating = parse_rating(rating_text)

        # 提取评论数
        review_count = None
        review_elem = card.select_one("a[href*='#customerReviews'] span")
        if review_elem:
            review_text = review_elem.get_text(" ", strip=True)
            review_count = parse_review_count(review_text)
        if review_count is None:
            review_count = parse_review_count_from_full_text(full_text)

        # 获取排名
        rank_str = rank_map.get(asin, None)
        if rank_str is None:
            # 从卡片文本中提取排名（如 "#1", "#2"）
            rank_match = re.search(r"#(\d+)", full_text)
            rank_str = rank_match.group(1) if rank_match else str(idx + 1)
        rank = int(rank_str) if rank_str.isdigit() else idx + 1

        # 提取品牌
        brand = extract_brand_from_text(title)

        results.append(
            AmazonSearchProduct(
                keyword=category,
                page_number=page_number,
                position=rank,
                asin=asin,
                is_sponsored=False,
                brand=brand,
                title=title,
                price=price,
                currency=currency,
                rating=rating,
                review_count=review_count,
                badge=f"Best Seller #{rank}",
                bought_info=None,
                url=f"{AMAZON_BASE_URL}/dp/{asin}",
            )
        )

    return results


def parse_movers_shakers_results(html: str, category: str) -> List[AmazonSearchProduct]:
    soup = BeautifulSoup(html, "html.parser")
    results: List[AmazonSearchProduct] = []

    # 获取排名和变化信息
    products_data = extract_products_from_json(soup)

    # 优先用新的 [data-asin] 结构
    cards = soup.select("[data-asin]")
    if not cards:
        cards = soup.select("div.p13n-sc-uncoverable-faceout")

    for idx, card in enumerate(cards):
        # 获取 ASIN
        asin = card.get("data-asin", "").strip()
        if not asin or len(asin) != 10:
            # 优先从JSON获取
            asin = None
            for product_info in products_data:
                asin_link = card.select_one(f"a[href*='/dp/{product_info.get('id')}']")
                if asin_link:
                    asin = product_info.get("id", "")
                    break
            # 如果没找到，从链接提取
            if not asin:
                link = card.select_one("a[href*='/dp/']")
                if link:
                    href = link.get("href", "")
                    match = re.search(r"/dp/([A-Z0-9]{10})", href)
                    if match:
                        asin = match.group(1)

        if not asin or len(asin) != 10:
            continue

        # 从JSON获取飙升信息
        product_info = next((p for p in products_data if p.get("id") == asin), {})
        metadata = product_info.get("metadataMap", {})
        current_rank = metadata.get("render.zg.bsms.currentSalesRank", "")
        percentage_change = metadata.get("render.zg.bsms.percentageChange", "")

        # 获取卡片完整文本
        full_text = card.get_text(" ", strip=True)

        # 提取标题
        title = extract_title_from_bs_card(full_text)

        # 提取价格 - 多种选择器兼容
        price_elem = (
            card.select_one("[data-a-color='price'] span") or
            card.select_one("._cDEzb_p13n-sc-price_3mJ9Z") or
            card.select_one("[class*='p13n-sc-price']") or
            card.select_one("span.a-color-price") or
            card.select_one("span.a-offscreen")
        )
        price_text = price_elem.get_text(" ", strip=True) if price_elem else ""
        price = parse_price(price_text)
        currency = detect_currency(price_text)

        # 提取评分
        rating_elem = card.select_one("i span") or card.select_one("span.a-icon-alt")
        rating_text = rating_elem.get_text(" ", strip=True) if rating_elem else ""
        rating = parse_rating(rating_text)

        # 提取评论数
        review_elem = card.select_one("a[href*='#customerReviews'] span")
        review_text = review_elem.get_text(" ", strip=True) if review_elem else ""
        if not review_text:
            review_text = extract_review_count_from_text(full_text)
        review_count = parse_review_count(review_text)
        if review_count is None:
            review_count = parse_review_count_from_full_text(full_text)

        # 构建badge
        badge = None
        if percentage_change:
            badge = f"{percentage_change}% (Rank: {current_rank})"
        elif current_rank:
            badge = f"Rank: {current_rank}"

        # 提取品牌
        brand = extract_brand_from_text(title)

        results.append(
            AmazonSearchProduct(
                keyword=category,
                page_number=1,
                position=idx + 1,
                asin=asin,
                is_sponsored=False,
                brand=brand,
                title=title,
                price=price,
                currency=currency,
                rating=rating,
                review_count=review_count,
                badge=badge,
                bought_info=None,
                url=f"{AMAZON_BASE_URL}/dp/{asin}",
            )
        )

    return results


def extract_title_from_bs_card(text: str) -> str:
    """从 Best Sellers 卡片完整文本中提取产品标题
    
    卡片文本格式（不同语言）：
    英文: "Amazon Fire TV Stick 4K Plus ... 4.7 out of 5 stars 63,699 $54.99"
    中文: "Amazon Fire TV Stick 4K Plus ... 4.4 颗星，最多 5 颗星 274,393 $11.99"
    """
    # 标准化分隔符
    text = text.replace("|", " ")
    text = re.sub(r"Watch the video\s*Watch", "", text, flags=re.IGNORECASE)
    # 去掉开头的排名号
    text = re.sub(r"^#\d+\s*", "", text)

    # 策略：找到评分+评论数+价格标记的起始位置，截取其前的内容为标题

    # 1. 找价格（"$"开头）
    price_match = re.search(r"\$[\d,.]+", text)
    # 2. 找评论数（通常是5位数以上的大数字，紧跟在评分后）
    review_match = re.search(r"(?:out of 5 stars|颗星)[^\d]*([\d,]{4,})", text)
    # 3. 找评分（如 "4.7" 或 "4.4"）
    rating_match = re.search(r"(\d\.\d)\s*(?:out of 5 stars|颗星)", text)

    # 取最靠前的标记位置
    positions = []
    if price_match:
        positions.append(price_match.start())
    if review_match:
        positions.append(review_match.start())
    if rating_match:
        positions.append(rating_match.start())

    if positions:
        cutoff = min(positions)
        title = text[:cutoff].strip()
        title = clean_text(title)
        if len(title) > 5:
            return title

    # 兜底：去掉排名号后取前120字符
    text = re.sub(r"^#\d+\s*", "", text)
    return clean_text(text[:120])


def extract_product_title_from_text(text: str) -> str:
    """从包含评分、评论数等信息的文本中提取产品标题"""
    # 移除 "Watch the video Watch" 等视频相关文字
    text = re.sub(r"Watch the video\s*Watch", "", text, flags=re.IGNORECASE)

    # 尝试匹配常见的模式
    # 例如: "Drift Car Air Freshener ... 3.8 out of 5 stars 18,609 $12.95"
    patterns = [
        r"^([^$]+?)\s+\d[\d,]*\s*out of 5 stars",
        r"^([^$]+?)\s+\d+\.\d+\s*out of 5 stars",
        r"^(.+?)\s+\d+[,\d]*\s*\$",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            title = clean_text(match.group(1))
            if len(title) > 5:
                return title

    # 如果没匹配到，返回前100个字符
    return clean_text(text[:100])


def extract_review_count_from_text(text: str) -> str:
    """从文本中提取评论数"""
    match = re.search(r"([\d,]+)\s*$", text)
    if match:
        return match.group(1).replace(",", "")

    # 匹配 "18,609" 格式的数字
    match = re.search(r"([\d,]+)\s+[\$€£]", text)
    if match:
        return match.group(1).replace(",", "")

    return ""


def parse_review_count_from_full_text(text: str) -> Optional[int]:
    """从 Best Sellers 卡片完整文本中提取评论数"""
    text = text.replace("|", " ")
    # 格式: "out of 5 stars  274,393"
    match = re.search(r"out of 5 stars\s+([\d,]+)", text)
    if match:
        num = int(match.group(1).replace(",", ""))
        if num > 100:  # 评论数通常大于100
            return num

    # 格式: "274,393 $11.99" (评论数在价格前)
    match = re.search(r"([\d,]+)\s+\$", text)
    if match:
        num = int(match.group(1).replace(",", ""))
        if num > 100:
            return num

    return None


def extract_products_from_json(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    grid_elements = soup.select("[data-client-recs-list]")
    for element in grid_elements:
        json_str = element.get("data-client-recs-list", "")
        if json_str:
            try:
                data = json.loads(json_str)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
    return []


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_brand_from_text(text: str) -> Optional[str]:
    patterns = [
        r"Visit the ([A-Za-z0-9&' -]{2,40}) Store",
        r"Brand[: ]+([A-Za-z0-9&' -]{2,40})",
        r"\bby ([A-Za-z0-9&' -]{2,40})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return None


def parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d[\d,]*\.?\d*)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def detect_currency(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    if "$" in text:
        return "USD"
    return None


def parse_rating(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return float(match.group(1))


def parse_review_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    # 移除括号
    text = text.replace("(", "").replace(")", "")
    text = text.replace(",", "")
    # 匹配数字和K/M后缀
    match = re.search(r"([\d.]+)\s*([KM])?", text, flags=re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    multiplier = 1
    if suffix and suffix.upper() == "K":
        multiplier = 1_000
    elif suffix and suffix.upper() == "M":
        multiplier = 1_000_000
    return int(number * multiplier)


# ============== URL 直接抓取功能 ==============

def classify_url(url: str) -> str:
    """
    根据 URL 判断页面类型。

    Returns:
        "best_sellers" | "movers_shakers" | "search" | "unknown"
    """
    lower = url.lower()
    if "/zgbs/" in lower or "/best-sellers" in lower or "/bestsellers" in lower:
        return "best_sellers"
    if "/movers-and-shakers" in lower or "/movers_shakers" in lower:
        return "movers_shakers"
    if "/s?" in lower or "/s/" in lower:
        return "search"
    return "unknown"


def extract_page_number_from_url(url: str) -> int:
    """从 URL 中提取页码参数，默认返回 1"""
    import re as _re
    m = _re.search(r"[?&]pg=(\d+)", url)
    if m:
        return int(m.group(1))
    m = _re.search(r"[?&]page=(\d+)", url)
    if m:
        return int(m.group(1))
    return 1


def scrape_by_url(
    browser: Browser,
    url: str,
    pages: int = 1,
    max_items: int | None = None,
) -> List[AmazonSearchProduct]:
    """
    根据输入的 Amazon URL 自动识别页面类型并抓取商品数据。

    支持的 URL 类型：
    - Best Sellers 榜单页
    - Movers & Shakers 飙升榜页
    - 关键词搜索结果页

    Args:
        browser: Playwright Browser 实例
        url: Amazon 页面 URL
        pages: 从该 URL 开始再抓取几页（默认只抓该 URL 本身）
        max_items: 最多抓取商品数

    Returns:
        AmazonSearchProduct 列表
    """
    page_type = classify_url(url)
    start_page = extract_page_number_from_url(url)
    products: List[AmazonSearchProduct] = []

    pw_page = browser.new_page()
    pw_page.set_default_timeout(30_000)

    try:
        if page_type == "best_sellers":
            # 从 URL 中提取类目
            category = _extract_category_from_bs_url(url)
            for page_num in range(start_page, start_page + pages):
                if page_num == start_page:
                    page_url = url
                else:
                    # 构建下一页 URL
                    if "?" in url:
                        base_url = url.split("?")[0]
                    else:
                        base_url = url
                    encoded = category.strip().replace(" ", "-")
                    page_url = f"{AMAZON_BASE_URL}/Best-Sellers-{encoded}/zgbs/{encoded}/ref=zg_bs_pg_{page_num}?_encoding=UTF8&pg={page_num}"

                try:
                    html = fetch_page_html(pw_page, page_url, page_num)
                except AmazonPageError:
                    if products:
                        break
                    raise
                page_products = parse_best_sellers_results(html, category, page_num)
                products.extend(page_products)
                if max_items is not None and len(products) >= max_items:
                    products = products[:max_items]
                    break

                if page_num < start_page + pages - 1:
                    sleep_with_jitter(2, 4)

        elif page_type == "movers_shakers":
            category = _extract_category_from_ms_url(url)
            try:
                html = fetch_page_html(pw_page, url, 1)
            except AmazonPageError:
                if products:
                    pass
                else:
                    raise
            else:
                products = parse_movers_shakers_results(html, category)
                if max_items is not None:
                    products = products[:max_items]

        elif page_type == "search":
            # 搜索页面
            keyword = _extract_keyword_from_search_url(url)
            for page_num in range(start_page, start_page + pages):
                if page_num == start_page:
                    page_url = url
                else:
                    # 替换或添加 page 参数
                    if "page=" in page_url:
                        import re as _re
                        page_url = _re.sub(r"page=\d+", f"page={page_num}", url)
                    else:
                        page_url = url + f"&page={page_num}"

                try:
                    html = fetch_page_html(pw_page, page_url, page_num)
                except AmazonPageError:
                    if products:
                        break
                    raise
                page_products = parse_search_results(html, keyword, page_num)
                products.extend(page_products)
                if max_items is not None and len(products) >= max_items:
                    products = products[:max_items]
                    break

                if page_num < start_page + pages - 1:
                    sleep_with_jitter(2, 4)
        else:
            raise ValueError(
                f"无法识别 URL 类型。支持的 URL 类型：\n"
                f"  - Best Sellers: https://www.amazon.com/Best-Sellers-.../zgbs/...\n"
                f"  - Movers & Shakers: https://www.amazon.com/gp/movers-and-shakers/...\n"
                f"  - 搜索结果: https://www.amazon.com/s?k=...\n"
                f"当前 URL: {url}"
            )
    finally:
        pw_page.close()

    return products


def _extract_category_from_bs_url(url: str) -> str:
    """从 Best Sellers URL 中提取类目名称"""
    # /Best-Sellers-Automotive/zgbs/automotive
    m = re.search(r"Best-Sellers-([^/]+)", url, re.IGNORECASE)
    if m:
        return m.group(1).replace("-", " ")
    # /zgbs/automotive
    m = re.search(r"/zgbs/([^/?]+)", url, re.IGNORECASE)
    if m:
        return m.group(1).replace("-", " ")
    return "automotive"


def _extract_category_from_ms_url(url: str) -> str:
    """从 Movers & Shakers URL 中提取类目名称"""
    m = re.search(r"movers-and-shakers/([^/?]+)", url, re.IGNORECASE)
    if m:
        return m.group(1).replace("-", " ")
    return "automotive"


def _extract_keyword_from_search_url(url: str) -> str:
    """从搜索 URL 中提取关键词"""
    m = re.search(r"[?&]k=([^&]+)", url)
    if m:
        from urllib.parse import unquote_plus
        return unquote_plus(m.group(1))
    return "search"


# ============== 关键词搜索功能 ==============

def build_search_url(keyword: str, page: int = 1) -> str:
    """构建搜索URL"""
    encoded = keyword.replace(" ", "+")
    if page > 1:
        return f"{AMAZON_BASE_URL}/s?k={encoded}&page={page}&ref=nb_sb_noss"
    return f"{AMAZON_BASE_URL}/s?k={encoded}&ref=nb_sb_noss"


def scrape_search(
    browser: Browser,
    keyword: str,
    pages: int = 1,
    max_items: int | None = None,
) -> List[AmazonSearchProduct]:
    """抓取关键词搜索结果"""
    page = browser.new_page()
    page.set_default_timeout(30_000)
    products: List[AmazonSearchProduct] = []

    try:
        for page_number in range(1, pages + 1):
            url = build_search_url(keyword, page_number)
            try:
                html = fetch_page_html(page, url, page_number)
            except AmazonPageError:
                if products:
                    return products[:max_items] if max_items is not None else products
                raise
            page_products = parse_search_results(html, keyword, page_number)
            products.extend(page_products)
            if max_items is not None and len(products) >= max_items:
                return products[:max_items]
    finally:
        page.close()

    return products


def parse_search_results(html: str, keyword: str, page_number: int) -> List[AmazonSearchProduct]:
    """解析搜索结果页面"""
    soup = BeautifulSoup(html, "html.parser")
    results: List[AmazonSearchProduct] = []

    # 查找产品卡片
    cards = soup.select("[data-component-type='s-search-result']")

    for idx, card in enumerate(cards):
        # 提取ASIN
        asin = card.get("data-asin", "") or card.get("data-ad-asin", "")
        if not asin:
            link = card.select_one("h2 a")
            if link:
                href = link.get("href", "")
                match = re.search(r"/dp/([A-Z0-9]{10})", href)
                if match:
                    asin = match.group(1)

        if not asin or len(asin) != 10:
            continue

        # 检查是否赞助
        is_sponsored = bool(card.select_one("[data-component-type='s-ad-feedback']"))

        # 提取标题
        title_elem = (
            card.select_one("h2 a span") or
            card.select_one("h2 span") or
            card.select_one("span.a-text-normal")
        )
        title = clean_text(title_elem.get_text(" ", strip=True) if title_elem else "")
        title = extract_product_title_from_text(title)

        # 提取价格
        price_whole = card.select_one("span.a-price-whole")
        price_frac = card.select_one("span.a-price-fraction")
        price_text = ""
        if price_whole:
            price_text = f"${price_whole.get_text(strip=True)}"
            if price_frac:
                price_text += price_frac.get_text(strip=True)
        price = parse_price(price_text)
        currency = detect_currency(price_text)

        # 提取评分
        rating_elem = card.select_one("i span.a-icon-alt") or card.select_one("[class*='a-icon-star'] span")
        rating_text = rating_elem.get_text(" ", strip=True) if rating_elem else ""
        rating = parse_rating(rating_text)

        # 提取评论数
        review_elem = card.select_one("[class*='s-underline-text']")
        if not review_elem:
            review_elem = card.select_one("div.a-section.a-spacing-none.a-spacing-top-micro span")
        review_text = review_elem.get_text(" ", strip=True) if review_elem else ""
        review_count = parse_review_count(review_text)

        # 提取品牌
        brand = extract_brand_from_text(title)

        results.append(
            AmazonSearchProduct(
                keyword=keyword,
                page_number=page_number,
                position=idx + 1,
                asin=asin,
                is_sponsored=is_sponsored,
                brand=brand,
                title=title,
                price=price,
                currency=currency,
                rating=rating,
                review_count=review_count,
                badge=None,
                bought_info=None,
                url=f"{AMAZON_BASE_URL}/dp/{asin}",
            )
        )

    return results
