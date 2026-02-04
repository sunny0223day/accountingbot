from __future__ import annotations

from pprint import pprint

from db import (
    create_order,
    add_item,
    get_bill,
    mark_paid,
    get_user_debt,
    set_discount_percent,
)


def print_bill(order_id: int) -> None:
    data = get_bill(order_id)
    order = data["order"]
    print("\n" + "=" * 60)
    print(f"Order #{order['order_id']} | {order['vendor']} | status={order['status']}")
    print(f"discount={order['discount_type']} {order['discount_value']}")
    print("-" * 60)

    for p in data["participants"]:
        uid = p["user_id"]
        paid = "✅已付" if p["paid"] else "❌未付"
        print(f"[{uid}] subtotal={p['subtotal']} total_due={p['total_due']} {paid}")
        for it in p["items"]:
            note = f" ({it['note']})" if it["note"] else ""
            print(f"  - {it['name']} x{it['qty']} @ {it['unit_price']} = {it['line_total']}{note}")
    print("=" * 60)


def main():
    # 用簡單的字串當 user_id（之後接 Discord 會用真正的 discord user id）
    A = "user_A"
    B = "user_B"

    order_id = create_order(vendor="50嵐", creator_id=A, note="下午茶")
    print(f"✅ 已開單：#{order_id}")

    add_item(order_id, user_id=A, name="珍奶微糖", unit_price=60, qty=1)
    add_item(order_id, user_id=B, name="紅茶去冰", unit_price=40, qty=1)
    add_item(order_id, user_id=B, name="波霸", unit_price=10, qty=1, note="加料")

    print_bill(order_id)

    print("\n✅ 設定整張單打九折（0.9）")
    set_discount_percent(order_id, 0.9)
    print_bill(order_id)

    print("\n✅ B 付款")
    mark_paid(order_id, user_id=B)
    print_bill(order_id)

    print("\n✅ 查 A 欠款")
    debt = get_user_debt(A)
    pprint(debt)


if __name__ == "__main__":
    main()
