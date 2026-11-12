import sqlite3
import re
import uuid
import json
from pathlib import Path

db_path = Path(__file__).resolve().parent / "chichas.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Create tables
cur.executescript("""
CREATE TABLE IF NOT EXISTS restaurants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    timezone TEXT NOT NULL DEFAULT 'America/New_York',
    hours TEXT NOT NULL DEFAULT '{}',
    orders_paused BOOLEAN NOT NULL DEFAULT 0,
    pause_reason TEXT,
    pickup_cutoff_minutes INTEGER NOT NULL DEFAULT 0,
    tax_rate_bps INTEGER NOT NULL DEFAULT 750,
    tax_configured BOOLEAN NOT NULL DEFAULT 1,
    menu_approved BOOLEAN NOT NULL DEFAULT 1,
    enabled_for_live_orders BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS menu_categories (
    id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(restaurant_id, name)
);

CREATE TABLE IF NOT EXISTS menu_items (
    id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    category_id TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    available BOOLEAN NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT 1,
    spice_enabled BOOLEAN NOT NULL DEFAULT 0,
    special_instructions_allowed BOOLEAN NOT NULL DEFAULT 1,
    review_note TEXT,
    source_page INTEGER,
    unavailable_reason TEXT,
    availability_updated_at TEXT,
    availability_updated_by TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT,
    phone_e164 TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    customer_id TEXT,
    cart_id TEXT,
    reference TEXT UNIQUE NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
    subtotal_cents INTEGER NOT NULL,
    tax_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    pickup_eta TEXT,
    accepted_by TEXT,
    customer_confirmed_at TEXT,
    pos_entry_confirmed BOOLEAN NOT NULL DEFAULT 0,
    status_updated_at TEXT,
    notification_consent BOOLEAN NOT NULL DEFAULT 0,
    is_test BOOLEAN NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    item_name_snapshot TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    modifiers_snapshot TEXT NOT NULL DEFAULT '{}',
    special_instructions TEXT
);

CREATE TABLE IF NOT EXISTS order_events (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS menu_availability_events (
    id TEXT PRIMARY KEY,
    menu_item_id TEXT NOT NULL,
    previous_available BOOLEAN NOT NULL,
    new_available BOOLEAN NOT NULL,
    reason TEXT,
    actor TEXT NOT NULL,
    changed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(order_id, event_type)
);
""")

# Insert restaurant
rest_id = str(uuid.uuid4())
hours_json = json.dumps({
    "mon": "11:00-21:30", "tue": "11:00-21:30", "wed": "11:00-21:30",
    "thu": "11:00-21:30", "fri": "10:00-22:00", "sat": "10:00-22:00",
    "sun": "10:00-21:30"
})

cur.execute("""
INSERT OR IGNORE INTO restaurants (id, name, address, timezone, hours, enabled_for_live_orders, menu_approved, tax_configured, tax_rate_bps)
VALUES (?, 'Chicha''s', '304 Ashourian Ave Ste 108, St. Augustine, FL 32092', 'America/New_York', ?, 1, 1, 1, 750)
""", (rest_id, hours_json))

# Get restaurant id
cur.execute("SELECT id FROM restaurants WHERE name = 'Chicha''s' LIMIT 1")
row = cur.fetchone()
if row:
    rest_id = row[0]

# Parse seed_menu_draft.sql for categories and items
sql_file = Path(__file__).resolve().parent / "supabase" / "seed_menu_draft.sql"
if sql_file.exists():
    content = sql_file.read_text(encoding="utf-8")
    
    # Extract categories
    cat_matches = re.findall(r"INSERT INTO menu_categories.*?SELECT id,'([^']+)'", content)
    cat_map = {}
    for cat_name in set(cat_matches):
        cat_id = str(uuid.uuid4())
        cur.execute("INSERT OR IGNORE INTO menu_categories (id, restaurant_id, name) VALUES (?, ?, ?)", (cat_id, rest_id, cat_name))
        cur.execute("SELECT id FROM menu_categories WHERE restaurant_id = ? AND name = ?", (rest_id, cat_name))
        cat_map[cat_name] = cur.fetchone()[0]

    # Extract items:
    # INSERT INTO menu_items(restaurant_id,category_id,sku,name,price_cents,active,spice_enabled,special_instructions_allowed,review_note,source_page) SELECT r.id,c.id,'CHI-001','Veg Manchow Soup',599,true,false,true,'Confirm...',1 FROM restaurants r JOIN menu_categories c ON c.restaurant_id=r.id AND c.name='Soups' ...
    item_pattern = re.compile(
        r"SELECT r\.id,c\.id,'([^']+)','([^']+)',(\d+),(true|false),(true|false),(true|false),(NULL|'[^']*'),(\d+)\s+FROM restaurants r JOIN menu_categories c ON c\.restaurant_id=r\.id AND c\.name='([^']+)'",
        re.MULTILINE
    )
    items_added = 0
    for match in item_pattern.finditer(content):
        sku, name, price, active, spice, special, review, page, cat_name = match.groups()
        cat_id = cat_map.get(cat_name)
        if not cat_id:
            continue
        review_val = None if review == "NULL" else review.strip("'")
        item_id = str(uuid.uuid4())
        cur.execute("""
        INSERT OR IGNORE INTO menu_items (
            id, restaurant_id, category_id, sku, name, price_cents, active, available,
            spice_enabled, special_instructions_allowed, review_note, source_page
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (
            item_id, rest_id, cat_id, sku, name, int(price),
            1 if active == "true" else 0,
            1 if spice == "true" else 0,
            1 if special == "true" else 0,
            review_val, int(page)
        ))
        items_added += 1

    conn.commit()
    print(f"Seeded SQLite database: {items_added} menu items across {len(cat_map)} categories.")

cur.execute("SELECT COUNT(*) FROM menu_items")
print(f"Total items in menu_items: {cur.fetchone()[0]}")
conn.close()

