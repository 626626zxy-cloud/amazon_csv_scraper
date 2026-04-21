from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page

from models import AmazonSearchProduct


# ═══════════════════════════════════════════════════════════════════
# 亚马逊站点配置
# ═══════════════════════════════════════════════════════════════════

MARKETPLACES: Dict[str, Dict[str, str]] = {
    "US": {"domain": "amazon.com",    "currency": "USD", "symbol": "$"},
    "UK": {"domain": "amazon.co.uk",  "currency": "GBP", "symbol": "£"},
    "DE": {"domain": "amazon.de",     "currency": "EUR", "symbol": "€"},
    "FR": {"domain": "amazon.fr",     "currency": "EUR", "symbol": "€"},
    "IT": {"domain": "amazon.it",     "currency": "EUR", "symbol": "€"},
    "ES": {"domain": "amazon.es",     "currency": "EUR", "symbol": "€"},
    "JP": {"domain": "amazon.co.jp",  "currency": "JPY", "symbol": "¥"},
    "CA": {"domain": "amazon.ca",     "currency": "CAD", "symbol": "C$"},
    "AU": {"domain": "amazon.com.au", "currency": "AUD", "symbol": "A$"},
    "MX": {"domain": "amazon.com.mx", "currency": "MXN", "symbol": "MX$"},
    "BR": {"domain": "amazon.com.br", "currency": "BRL", "symbol": "R$"},
    "IN": {"domain": "amazon.in",     "currency": "INR", "symbol": "₹"},
    "SG": {"domain": "amazon.sg",     "currency": "SGD", "symbol": "S$"},
    "AE": {"domain": "amazon.ae",     "currency": "AED", "symbol": "د.إ"},
    "SA": {"domain": "amazon.sa",     "currency": "SAR", "symbol": "﷼"},
    "NL": {"domain": "amazon.nl",     "currency": "EUR", "symbol": "€"},
    "SE": {"domain": "amazon.se",     "currency": "SEK", "symbol": "kr"},
    "PL": {"domain": "amazon.pl",     "currency": "PLN", "symbol": "zł"},
    "BE": {"domain": "amazon.com.be", "currency": "EUR", "symbol": "€"},
}

# Amazon 搜索排序参数映射
SORT_OPTIONS: Dict[str, str] = {
    "relevance":        "",                               # 默认相关度
    "sales-rank":       "&s=exact-aware-popularity-rank",  # 🔥 销量/人气排名
    "price-asc":        "&s=price-asc-rank",               # 价格从低到高
    "price-desc":       "&s=price-desc-rank",              # 价格从高到低
    "review-rank":      "&s=review-rank",                  # 评论数排序
    "date-desc":        "&s=date-desc-rank",               # 最新上架
    "avg-rating":       "&s=avg-rating-rank",              # 平均评分
}

DEBUG_DIR = Path("outputs") / "debug"
MAX_PAGE_RETRIES = 3


class AmazonPageError(RuntimeError):
    pass


# ═══════════════════════════════════════════════════════════════════
# URL 构建
# ═══════════════════════════════════════════════════════════════════

def get_base_url(marketplace: str = "US") -> str:
    """获取站点基础URL"""
    domain = MARKETPLACES.get(marketplace, MARKETPLACES["US"])["domain"]
    return f"https://www.{domain}"


def build_search_url(
    keyword: str,
    page_number: int = 1,
    marketplace: str = "US",
    sort_by: str = "relevance",
    min_price: float | None = None,
    max_price: float | None = None,
) -> str:
    """
    构建 Amazon 搜索 URL

    Args:
        keyword: 搜索关键词
        page_number: 页码
        marketplace: 站点代码（US/UK/DE/JP等）
        sort_by: 排序方式（relevance/price-asc/price-desc/review-rank/date-desc/avg-rating）
        min_price: 最低价格筛选
        max_price: 最高价格筛选
    """
    base = get_base_url(marketplace)
    encoded = quote_plus(keyword.strip())
    url = f"{base}/s?k={encoded}"

    # 排序
    sort_suffix = SORT_OPTIONS.get(sort_by, "")
    if sort_suffix:
        url += sort_suffix

    # 价格区间（Amazon URL 参数）
    if min_price is not None:
        url += f"&rh=p_36%3A{int(min_price * 100)}-"
    if max_price is not None:
        # 如果已有 min_price，追加 max；否则单独设置
        if min_price is not None:
            url = url.rstrip("-") + f"%2C{int(max_price * 100)}"
        else:
            url += f"&rh=p_36%3A-{int(max_price * 100)}"

    # 页码
    if page_number > 1:
        url += f"&page={page_number}"

    return url


