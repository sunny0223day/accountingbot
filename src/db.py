from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------- Paths ----------
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "app.sqlite3"


def now_iso() -> str:
    # SQLite 用 TEXT 存 ISO8601，簡單又好用
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


# ---------- Data models ----------
@dataclass
class LineItem:
    item_id: int
    order_id: int
    user_id: str
    name: str
    unit_price: int
    qty: int
    note: str


# ---------- Core DB actions ----------
def create_order(
    vendor: str,
    creator_id: str,
    payer_id: Optional[str] = None,
    note: str = "",
) -> int:
    """
    建立一張新單，回傳 order_id。
    payer_id 若不填，預設等於 creator_id。
    """
    if not payer_id:
        payer_id = creator_id

    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (created_at, vendor, note, creator_id, payer_id, discount_type, discount_value, status)
            VALUES (?, ?, ?, ?, ?, 'none', 0, 'open')
            """,
            (now_iso(), vendor, note, creator_id, payer_id),
        )
        order_id = int(cur.lastrowid)
        conn.commit()
        return order_id


def add_item(
    order_id: int,
    user_id: str,
    name: str,
    unit_price: int,
    qty: int = 1,
    note: str = "",
    created_by: Optional[str] = None,
) -> int:
    """
    新增一筆品項，回傳 item_id。新增後會自動 recalc_order。
    """
    if created_by is None:
        created_by = user_id

    if qty <= 0:
        raise ValueError("qty 必須 > 0")
    if unit_price < 0:
        raise ValueError("unit_price 必須 >= 0")

    with connect() as conn:
        _ensure_order_editable(conn, order_id)

        cur = conn.execute(
            """
            INSERT INTO line_items (order_id, user_id, name, unit_price, qty, note, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order_id, user_id, name, unit_price, qty, note, now_iso(), created_by),
        )
        item_id = int(cur.lastrowid)

        # 重新計算 participants.total_due
        recalc_order_conn(conn, order_id)

        conn.commit()
        return item_id


def set_discount_percent(order_id: int, percent: float) -> None:
    """
    設定整張單折扣（例如 0.9 代表打九折），並重新計算。
    MVP 先支援 percent；amount 之後再加。
    """
    if not (0 <= percent <= 1.0):
        raise ValueError("percent 必須在 0 ~ 1 之間，例如 0.9")

    with connect() as conn:
        _ensure_order_editable(conn, order_id)

        conn.execute(
            """
            UPDATE orders
            SET discount_type='percent', discount_value=?
            WHERE order_id=?
            """,
            (percent, order_id),
        )
        recalc_order_conn(conn, order_id)
        conn.commit()

def set_adjustment(order_id: int, adjustment: int, actor_id: str) -> None:
    """
    設定「每人矯正金額」（可正可負），僅開單者可用。
    計算順序：先 discount，再 adjustment（在 recalc_order_conn 內處理）。
    """
    with connect() as conn:
        order = _get_order_row(conn, order_id)

        if order["status"] == "cancelled":
            raise ValueError("此訂單已作廢，不能設定矯正。")
        if order["creator_id"] != actor_id:
            raise ValueError("只有開單的人可以設定矯正金額。")

        conn.execute(
            "UPDATE orders SET adjustment=? WHERE order_id=?",
            (int(adjustment), int(order_id)),
        )

        # 重新計算 participants.total_due
        recalc_order_conn(conn, order_id)
        conn.commit()


def cancel_order(order_id: int, actor_id: str) -> None:
    """
    作廢整張單（status=cancelled），僅開單者可用。
    cancelled 單不計入 /debt，且不可再修改/解鎖。
    """
    with connect() as conn:
        order = _get_order_row(conn, order_id)

        if order["status"] == "cancelled":
            raise ValueError("此訂單已作廢。")
        if order["creator_id"] != actor_id:
            raise ValueError("只有開單的人可以作廢此訂單。")

        conn.execute("UPDATE orders SET status='cancelled' WHERE order_id=?", (order_id,))
        conn.commit()


