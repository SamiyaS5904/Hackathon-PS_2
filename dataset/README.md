# Testing dataset

`offerpilot_dataset_csv.zip` (21.5 MB) holds the full synthetic dataset OfferPilot runs on, as one CSV per table:

| File | What it is |
|---|---|
| customers.csv | 3,000 customers: city, language, signup date, marketing consent, wallet and points balance |
| products.csv | 280 products: category, price, cost (so margin can be computed) |
| orders.csv, order_items.csv | 37,011 orders over 12 months, with coupon and sale-day flags |
| sessions.csv, browse_events.csv | visits, product views, searches, wishlist adds |
| cart_events.csv | cart adds, removals and abandoned carts |
| wallet_txns.csv, points_ledger.csv | wallet top-ups and spends; reward points earned, spent and expiring |
| campaigns.csv, offers_history.csv | 19,663 past campaign offers with a random holdout ("not sent") group, used to train the uplift model |
| calendar.csv | festivals, sale days, paydays |
| customer_groups.csv | the 7 behaviour groups the autopilot uses (built only from visible behaviour) |

Not included on purpose: `customer_traits` (the hidden "true behaviour" used only by the test harness to score
results) and Telegram chat ids.

Money is in integer paise (divide by 100 for rupees). Times are UTC.

The live app already has this data loaded. To rebuild the exact same database locally:

```bash
python -m data.generate --customers 3000 --months 12 --seed 42
```

To run OfferPilot on a real shop's export instead, see `docs/csv_import/` (column mapping + a 50-customer sample).
