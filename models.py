from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class AmazonSearchProduct:
    keyword: str
    page_number: int
    position: int
    asin: str
    is_sponsored: bool
    brand: Optional[str]
    title: str
    price: Optional[float]
    currency: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    badge: Optional[str]
    bought_info: Optional[str]
    url: str
    marketplace: str = "US"

    def to_dict(self) -> dict:
        return asdict(self)
