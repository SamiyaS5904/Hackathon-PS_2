# Running OfferPilot on your own shop's data

OfferPilot normally runs on generated demo data. To try it on a real shop, export three CSV files and import them
into a **new** database. Nothing in the demo database is touched.

## 1. Export
| File | One row per | Required columns | Optional |
|---|---|---|---|
| customers.csv | customer | id, first name | last name, city, signup date, marketing consent, language |
| products.csv | product | id, name, category, **price, cost** | brand, type |
| orders.csv | order **line** | order id, customer id, time, product id, quantity, unit price | order discount, delivery fee, status |
| campaigns.csv (optional) | past offer | customer id, offer type, group (treatment / holdout), expiry | value, category, sent / redeemed time |

Cost is required: without it there's no margin, and without margin OfferPilot can't protect profit.

## 2. Map your columns
Copy `mapping.example.yaml`, then change the right-hand side to your column names. Map your categories to the
five rule categories (electronics, fashion, beauty, home, grocery); each has its own discount cap on the Rules page.

## 3. Import
```bash
python -m data.import_csv --mapping my_mapping.yaml --customers customers.csv --products products.csv --orders orders.csv --db data/brand.db
```
The sample files in this folder are a made-up brand (Glow & Co, 50 customers) that you can import to try it.

## 4. Run
Set `DB_PATH=data/brand.db` in `.env`, then start the API and dashboard as usual.

## What changes on real data
- **No browsing, cart, wallet or points data?** Those signals are empty, and the moments that need them (cart left,
  product interest, points expiring) never fire. Decisions are then based on order habits and margins.
- **No past campaigns → learning mode:**
  - the demo uplift model is never used;
  - every offer's estimated effect is cut by 30% (so fewer offers pass);
  - 10% of the customers who allow marketing are kept in a holdout that gets no offers.

  After a few weeks, comparing the holdout with everyone else shows what offers really add. Export those results
  as campaigns.csv, re-import, and train your own model: `python -m engine.uplift --train`.
- **Sale days:** the calendar uses OfferPilot's Indian sale calendar (Diwali, Big Billion Days and so on). Replace
  the `calendar` table if your brand runs its own sales.
