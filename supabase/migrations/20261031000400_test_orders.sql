ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_test boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS orders_restaurant_created_idx ON orders(restaurant_id,created_at DESC);
