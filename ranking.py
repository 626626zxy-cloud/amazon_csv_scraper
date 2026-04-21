from __future__ import annotations

import re
from typing import Iterable


def add_ranking_fields(rows: Iterable[dict]) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        ranked = dict(row)
        ranked["bought_count_est"] = parse_bought_count(row.get("bought_info"))
        ranked["sales_proxy_score"] = build_sales_proxy_score(ranked)
        enriched.append(ranked)
    return enriched


def sort_rows(
    rows: Iterable[dict],
    sort_by: str,
    min_price: float | None = None,
    max_price: float | None = None,
) -> list[dict]:
    """排序并可按价格区间过滤商品"""
    row_list = list(rows)

    # 价格过滤
    if min_price is not None or max_price is not None:
        row_list = [
            r for r in row_list
            if _filter_by_price(r, min_price, max_price)
        ]

    # 排序
    if sort_by == "sales_proxy":
        return sorted(
            row_list,
            key=lambda row: (
                _to_int(row.get("sales_proxy_score")),
                _to_int(row.get("bought_count_est")),
                _to_int(row.get("review_count")),
                _to_float(row.get("rating")),
            ),
            reverse=True,
        )
    if sort_by == "review_count":
        return sorted(
            row_list,
            key=lambda row: (_to_int(row.get("review_count")), _to_float(row.get("rating"))),
            reverse=True,
        )
    if sort_by == "price_low":
        return sorted(
            row_list,
            key=lambda row: (_to_float(row.get("price")) if row.get("price") else 99999),
        )
    if sort_by == "price_high":
        return sorted(
            row_list,
            key=lambda row: (_to_float(row.get("price")) if row.get("price") else 0),
            reverse=True,
        )
    return row_list


def _filter_by_price(row: dict, min_price: float | None, max_price: float | None) -> bool:
    """根据价格区间过滤商品"""
    price = _to_float(row.get("price"))
    if price is None or price == 0:
        return True  # 没有价格信息的不过滤
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


def parse_bought_count(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    match = re.search(r"(\d+(?:\.\d+)?)([KM]?)\+?", text, flags=re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2).upper()
    if suffix == "K":
        number *= 1_000
    elif suffix == "M":
        number *= 1_000_000
    return int(number)


def build_sales_proxy_score(row: dict) -> int:
    bought = parse_bought_count(row.get("bought_info"))
    reviews = _to_int(row.get("review_count"))
    rating = _to_float(row.get("rating"))
    badge_bonus = 0
    badge = str(row.get("badge") or "").lower()
    if "best seller" in badge or "overall pick" in badge:
        badge_bonus = 500
    elif badge:
        badge_bonus = 150
    rating_bonus = int(rating * 100) if rating else 0
    return bought * 10 + reviews + rating_bonus + badge_bonus


def _to_int(value: object) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def _to_float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    return float(value)
