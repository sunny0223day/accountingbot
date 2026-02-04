PRAGMA foreign_keys = ON;

-------------------------------
-- 1) 訂單（每張單一筆）
-------------------------------
CREATE TABLE IF NOT EXISTS orders (
  order_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at    TEXT NOT NULL,              -- ISO8601 時間
  vendor        TEXT NOT NULL,              -- 店家/團名
  note          TEXT DEFAULT '',
  creator_id    TEXT NOT NULL,              -- Discord user id（開單人）
  payer_id      TEXT NOT NULL,              -- 付款人（通常同 creator）
  discount_type TEXT NOT NULL DEFAULT 'none',  -- none | percent | amount
  discount_value REAL NOT NULL DEFAULT 0,      -- percent: 0.9, amount: 50
  status        TEXT NOT NULL DEFAULT 'open',   -- open | locked | cancelled
  adjustment INTEGER NOT NULL DEFAULT 0      -- 調整金額（正數代表加，負數代表扣）
);

-------------------------------
-- 2) 品項（誰點了什麼）
-------------------------------
CREATE TABLE IF NOT EXISTS line_items (
  item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL,
  user_id     TEXT NOT NULL,       -- 誰點的
  name        TEXT NOT NULL,       -- 品名
  unit_price  INTEGER NOT NULL CHECK(unit_price >= 0),
  qty         INTEGER NOT NULL DEFAULT 1 CHECK(qty > 0),
  note        TEXT DEFAULT '',
  created_at  TEXT NOT NULL,
  created_by  TEXT NOT NULL,       -- 是誰輸入這筆（可跟 user_id 不同）
  FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
);

-------------------------------
-- 3) 參與者（每張單每人一筆：應付與付款狀態）
-------------------------------
CREATE TABLE IF NOT EXISTS participants (
  order_id   INTEGER NOT NULL,
  user_id    TEXT NOT NULL,
  total_due  INTEGER NOT NULL DEFAULT 0 CHECK(total_due >= 0),
  paid       INTEGER NOT NULL DEFAULT 0 CHECK(paid IN (0,1)),
  paid_at    TEXT,
  paid_to    TEXT,   -- 付給誰（可空，預設 payer）
  PRIMARY KEY(order_id, user_id),
  FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
);

-------------------------------
-- 常用索引（讓查詢更快）
-------------------------------
CREATE INDEX IF NOT EXISTS idx_line_items_order ON line_items(order_id);
CREATE INDEX IF NOT EXISTS idx_participants_user ON participants(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