def mark_paid(order_id: int, user_id: str, paid_to: Optional[str] = None) -> None:
    """
    將某人在某張單標記為已付。
    """
    with connect() as conn:
        # 如果 participants 還沒建立，先 recalc 會建立
        recalc_order_conn(conn, order_id)

        payer_id = conn.execute(
            "SELECT payer_id FROM orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if payer_id is None:
            raise ValueError(f"找不到 order_id={order_id}")
        default_paid_to = payer_id["payer_id"]
        if paid_to is None:
            paid_to = default_paid_to

        # 先確認這個人有在 participants
        row = conn.execute(
            "SELECT total_due, paid FROM participants WHERE order_id=? AND user_id=?",
            (order_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError("這個 user 在此單沒有任何品項，無法付款。")

        conn.execute(
            """
            UPDATE participants
            SET paid=1, paid_at=?, paid_to=?
            WHERE order_id=? AND user_id=?
            """,
            (now_iso(), paid_to, order_id, user_id),
        )
        conn.commit()


# ---------- Queries ----------
def get_bill(order_id: int) -> Dict[str, Any]:
    """
    取得整張單的帳單資料：
    - order metadata
    - 每個人的品項清單、subtotal、total_due、paid
    """
    with connect() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None:
            raise ValueError(f"找不到 order_id={order_id}")

        # 確保 participants 是最新的
        recalc_order_conn(conn, order_id)

        items = conn.execute(
            """
            SELECT item_id, order_id, user_id, name, unit_price, qty, note
            FROM line_items
            WHERE order_id=?
            ORDER BY user_id, item_id
            """,
            (order_id,),
        ).fetchall()

        parts = conn.execute(
            """
            SELECT order_id, user_id, total_due, paid, paid_at, COALESCE(paid_to, '') AS paid_to
            FROM participants
            WHERE order_id=?
            ORDER BY user_id
            """,
            (order_id,),
        ).fetchall()

    # 組合資料（不在 conn 內做太多）
    by_user_items: Dict[str, List[Dict[str, Any]]] = {}
    for r in items:
        by_user_items.setdefault(r["user_id"], []).append(
            {
                "name": r["name"],
                "unit_price": r["unit_price"],
                "qty": r["qty"],
                "note": r["note"],
                "line_total": int(r["unit_price"]) * int(r["qty"]),
            }
        )

    # subtotal per user
    subtotals: Dict[str, int] = {
        uid: sum(x["line_total"] for x in its) for uid, its in by_user_items.items()
    }

    participants: List[Dict[str, Any]] = []
    for p in parts:
        uid = p["user_id"]
        participants.append(
            {
                "user_id": uid,
                "subtotal": subtotals.get(uid, 0),
                "total_due": int(p["total_due"]),
                "paid": bool(p["paid"]),
                "paid_at": p["paid_at"],
                "paid_to": p["paid_to"],
                "items": by_user_items.get(uid, []),
            }
        )

    return {
        "order": dict(order),
        "participants": participants,
    }


def get_user_debt(user_id: str) -> Dict[str, Any]:
    """
    查某人目前未付清總欠款與明細（忽略 cancelled 單）。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT o.order_id, o.vendor, o.created_at, o.payer_id,
                   p.total_due, p.paid
            FROM participants p
            JOIN orders o ON o.order_id = p.order_id
            WHERE p.user_id=?
              AND o.status != 'cancelled'
              AND p.paid = 0
            ORDER BY o.created_at DESC
            """,
            (user_id,),
        ).fetchall()

    details = []
    total = 0
    for r in rows:
        due = int(r["total_due"])
        total += due
        details.append(
            {
                "order_id": r["order_id"],
                "vendor": r["vendor"],
                "created_at": r["created_at"],
                "payer_id": r["payer_id"],
                "amount": due,
            }
        )

    return {"user_id": user_id, "total_debt": total, "details": details}


def get_user_overview(user_id: str, limit: int = 10) -> Dict[str, Any]:
    """
    個人總覽：
    - unpaid：未付清的訂單（忽略 cancelled）
    - paid_recent：最近已付清的訂單（忽略 cancelled）
    - my_orders：我開的訂單（忽略 cancelled）
    """
    with connect() as conn:
        unpaid_rows = conn.execute(
            """
            SELECT o.order_id, o.vendor, o.created_at, o.status, o.payer_id,
                   p.total_due, p.paid
            FROM participants p
            JOIN orders o ON o.order_id = p.order_id
            WHERE p.user_id=?
              AND o.status != 'cancelled'
              AND p.paid = 0
            ORDER BY o.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

        paid_rows = conn.execute(
            """
            SELECT o.order_id, o.vendor, o.created_at, o.status, o.payer_id,
                   p.total_due, p.paid_at
            FROM participants p
            JOIN orders o ON o.order_id = p.order_id
            WHERE p.user_id=?
              AND o.status != 'cancelled'
              AND p.paid = 1
            ORDER BY p.paid_at DESC, o.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

        my_orders = conn.execute(
            """
            SELECT o.order_id, o.vendor, o.created_at, o.status, o.payer_id,
                   o.discount_type, o.discount_value,
                   (SELECT COUNT(DISTINCT li.user_id)
                      FROM line_items li
                     WHERE li.order_id = o.order_id) AS people_count,
                   (SELECT COALESCE(SUM(p.total_due), 0)
                      FROM participants p
                     WHERE p.order_id = o.order_id) AS total_after_discount
            FROM orders o
            WHERE o.creator_id=?
              AND o.status != 'cancelled'
            ORDER BY o.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    def row_to_dict(r):
        return {k: r[k] for k in r.keys()}

    return {
        "user_id": user_id,
        "unpaid": [row_to_dict(r) for r in unpaid_rows],
        "paid_recent": [row_to_dict(r) for r in paid_rows],
        "my_orders": [row_to_dict(r) for r in my_orders],
    }


# ---------- Recalculation ----------
def recalc_order(order_id: int) -> None:
    with connect() as conn:
        recalc_order_conn(conn, order_id)
        conn.commit()


def recalc_order_conn(conn: sqlite3.Connection, order_id: int) -> None:
    """
    重新計算 participants.total_due（在同一個 conn/transaction 裡）。
    MVP 規則：
      - subtotal = Σ(unit_price*qty) per user
      - discount none: total_due=subtotal
      - discount percent: total_due=round(subtotal*percent)
      - adjustment: total_due=total_due+adjustment (每人固定加減)
      - cancelled: 不處理（但保留資料）
    """
    order = conn.execute(
        "SELECT status, discount_type, discount_value, adjustment FROM orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise ValueError(f"找不到 order_id={order_id}")

    if order["status"] == "cancelled":
        return  # 作廢單不再更新（也不計入欠款）

    # 計算每人 subtotal
    rows = conn.execute(
        """
        SELECT user_id, SUM(unit_price * qty) AS subtotal
        FROM line_items
        WHERE order_id=?
        GROUP BY user_id
        """,
        (order_id,),
    ).fetchall()

    subtotals = {r["user_id"]: int(r["subtotal"] or 0) for r in rows}

    discount_type = order["discount_type"]
    discount_value = float(order["discount_value"])
    adjustment = int(order["adjustment"] or 0)

    def calc_total(subtotal: int) -> int:
        if discount_type == "none":
            return subtotal
        if discount_type == "percent":
            # round() 在 .5 會走 bankers rounding；MVP 可接受
            return int(round(subtotal * discount_value))
        if discount_type == "amount":
            # 先不做：避免規則不清晰造成爭議
            # 你要做時我可以幫你加「按比例分攤」版本
            return subtotal
        return subtotal

    # upsert participants
    for uid, subtotal in subtotals.items():
        total_due = max(0, calc_total(subtotal) + adjustment)

        # 若已付，不動 total_due 也可以，但通常改折扣後已付者也應該一致更新
        # 這裡我們照「更新 total_due，但保留 paid 狀態」
        conn.execute(
            """
            INSERT INTO participants (order_id, user_id, total_due, paid, paid_at, paid_to)
            VALUES (?, ?, ?, 0, NULL, NULL)
            ON CONFLICT(order_id, user_id)
            DO UPDATE SET total_due=excluded.total_due
            """,
            (order_id, uid, total_due),
        )

    # 清掉「已經沒有品項的人」的 participants（避免殘留）
    conn.execute(
        """
        DELETE FROM participants
        WHERE order_id=?
          AND user_id NOT IN (
            SELECT DISTINCT user_id FROM line_items WHERE order_id=?
          )
        """,
        (order_id, order_id),
    )


# ---------- Helpers ----------
def _ensure_order_editable(conn: sqlite3.Connection, order_id: int) -> None:
    row = conn.execute(
        "SELECT status FROM orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"找不到 order_id={order_id}")
    if row["status"] in ("locked", "cancelled"):
        raise ValueError("此訂單已鎖定或作廢，不能修改。")


def _get_order_row(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        raise ValueError(f"找不到 order_id={order_id}")
    return row


def lock_order(order_id: int, actor_id: str) -> None:
    """收單：將訂單狀態設為 locked（僅開單者可用）。"""
    with connect() as conn:
        order = _get_order_row(conn, order_id)

        if order["status"] == "cancelled":
            raise ValueError("此訂單已作廢，不能收單。")
        if order["creator_id"] != actor_id:
            raise ValueError("只有開單的人可以收單。")

        conn.execute("UPDATE orders SET status='locked' WHERE order_id=?", (order_id,))
        conn.commit()


def unlock_order(order_id: int, actor_id: str) -> None:
    """解鎖：將 locked 改回 open（僅開單者可用）。"""
    with connect() as conn:
        order = _get_order_row(conn, order_id)

        if order["status"] == "cancelled":
            raise ValueError("此訂單已作廢，不能解鎖。")
        if order["creator_id"] != actor_id:
            raise ValueError("只有開單的人可以解鎖此訂單。")

        conn.execute("UPDATE orders SET status='open' WHERE order_id=?", (order_id,))
        conn.commit()

def list_orders_for_picker(limit: int = 25) -> list[dict]:
    """
    給下拉選單用：列出最近的未作廢訂單
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, vendor, created_at, status, creator_id, payer_id, discount_type, discount_value
            FROM orders
            WHERE status != 'cancelled'
            ORDER BY order_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def search_orders_for_picker(keyword: str, limit: int = 25) -> list[dict]:
    """
    給 autocomplete 用：依 keyword 過濾（可搜 order_id / vendor）
    """
    kw = f"%{keyword.strip()}%"
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT order_id, vendor, created_at, status, creator_id, payer_id, discount_type, discount_value
            FROM orders
            WHERE status != 'cancelled'
              AND (CAST(order_id AS TEXT) LIKE ? OR vendor LIKE ?)
            ORDER BY order_id DESC
            LIMIT ?
            """,
            (kw, kw, limit),
        ).fetchall()
    return [dict(r) for r in rows]
