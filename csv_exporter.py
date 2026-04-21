from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Any

import pandas as pd

from models import AmazonSearchProduct


def export_products(products: Iterable[AmazonSearchProduct], output_path: str) -> Path:
    rows = [product.to_dict() for product in products]
    return export_rows(rows, output_path)


def export_rows(rows: Iterable[Mapping[str, Any]], output_path: str) -> Path:
    row_list = list(rows)
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(row_list).to_csv(path, index=False, encoding="utf-8-sig")
    return path
