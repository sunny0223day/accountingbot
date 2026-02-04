from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "db" / "app.sqlite3"
SCHEMA_PATH = ROOT / "db" / "schema.sql"


def init_db() -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"找不到 schema 檔案：{SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # 建議開啟 WAL：多指令併發時比較穩（你量很小，但開了沒壞處）
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.executescript(schema_sql)
        conn.commit()

    print(f"✅ DB 建立完成：{DB_PATH}")


def show_tables() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ).fetchall()
    print("📋 Tables:", [r[0] for r in rows])


if __name__ == "__main__":
    init_db()
    show_tables()
