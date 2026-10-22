"""
Catalog and identity building blocks for the synthetic company data.

Where it sits: used only by data/generate.py.
  - build_products()          -> the ~300 products the shop sells, with price, cost and margin
  - build_customer_identity() -> name, gender, age, city, language, device for one customer

Key idea: margins differ a lot by category (electronics thin, fashion fat). That is what makes the
rules interesting: a 10% discount is harmless on a kurta but would wipe out the profit on earphones.
Margins here are chosen so the default category caps in config/rules.yaml leave at least ~10% margin.

Brands are fictional on purpose, so the dataset never looks like a real company's data.
"""

from __future__ import annotations

import random

# ---------------------------------------------------------------------------------------------------
# Product catalog definition
# category -> margin range (%), brands, and subcategories with a price range in rupees
# ---------------------------------------------------------------------------------------------------
CATEGORIES: dict[str, dict] = {
    "electronics": {
        "margin_pct": (18, 28),  # thin margins: discounts must stay small (cap 8%)
        "brands": ["Voltix", "SonicBay", "Nexa", "Zentro", "Pulse"],
        "subcategories": {
            "Earphones": (499, 2999),
            "Smartwatch": (1499, 6999),
            "Power Bank": (699, 2499),
            "Bluetooth Speaker": (999, 4999),
            "Phone Case": (199, 799),
            "Charger": (299, 1499),
            "Keyboard": (599, 2999),
            "Mouse": (299, 1499),
        },
    },
    "fashion": {
        "margin_pct": (40, 60),  # fat margins: bigger discounts are still profitable (cap 30%)
        "brands": ["Urban Thread", "Desi Loom", "StreetFox", "Kaira", "Northline"],
        "subcategories": {
            "T-Shirt": (299, 999),
            "Kurta": (499, 1999),
            "Jeans": (799, 2499),
            "Sneakers": (999, 3999),
            "Saree": (899, 4999),
            "Jacket": (1199, 3999),
            "Handbag": (599, 2999),
            "Sunglasses": (399, 1499),
        },
    },
    "beauty": {
        "margin_pct": (35, 55),
        "brands": ["Glowveda", "PureLeaf", "Aurae", "Mira", "Velvet Co"],
        "subcategories": {
            "Face Wash": (149, 499),
            "Moisturiser": (199, 799),
            "Sunscreen": (249, 899),
            "Lipstick": (199, 999),
            "Shampoo": (149, 599),
            "Perfume": (499, 2499),
            "Hair Oil": (99, 399),
            "Serum": (399, 1499),
        },
    },
    "home": {
        "margin_pct": (28, 45),
        "brands": ["Casa Nova", "Homely", "Nestwell", "Woodmark", "Brightly"],
        "subcategories": {
            "Bedsheet": (499, 1999),
            "Cookware Set": (999, 3999),
            "Water Bottle": (199, 799),
            "Table Lamp": (599, 2499),
            "Storage Box": (249, 999),
            "Towel Set": (399, 1499),
            "Wall Clock": (349, 1299),
            "Dinner Set": (899, 3499),
        },
    },
    "grocery": {
        "margin_pct": (18, 28),  # low margin, high frequency: perfect for habit offers but small discounts
        "brands": ["FreshFarm", "Annapurna", "DailyGood", "Organica", "Tasty Bites"],
        "subcategories": {
            "Basmati Rice": (149, 599),
            "Atta": (199, 499),
            "Cooking Oil": (149, 399),
            "Green Tea": (149, 449),
            "Coffee": (199, 699),
            "Dry Fruits": (299, 999),
            "Snacks": (49, 199),
            "Breakfast Cereal": (149, 449),
        },
    },
}

# How likely each category is to be RE-ordered (used to pick each customer's favourite items).
# People restock rice and shampoo; they rarely buy the same smartwatch twice.
REPEATABILITY: dict[str, float] = {
    "grocery": 3.0,
    "beauty": 2.5,
    "home": 1.0,
    "fashion": 0.8,
    "electronics": 0.3,
}

PRODUCTS_PER_SUBCATEGORY = 7  # 5 categories x 8 subcategories x 7 = 280 products


def build_products(rng: random.Random) -> list[dict]:
    """
    Create the product catalog.

    Returns a list of dicts: id, name, category, subcategory, brand, price_paise, cost_paise, margin_pct.
    Prices are rounded to end in 9 (Rs 499, Rs 1299...) like real Indian e-commerce prices.
    """
    products: list[dict] = []
    next_id = 1
    for category, spec in CATEGORIES.items():
        margin_low, margin_high = spec["margin_pct"]
        for subcategory, (price_low, price_high) in spec["subcategories"].items():
            for variant in range(PRODUCTS_PER_SUBCATEGORY):
                brand = spec["brands"][variant % len(spec["brands"])]
                price_rupees = _round_price(rng.uniform(price_low, price_high))
                margin_pct = round(rng.uniform(margin_low, margin_high), 1)
                price_paise = price_rupees * 100
                cost_paise = round(price_paise * (1 - margin_pct / 100))
                products.append(
                    {
                        "id": next_id,
                        "name": f"{brand} {subcategory} {_VARIANT_NAMES[variant]}",
                        "category": category,
                        "subcategory": subcategory,
                        "brand": brand,
                        "price_paise": price_paise,
                        "cost_paise": cost_paise,
                        # store the margin that results from the rounded cost, so it is exact
                        "margin_pct": round((price_paise - cost_paise) / price_paise * 100, 2),
                    }
                )
                next_id += 1
    return products