# ═══════════════════════════════════════════════════════════════════
# 核心搜索函数
# ═══════════════════════════════════════════════════════════════════

def scrape_keyword(
    browser: Browser,
    keyword: str,
    pages: int = 1,
    max_items: int | None = None,
    marketplace: str = "US",
    sort_by: str = "relevance",
    min_price: float | None = None,
    max_price: float | None = None,
) -> List[AmazonSearchProduct]:
    """
    按关键词搜索 Amazon 商品

    Args:
        browser: Playwright Browser 实例
        keyword: 搜索关键词
        pages: 抓取页数
        max_items: 最多返回商品数
        marketplace: 站点代码（US/UK/DE/JP等）
        sort_by: 排序方式
        min_price: 最低价格筛选
        max_price: 最高价格筛选

    Returns:
        AmazonSearchProduct 列表
    """
    mp_config = MARKETPLACES.get(marketplace, MARKETPLACES["US"])
    base_url = get_base_url(marketplace)

    page = browser.new_page()
    page.set_default_timeout(20_000)
    products: List[AmazonSearchProduct] = []

    try:
        for page_number in range(1, pages + 1):
            url = build_search_url(
                keyword, page_number,
                marketplace=marketplace,
                sort_by=sort_by,
                min_price=min_price,
                max_price=max_price,
            )
            try:
                html = fetch_search_page_html(page, url, page_number)
            except AmazonPageError:
                if products:
                    return products[:max_items] if max_items is not None else products
                raise
            page_products = parse_search_results(
                html, keyword, page_number,
                marketplace=marketplace,
                currency=mp_config["currency"],
                currency_symbol=mp_config["symbol"],
                base_url=base_url,
            )
            products.extend(page_products)
            if max_items is not None and len(products) >= max_items:
                return products[:max_items]
    finally:
        page.close()

    return products


# ═══════════════════════════════════════════════════════════════════
# 页面抓取与状态检测
# ═══════════════════════════════════════════════════════════════════

def fetch_search_page_html(page: Page, url: str, page_number: int) -> str:
    last_error: AmazonPageError | None = None

    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        if attempt > 1:
            sleep_with_jitter(2.5, 5.5)

        page.goto(url, wait_until="domcontentloaded")
        try:
            wait_for_results(page)
            html = page.content()
            if has_search_results(html) or classify_search_page(html) == "empty":
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
    for _ in range(8):
        html = page.content()
        state = classify_search_page(html)
        if state == "results":
            return
        if state == "blocked":
            raise AmazonPageError(build_page_error(page, html, "Amazon returned a verification or blocked page."))
        if state == "empty":
            return
        page.wait_for_timeout(1_500)

    html = page.content()
    raise AmazonPageError(
        build_page_error(
            page,
            html,
            "Amazon search results did not load in time.",
        )
    )


def classify_search_page(html: str) -> str:
    lower_html = html.lower()
    if has_search_results(html):
        return "results"
    blocked_tokens = [
        "enter the characters you see below",
        "sorry, we just need to make sure you're not a robot",
        "to discuss automated access to amazon data",
        "captcha",
        "api-services-support@amazon.com",
    ]
    if any(token in lower_html for token in blocked_tokens):
        return "blocked"
    empty_tokens = [
        "no results for",
        "did not match any products",
        "check each product page for other buying options",
    ]
    if any(token in lower_html for token in empty_tokens):
        return "empty"
    return "loading"


