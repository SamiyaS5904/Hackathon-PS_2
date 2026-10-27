-- =====================================================================================================
-- OFFERPILOT DATABASE SCHEMA (SQLite)
-- -----------------------------------------------------------------------------------------------------
-- Part 1: COMPANY DATA. What an e-commerce company would already have about its customers.
--         Filled by data/generate.py (synthetic, 3,000 customers, 12 months).
-- Part 2: HIDDEN TRUTH. Each customer's real personality. ONLY data/generate.py and harness/ may read
--         it. The agent, features, rules and dashboard must never touch customer_traits.
-- Part 3: PIPELINE TABLES. Written by OfferPilot itself (decisions, testers, rules versions, day plans).
--
-- Conventions:
--   * Money is INTEGER paise (Rs 1 = 100 paise) to avoid rounding errors.
--   * Timestamps are TEXT in UTC, ISO format "YYYY-MM-DDTHH:MM:SSZ". The dashboard shows them in IST.
--   * JSON columns are TEXT holding JSON.
-- =====================================================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------------------------------
-- PART 1: COMPANY DATA
-- ---------------------------------------------------------------------------------------------------

-- Who the customers are. No phone numbers (not needed: Telegram bots can't message phone numbers).
CREATE TABLE IF NOT EXISTS customers (
    id                INTEGER PRIMARY KEY,
    first_name        TEXT NOT NULL,
    last_name         TEXT NOT NULL,
    gender            TEXT NOT NULL,          -- 'F' / 'M'
    age               INTEGER NOT NULL,
    city              TEXT NOT NULL,
    city_tier         INTEGER NOT NULL,       -- 1 = metro, 2, 3
    signup_date       TEXT NOT NULL,          -- YYYY-MM-DD
    device            TEXT NOT NULL,          -- 'android' / 'ios' / 'web'
    language          TEXT NOT NULL,
    marketing_consent INTEGER NOT NULL,       -- 1 = agreed to receive offers
    telegram_chat_id  INTEGER                 -- NULL unless a real tester is linked to this customer
);

-- What the shop sells. cost + margin let us check that an offer never loses money.
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,                -- electronics / fashion / beauty / home / grocery
    subcategory TEXT NOT NULL,
    brand       TEXT NOT NULL,
    price_paise INTEGER NOT NULL,
    cost_paise  INTEGER NOT NULL,
    margin_pct  REAL NOT NULL                 -- (price - cost) / price * 100
);

-- One visit to the app/website.
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    device      TEXT NOT NULL,
    source      TEXT NOT NULL                 -- 'organic' / 'notification' / 'ad'
);

-- What they looked at during a visit.
CREATE TABLE IF NOT EXISTS browse_events (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,                -- 'view' / 'search' / 'wishlist_add'
    product_id  INTEGER REFERENCES products(id),
    query       TEXT                          -- search text, only for 'search'
);

-- Cart activity. 'abandon' = left the app with items still in the cart.
CREATE TABLE IF NOT EXISTS cart_events (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    customer_id      INTEGER NOT NULL REFERENCES customers(id),
    ts               TEXT NOT NULL,
    event_type       TEXT NOT NULL,           -- 'add' / 'remove' / 'checkout' / 'abandon'
    product_id       INTEGER REFERENCES products(id),
    qty              INTEGER NOT NULL DEFAULT 1,
    cart_value_paise INTEGER NOT NULL         -- total cart value after this event
);