_VARIANT_NAMES = ["Classic", "Pro", "Lite", "Plus", "Max", "Neo", "Prime"]


def _round_price(rupees: float) -> int:
    """Round to a 'shop price' ending in 9: 512 -> 509, 1287 -> 1289."""
    value = int(round(rupees / 10.0) * 10) - 1
    return max(value, 49)


# ---------------------------------------------------------------------------------------------------
# Customer identity (no phone numbers: Telegram bots can't use them anyway)
# ---------------------------------------------------------------------------------------------------
FEMALE_FIRST_NAMES = [
    "Aarohi", "Ananya", "Diya", "Isha", "Kavya", "Meera", "Neha", "Pooja", "Priya", "Riya",
    "Sana", "Simran", "Sneha", "Tanvi", "Aditi", "Anjali", "Bhavna", "Deepika", "Divya", "Gauri",
    "Harleen", "Jasleen", "Khushi", "Lavanya", "Mansi", "Nikita", "Pallavi", "Rashmi", "Sakshi", "Shreya",
    "Swati", "Trisha", "Vaishnavi", "Zoya", "Fatima", "Nandini", "Ritika", "Samiya", "Aisha", "Komal",
]
MALE_FIRST_NAMES = [
    "Aarav", "Aditya", "Aman", "Arjun", "Dev", "Harsh", "Ishaan", "Kabir", "Karan", "Manav",
    "Nikhil", "Rahul", "Rohan", "Sahil", "Siddharth", "Varun", "Vikram", "Yash", "Abhishek", "Ankit",
    "Deepak", "Gaurav", "Gurpreet", "Hardik", "Jatin", "Kunal", "Mohit", "Naveen", "Pranav", "Rajat",
    "Sameer", "Tushar", "Uday", "Vivek", "Imran", "Farhan", "Ritesh", "Saurabh", "Tarun", "Akash",
]
LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Singh", "Kumar", "Patel", "Shah", "Mehta", "Reddy", "Nair",
    "Iyer", "Rao", "Das", "Bose", "Chatterjee", "Banerjee", "Joshi", "Kulkarni", "Desai", "Malhotra",
    "Kapoor", "Khanna", "Arora", "Bhatia", "Sehgal", "Gill", "Sandhu", "Khan", "Ansari", "Pillai",
    "Menon", "Agarwal", "Jain", "Mishra", "Pandey", "Tiwari", "Saxena", "Chauhan", "Thakur", "Yadav",
]

# city -> (tier, most common language)
CITIES: dict[str, tuple[int, str]] = {
    "Mumbai": (1, "Hindi"), "Delhi": (1, "Hindi"), "Bengaluru": (1, "Kannada"),
    "Hyderabad": (1, "Telugu"), "Chennai": (1, "Tamil"), "Kolkata": (1, "Bengali"),
    "Pune": (1, "Marathi"), "Ahmedabad": (1, "Gujarati"),
    "Jaipur": (2, "Hindi"), "Lucknow": (2, "Hindi"), "Chandigarh": (2, "Punjabi"),
    "Indore": (2, "Hindi"), "Kochi": (2, "Malayalam"), "Coimbatore": (2, "Tamil"),
    "Nagpur": (2, "Marathi"), "Ludhiana": (2, "Punjabi"), "Surat": (2, "Gujarati"),
    "Bhopal": (2, "Hindi"),
    "Amritsar": (3, "Punjabi"), "Dehradun": (3, "Hindi"), "Mysuru": (3, "Kannada"),
    "Udaipur": (3, "Hindi"), "Guwahati": (3, "Assamese"), "Vijayawada": (3, "Telugu"),
    "Jalandhar": (3, "Punjabi"), "Patna": (3, "Hindi"),
}
# Bigger cities have more online shoppers.
_TIER_WEIGHT = {1: 3.0, 2: 1.6, 3: 1.0}


def build_customer_identity(rng: random.Random, age_range: tuple[int, int]) -> dict:
    """
    Make the non-behavioural details of one customer.

    age_range comes from the persona (e.g. new users skew younger). About half of customers use their
    regional language, the rest English.
    """
    gender = rng.choice(["F", "M"])
    first_name = rng.choice(FEMALE_FIRST_NAMES if gender == "F" else MALE_FIRST_NAMES)
    city = rng.choices(list(CITIES), weights=[_TIER_WEIGHT[t] for t, _ in CITIES.values()])[0]
    tier, regional_language = CITIES[city]
    return {
        "first_name": first_name,
        "last_name": rng.choice(LAST_NAMES),
        "gender": gender,
        "age": rng.randint(*age_range),
        "city": city,
        "city_tier": tier,
        "language": regional_language if rng.random() < 0.5 else "English",
        "device": rng.choices(["android", "ios", "web"], weights=[65, 20, 15])[0],
        "marketing_consent": 1 if rng.random() < 0.92 else 0,  # ~8% opted out: rules must respect it
    }