def has_search_results(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select("div[data-component-type='s-search-result'][data-asin]"))


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


# ═══════════════════════════════════════════════════════════════════
# 搜索结果解析
# ═══════════════════════════════════════════════════════════════════

def parse_search_results(
    html: str,
    keyword: str,
    page_number: int,
    marketplace: str = "US",
    currency: str = "USD",
    currency_symbol: str = "$",
    base_url: str | None = None,
) -> List[AmazonSearchProduct]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div[data-component-type='s-search-result'][data-asin]")
    results: List[AmazonSearchProduct] = []

    if base_url is None:
        base_url = get_base_url(marketplace)

    position = 0
    for card in cards:
        asin = (card.get("data-asin") or "").strip()
        if not asin:
            continue

        title_node = card.select_one("h2 span") or card.select_one("h2")
        link_node = (
            card.select_one("a[href*='/dp/'][aria-label]")
            or card.select_one("a[href*='/dp/']")
            or card.select_one("a.a-link-normal[href]")
        )
        title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")
        relative_url = link_node.get("href", "").strip() if link_node else ""
        full_url = normalize_url(relative_url, base_url=base_url)

        if not title or not full_url:
            continue

        position += 1
        brand = extract_brand(card, title)
        whole_price = clean_text_from_selector(card, "span.a-price > span.a-offscreen")
        detected_currency = detect_currency(whole_price, currency_symbol, currency)
        price = parse_price(whole_price)

        rating_text = clean_text_from_selector(card, "span.a-icon-alt")
        rating = parse_rating(rating_text)

        review_text = (
            clean_text_from_selector(card, "a[href*='customerReviews'] span.a-size-base")
            or clean_text_from_selector(card, "a[href*='#customerReviews'] span")
            or extract_review_count_text(card)
        )
        review_count = parse_review_count(review_text)

        badge = first_non_empty(
            clean_text_from_selector(card, "span.a-badge-text"),
            clean_text_from_selector(card, "div.a-row.a-size-small span.a-color-secondary"),
        )
        bought_info = extract_bought_info(card)
        is_sponsored = detect_sponsored(card)

        results.append(
            AmazonSearchProduct(
                keyword=keyword,
                page_number=page_number,
                position=position,
                asin=asin,
                is_sponsored=is_sponsored,
                brand=brand,
                title=title,
                price=price,
                currency=detected_currency,
                rating=rating,
                review_count=review_count,
                badge=badge,
                bought_info=bought_info,
                url=full_url,
                marketplace=marketplace,
            )
        )

    return results


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def clean_text_from_selector(card, selector: str) -> Optional[str]:
    node = card.select_one(selector)
    if not node:
        return None
    return clean_text(node.get_text(" ", strip=True))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_review_count_text(card) -> Optional[str]:
    for node in card.select("a[href*='customerReviews'], a[href*='#customerReviews']"):
        text = clean_text(node.get_text(" ", strip=True))
        if re.fullmatch(r"[\d,.]+[KM]?", text, flags=re.IGNORECASE):
            return text
    return None


def extract_bought_info(card) -> Optional[str]:
    for node in card.select("span, div"):
        text = clean_text(node.get_text(" ", strip=True))
        if re.search(r"\d+[+,]?\s*bought in past month", text, flags=re.IGNORECASE):
            return normalize_bought_info(text)
    return find_text_matching(
        card.get_text(" ", strip=True),
        r"\d+(?:\.\d+)?[KM]?\+?\s*bought in past month",
    )