-- Past marketing campaigns (offers sent before OfferPilot existed).
CREATE TABLE IF NOT EXISTS campaigns (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    start_date TEXT NOT NULL,
    offer_type TEXT NOT NULL,
    value      REAL NOT NULL,
    target     TEXT NOT NULL                  -- category name, or 'favourite' (each customer's top category)
);

-- Orders and their items.
CREATE TABLE IF NOT EXISTS orders (
    id                 INTEGER PRIMARY KEY,
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    session_id         INTEGER REFERENCES sessions(id),
    ts                 TEXT NOT NULL,
    gross_paise        INTEGER NOT NULL,      -- items total before discount
    discount_paise     INTEGER NOT NULL,      -- discount from an offer (0 if none)
    delivery_fee_paise INTEGER NOT NULL,      -- what the customer paid for delivery
    net_paise          INTEGER NOT NULL,      -- what the customer paid in total
    offer_id           INTEGER REFERENCES offers_history(id),
    payment_method     TEXT NOT NULL,         -- 'wallet' / 'upi' / 'card' / 'cod'
    status             TEXT NOT NULL,         -- 'delivered' / 'cancelled' / 'returned'
    points_earned      INTEGER NOT NULL DEFAULT 0,
    points_redeemed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    product_id       INTEGER NOT NULL REFERENCES products(id),
    qty              INTEGER NOT NULL,
    unit_price_paise INTEGER NOT NULL
);

-- Wallet money movements. balance_after lets anyone see the balance at any time.
CREATE TABLE IF NOT EXISTS wallet_txns (
    id                  INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customers(id),
    ts                  TEXT NOT NULL,
    txn_type            TEXT NOT NULL,        -- 'topup' / 'spend' / 'refund' / 'cashback'
    amount_paise        INTEGER NOT NULL,     -- always positive; txn_type says the direction
    balance_after_paise INTEGER NOT NULL,
    order_id            INTEGER REFERENCES orders(id)
);

-- Reward points. Each 'earn' row expires 90 days later unless redeemed first.
CREATE TABLE IF NOT EXISTS points_ledger (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ts          TEXT NOT NULL,
    entry_type  TEXT NOT NULL,                -- 'earn' / 'redeem' / 'expire'
    points      INTEGER NOT NULL,             -- always positive; entry_type says the direction
    expires_at  TEXT,                         -- only for 'earn'
    order_id    INTEGER REFERENCES orders(id)
);

-- Every past offer, including the HOLDOUT group (picked at random, NOT sent).
-- Comparing treatment vs holdout shows who buys BECAUSE of an offer vs who buys anyway.
CREATE TABLE IF NOT EXISTS offers_history (
    id              INTEGER PRIMARY KEY,
    campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    offer_type      TEXT NOT NULL,
    value           REAL NOT NULL,
    category        TEXT,
    min_order_paise INTEGER NOT NULL,
    group_name      TEXT NOT NULL,            -- 'treatment' (sent) / 'holdout' (not sent)
    sent_at         TEXT,                     -- NULL for holdout
    expires_at      TEXT NOT NULL,
    opened_at       TEXT,
    clicked_at      TEXT,
    redeemed_at     TEXT,
    order_id        INTEGER REFERENCES orders(id),
    purchased_in_window INTEGER NOT NULL DEFAULT 0  -- 1 if they ordered before expiry (with or without the offer)
);

-- Festivals, paydays, weekends. Used to explain spikes in buying.
CREATE TABLE IF NOT EXISTS calendar (
    date          TEXT PRIMARY KEY,           -- YYYY-MM-DD
    is_weekend    INTEGER NOT NULL,
    is_payday     INTEGER NOT NULL,
    is_festival   INTEGER NOT NULL,
    festival_name TEXT
);

-- ---------------------------------------------------------------------------------------------------
-- PART 2: HIDDEN TRUTH (never shown to the agent)
-- ---------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customer_traits (
    customer_id           INTEGER PRIMARY KEY REFERENCES customers(id),
    persona               TEXT NOT NULL,
    price_sensitivity     REAL NOT NULL,      -- 0..1, how much price matters
    deal_affinity         REAL NOT NULL,      -- 0..1, how strongly sales/offers pull them in
    churn_propensity      REAL NOT NULL,      -- 0..1, how likely the habit breaks
    spam_tolerance        REAL NOT NULL,      -- 0..1, how many messages before they get annoyed
    base_order_gap_days   REAL NOT NULL,      -- their natural rhythm between orders
    habit_regularity      REAL NOT NULL,      -- 0..1, how regular that rhythm is
    cart_abandon_prob     REAL NOT NULL,
    peak_hour             INTEGER NOT NULL,   -- hour of day (IST) they usually shop
    category_weights      TEXT NOT NULL,      -- JSON {category: weight}
    favourite_product_ids TEXT NOT NULL,      -- JSON [product ids they love to reorder]
    habit_break_date      TEXT,               -- date their habit started breaking (NULL = never)
    true_uplift           TEXT NOT NULL,      -- JSON {offer_type: extra buy chance during an offer}
    wait_for_deal         REAL NOT NULL DEFAULT 0  -- learned habit of waiting for coupons at data end (data/habits.py)
);

-- ---------------------------------------------------------------------------------------------------
-- PART 3: PIPELINE TABLES (written by OfferPilot)
-- ---------------------------------------------------------------------------------------------------

-- Every decision the pipeline makes, with the full "why" for the marketing manager.
CREATE TABLE IF NOT EXISTS decisions (
    id                    INTEGER PRIMARY KEY,
    customer_id           INTEGER NOT NULL REFERENCES customers(id),
    created_at            TEXT NOT NULL,
    trigger_type          TEXT NOT NULL,      -- 'cart_abandoned' / 'habit_break' / 'manual' / ...
    trigger_detail        TEXT,               -- JSON
    decided_by            TEXT NOT NULL,      -- 'agent' / 'fallback' / 'gatekeeper'
    status                TEXT NOT NULL,      -- 'sent' / 'would_send' / 'scheduled' / 'blocked' / 'nothing' / 'cancelled'
    proposal              TEXT,               -- JSON: what the agent first proposed
    final_offer           TEXT,               -- JSON: offer after rule checks/repairs
    message               TEXT,
    send_at               TEXT,
    reasoning             TEXT,               -- JSON: summary, signals_used, alternatives_considered, expected_impact, why_now
    rule_checks           TEXT,               -- JSON list: rule, result (passed/repaired/failed), detail
    expected_profit_paise INTEGER,
    offer_cost_paise      INTEGER,
    rules_version         INTEGER,
    tool_trace            TEXT,               -- JSON list of tool calls the agent made
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cost_usd              REAL,
    sent_at               TEXT,
    telegram_message_id   INTEGER,
    offer_kind            TEXT DEFAULT 'normal', -- 'normal' / 'winback' (relies on retention value; own budget)
    variant_id            TEXT,               -- which message variant was sent (to compare reply rates later)
    variants              TEXT,               -- JSON list of the message variants the agent wrote
    sent_via              TEXT                -- which category bot sent it (config/bots.yaml), or 'main'
);

-- Real people testing on Telegram, each linked to one customer profile (re-linkable).
CREATE TABLE IF NOT EXISTS testers (
    id                INTEGER PRIMARY KEY,
    telegram_username TEXT NOT NULL UNIQUE,
    link_token        TEXT NOT NULL UNIQUE,
    customer_id       INTEGER REFERENCES customers(id),
    chat_id           INTEGER,
    created_at        TEXT NOT NULL,
    linked_at         TEXT
);

-- Every saved version of the rules. Exactly one row has is_active = 1.
CREATE TABLE IF NOT EXISTS rules_versions (
    version    INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    note       TEXT,
    rules_json TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 0
);

-- Settings that belong to this database (e.g. learning mode for an imported real shop). Key/value text.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Learning holdout (real shops without past campaigns): customers kept out of offers on purpose, so the shop can
-- later measure what offers really add and train its own uplift model.
CREATE TABLE IF NOT EXISTS holdout_assignments (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    grp         TEXT NOT NULL,                -- 'holdout' (never offered) / 'eligible'
    assigned_at TEXT NOT NULL
);

-- Imported shops: the brand's own ids (often text like "C-1042") mapped to our integer ids.
CREATE TABLE IF NOT EXISTS id_map (
    entity      TEXT NOT NULL,                -- 'customer' / 'product' / 'order'
    external_id TEXT NOT NULL,
    internal_id INTEGER NOT NULL,
    PRIMARY KEY (entity, external_id)
);

-- "Plan your day": how many AI decisions are allowed on each date (API cost control).
CREATE TABLE IF NOT EXISTS day_plans (
    date                  TEXT PRIMARY KEY,   -- YYYY-MM-DD (IST)
    agent_decision_limit  INTEGER NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- ---------------------------------------------------------------------------------------------------
-- INDEXES (fast lookups by customer and time)
-- ---------------------------------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_sessions_customer   ON sessions(customer_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_browse_customer     ON browse_events(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_cart_customer       ON cart_events(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_orders_customer     ON orders(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_items_order         ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_wallet_customer     ON wallet_txns(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_points_customer     ON points_ledger(customer_id, ts);
CREATE INDEX IF NOT EXISTS idx_offers_customer     ON offers_history(customer_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_decisions_customer  ON decisions(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_created   ON decisions(created_at);
