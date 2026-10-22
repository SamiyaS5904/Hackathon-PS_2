"""
Product catalog lookups for the engine (read from the database, cached in memory).

Where it sits: used by features.py (favourite items), profit.py (prices and margins), rules.py (does this
product/category exist? what's the category's average margin?) and fallback.py (product names for messages).

Key idea: the rules must check offers against REAL catalog data. An offer on a product that doesn't exist,
or a discount bigger than the product's margin, has to be caught.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    category: str
    subcategory: str
    brand: str
    price_paise: int
    cost_paise: int
    margin_pct: float


@dataclass
class Catalog:
    products: dict[int, Product]
    category_avg_margin_pct: dict[str, float]
    category_avg_price_paise: dict[str, int]

    @property
    def categories(self) -> list[str]:
        return sorted(self.category_avg_margin_pct)

    def get(self, product_id: int | None) -> Product | None:
        return self.products.get(product_id) if product_id is not None else None


def load_catalog(conn: sqlite3.Connection) -> Catalog:
    """Read all products once and pre-compute per-category averages."""
    products = {
        row["id"]: Product(row["id"], row["name"], row["category"], row["subcategory"], row["brand"],
                           row["price_paise"], row["cost_paise"], row["margin_pct"])
        for row in conn.execute("SELECT * FROM products")
    }
    margins: dict[str, list[float]] = {}
    prices: dict[str, list[int]] = {}
    for p in products.values():
        margins.setdefault(p.category, []).append(p.margin_pct)
        prices.setdefault(p.category, []).append(p.price_paise)
    return Catalog(
        products=products,
        category_avg_margin_pct={c: round(sum(v) / len(v), 2) for c, v in margins.items()},
        category_avg_price_paise={c: round(sum(v) / len(v)) for c, v in prices.items()},
    )