def normalize_bought_info(text: str) -> str:
    match = re.search(
        r"(\d+(?:\.\d+)?[KM]?\+?)\s*bought in past month",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return clean_text(text)
    return f"{match.group(1)} bought in past month"


def detect_sponsored(card) -> bool:
    for node in card.select("[aria-label], span, a"):
        text = clean_text(node.get_text(" ", strip=True))
        if text.lower() == "sponsored":
            return True

    html = str(card).lower()
    sponsored_tokens = [
        "sponsored",
        "adfeedback",
        "puis-sponsored-label-text",
        "sp-sponsored-result",
    ]
    return any(token in html for token in sponsored_tokens)


def extract_brand(card, title: str) -> Optional[str]:
    candidates = [
        infer_brand_from_title(title),
        extract_brand_from_store_link(card),
        extract_brand_from_card_text(card.get_text(" ", strip=True)),
        clean_text_from_selector(card, "h5.s-line-clamp-1 span"),
        clean_text_from_selector(card, "span.a-size-base-plus.a-color-base"),
    ]
    for candidate in candidates:
        normalized = normalize_brand(candidate)
        if normalized:
            return normalized
    return None


def extract_brand_from_store_link(card) -> Optional[str]:
    for node in card.select("a[href*='/stores/'], a[href*='/shop/']"):
        text = clean_text(node.get_text(" ", strip=True))
        if text:
            return text
    return None


def extract_brand_from_card_text(text: str) -> Optional[str]:
    patterns = [
        r"Visit the ([A-Za-z0-9&' -]{2,40}) Store",
        r"Brand[: ]+([A-Za-z0-9&' -]{2,40})",
        r"\bby ([A-Za-z0-9&' -]{2,40})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def infer_brand_from_title(title: str) -> Optional[str]:
    stop_terms = {
        "dog", "car", "seat", "cover", "pet", "pets", "front", "back", "rear",
        "waterproof", "scratch", "nonslip", "durable", "soft", "hard", "bottom",
        "for", "with", "portable", "large", "small", "medium", "standard",
        "backseat", "extender", "hammock", "protector", "booster", "bench",
    }
    tokens = re.findall(r"[A-Za-z0-9&'-]+", title)
    brand_tokens: List[str] = []

    for token in tokens[:4]:
        if token.lower() in stop_terms:
            break
        if not re.search(r"[A-Za-z]", token):
            break
        brand_tokens.append(token)
        if len(brand_tokens) >= 2 and token.isupper():
            continue
        if len(brand_tokens) >= 2 and token[0].isupper() and token[1:].islower():
            continue
        if len(brand_tokens) == 1 and token.isupper():
            continue
        if len(brand_tokens) >= 2:
            break

    if not brand_tokens:
        return None
    return " ".join(brand_tokens)


def normalize_brand(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    brand = clean_text(value)
    brand = re.sub(r"^(visit the|brand[: ]+|by)\s+", "", brand, flags=re.IGNORECASE)
    brand = re.sub(r"\s+store$", "", brand, flags=re.IGNORECASE)
    lower_brand = brand.lower()
    invalid_phrases = [
        "amazon's choice",
        "overall pick",
        "limited time deal",
        "trusted certifications",
        "fastest delivery",
        "free delivery",
        "delivery tomorrow",
        "products highlighted as",
        "rated 4+ stars",
        "purchased often",
        "returned infrequently",
        "carbon impact",
        "sustainability features",
    ]
    if any(phrase in lower_brand for phrase in invalid_phrases):
        return None
    if not brand:
        return None
    if len(brand) > 40:
        return None
    return brand


def parse_price(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d[\d,]*\.?\d*)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def detect_currency(text: Optional[str], symbol: str = "$", default_currency: str = "USD") -> Optional[str]:
    """
    检测货币类型

    Args:
        text: 价格文本
        symbol: 当前站点的货币符号
        default_currency: 当前站点的默认货币代码
    """
    if not text:
        return None
    # 如果文本中有货币符号，直接返回站点默认货币
    if symbol in text or "$" in text or "£" in text or "€" in text or "¥" in text:
        return default_currency
    return default_currency


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
    match = re.search(r"(\d+(?:[.,]\d+)?)([KM]?)", text, flags=re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2).upper()
    multiplier = 1
    if suffix == "K":
        multiplier = 1_000
    elif suffix == "M":
        multiplier = 1_000_000
    return int(number * multiplier)


def normalize_url(url: str, base_url: str | None = None) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = base_url or get_base_url("US")
    return f"{base}{url}"


def first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def find_text_matching(text: str, pattern: str) -> Optional[str]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return clean_text(match.group(0))
