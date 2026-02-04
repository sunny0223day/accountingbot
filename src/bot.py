from __future__ import annotations

import os
from typing import Optional

import discord
from discord import app_commands

# 數學運算用
import re
import ast
import operator

# 你的核心邏輯
from src.db import (
    create_order,
    add_item,
    get_bill,
    get_user_debt,
    get_user_overview,
    mark_paid,
    set_discount_percent,
    search_orders_for_picker,
    lock_order,
    unlock_order,
    cancel_order,
)

# UI 顯示用（DB 仍用 open/locked/cancelled）
STATUS_LABEL = {
    "open": "開放中",
    "locked": "收單",
    "cancelled": "作廢",
}


def status_text(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def uid(user: discord.abc.User) -> str:
    # DB 用字串存 Discord user id
    return str(user.id)


def money(n: int) -> str:
    return f"{n}"


class AccountingBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # ✅ 建議先用「Guild sync」：指令幾乎立刻生效（測試期超重要）
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Slash commands synced to guild {guild_id}")
        else:
            # 沒設 guild 的話就全域 sync（可能需要幾分鐘～幾小時才出現）
            await self.tree.sync()
            print("✅ Slash commands synced globally (may take time)")


bot = AccountingBot()


async def display_name_for(interaction: discord.Interaction, user_id: str) -> str:
    """用 user_id 取得在該 guild 的顯示名稱（優先暱稱）。抓不到就退回 username / user_id。"""
    uid_int = int(user_id)

    # 優先：快取（最省）
    if interaction.guild:
        m = interaction.guild.get_member(uid_int)
        if m:
            return m.display_name

        # 次選：REST 抓 guild member（不依賴 members intent）
        try:
            m = await interaction.guild.fetch_member(uid_int)
            return m.display_name
        except Exception:
            pass

    # 再退：抓 user（全域 username）
    try:
        u = await bot.fetch_user(uid_int)
        return u.name
    except Exception:
        return user_id


async def order_id_autocomplete(interaction: discord.Interaction, current: str):
    rows = search_orders_for_picker(current or "", limit=25)

    choices = []
    for o in rows:
        # Discord autocomplete 每個 label 最長 100 字
        label = f"#{o['order_id']} | {o['vendor']} | {o['created_at'][:16]} | {status_text(o['status'])}"
        choices.append(app_commands.Choice(name=label[:100], value=int(o["order_id"])))

    return choices


# -----------------------
# /open
# -----------------------
@bot.tree.command(name="open", description="開一張新單（店家/團名）")
@app_commands.describe(vendor="店家或團名，例如 50嵐、麥當勞", note="備註（可空）", payer="付款人（可空，預設你自己）")
async def open_cmd(
    interaction: discord.Interaction,
    vendor: str,
    note: Optional[str] = "",
    payer: Optional[discord.Member] = None,
):
    creator_id = uid(interaction.user)
    payer_id = uid(payer) if payer else creator_id

    order_id = create_order(vendor=vendor, creator_id=creator_id, payer_id=payer_id, note=note or "")

    await interaction.response.send_message(
        f"✅ 已開單：`#{order_id}`\n店家：**{vendor}**\n付款人：<@{payer_id}>",
        ephemeral=False,
    )


# -----------------------
# /add
# -----------------------
@bot.tree.command(name="add", description="在指定訂單新增品項")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(
    order_id="訂單編號（例如 12）",
    item="品名",
    price="單價（整數）",
    qty="數量（預設 1）",
    user="點餐的人（可空，預設你自己）",
    note="備註（可空）",
)
async def add_cmd(
    interaction: discord.Interaction,
    order_id: int,
    item: str,
    price: int,
    qty: Optional[int] = 1,
    user: Optional[discord.Member] = None,
    note: Optional[str] = "",
):
    try:
        target = user or interaction.user
        item_id = add_item(
            order_id=order_id,
            user_id=uid(target),
            name=item,
            unit_price=int(price),
            qty=int(qty or 1),
            note=note or "",
            created_by=uid(interaction.user),
        )
        await interaction.response.send_message(
            f"✅ 已加入 `#{order_id}`：<@{uid(target)}> - **{item}** x{qty or 1} @ {price}（item_id={item_id}）",
            ephemeral=False,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 加入失敗：{e}", ephemeral=True)


# -----------------------
# /bill
# -----------------------
@bot.tree.command(name="bill", description="查看整張單（含每人明細與付款狀態）")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號（例如 12）")
async def bill_cmd(interaction: discord.Interaction, order_id: int):
    try:
        data = get_bill(order_id)
        order = data["order"]
        parts = data["participants"]

        created_at = order["created_at"].replace("T", " ")[:16]

        embed = discord.Embed(
            title=f"訂單 #{order['order_id']}｜{order['vendor']}",
            description=(
                f"📅 建立時間：**{created_at}**\n"
                f"狀態：**{status_text(order['status'])}**｜折扣：`{order['discount_type']} {order['discount_value']}`"
            ),
        )

        for p in parts:
            u = p["user_id"]
            paid = "✅已付" if p["paid"] else "❌未付"

            lines = []
            for it in p["items"]:
                note = f"（{it['note']}）" if it["note"] else ""
                lines.append(f"- {it['name']} x{it['qty']} @ {it['unit_price']} = {it['line_total']} {note}")
            if not lines:
                lines.append("- （無品項）")

            display_name = await display_name_for(interaction, u)
            value_lines = [f"👤 <@{u}>"] + lines

            embed.add_field(
                name=f"{display_name}｜應付 {money(p['total_due'])}｜{paid}",
                value="\n".join(value_lines),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=False)
    except Exception as e:
        await interaction.response.send_message(f"❌ 查詢失敗：{e}", ephemeral=True)


# -----------------------
# /debt
# -----------------------
@bot.tree.command(name="debt", description="查某人目前未付清欠款")
@app_commands.describe(user="要查的人（可空，預設你自己）", public="是否公開顯示（預設：公開）")
async def debt_cmd(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    public: Optional[bool] = True,
):
    target = user or interaction.user
    try:
        debt = get_user_debt(uid(target))
        total = debt["total_debt"]
        details = debt["details"]

        ephemeral = not bool(public)

        if not details:
            await interaction.response.send_message(
                f"✅ <@{uid(target)}> 目前沒有未付清欠款。",
                ephemeral=ephemeral,
            )
            return

        lines = [f"**總欠款：{money(total)}**"]
        for d in details[:20]:
            lines.append(f"- `#{d['order_id']}` {d['vendor']}（欠 <@{d['payer_id']}>）：{money(d['amount'])}")

        await interaction.response.send_message(
            f"📌 <@{uid(target)}> 的欠款\n" + "\n".join(lines),
            ephemeral=ephemeral,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 查詢失敗：{e}", ephemeral=True)




# -----------------------
# /my
# -----------------------
@bot.tree.command(name="my", description="個人總覽：我欠多少、最近已付、我開的團")
async def my_cmd(interaction: discord.Interaction):
    me_id = uid(interaction.user)
    try:
        data = get_user_overview(me_id, limit=10)

        unpaid = data["unpaid"]
        paid_recent = data["paid_recent"]
        my_orders = data["my_orders"]

        embed = discord.Embed(
            title=f"👤 {interaction.user.display_name} 的總覽",
            description="（顯示最近 10 筆）",
        )

        # 未付清
        if unpaid:
            lines = []
            total_unpaid = 0
            for r in unpaid:
                amt = int(r["total_due"] or 0)
                total_unpaid += amt
                lines.append(
                    f"- `#{r['order_id']}` {r['vendor']}｜{status_text(r['status'])}｜欠 {money(amt)}（付給 <@{r['payer_id']}>）"
                )
            embed.add_field(
                name=f"📌 尚未付清（{len(unpaid)}）｜合計 {money(total_unpaid)}",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="📌 尚未付清",
                value="✅ 目前沒有未付清欠款。",
                inline=False,
            )

        # 最近已付
        if paid_recent:
            lines = []
            for r in paid_recent:
                amt = int(r["total_due"] or 0)
                lines.append(
                    f"- `#{r['order_id']}` {r['vendor']}｜{status_text(r['status'])}｜{money(amt)}（付給 <@{r['payer_id']}>）"
                )
            embed.add_field(
                name=f"✅ 最近已付（{len(paid_recent)}）",
                value="\n".join(lines),
                inline=False,
            )

        # 我開的團
        if my_orders:
            lines = []
            for r in my_orders:
                people = int(r.get("people_count") or 0)
                total = int(r.get("total_after_discount") or 0)
                discount = f"{r.get('discount_type')} {r.get('discount_value')}"
                lines.append(
                    f"- `#{r['order_id']}` {r['vendor']}｜{status_text(r['status'])}｜{people} 人｜"
                    f"折後總計 {money(total)}｜折扣 `{discount}`｜付款人 <@{r['payer_id']}>"
)

            embed.add_field(
                name=f"🧾 我開的團（{len(my_orders)}）",
                value="\n".join(lines),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.response.send_message(f"❌ 查詢失敗：{e}", ephemeral=True)


# -----------------------
# /help
# -----------------------
@bot.tree.command(name="help", description="顯示記帳機器人使用說明")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📒 記帳機器人使用說明",
        description=(
            "這是一個用來「訂餐分帳」的小工具\n"
            "可以記錄每個人點了什麼、誰付了錢、還有誰沒付。"
        ),
    )

    embed.add_field(
        name="🍱 參與訂單（一般使用者）",
        value=(
            "`/add` 加入你點的品項\n"
            "`/bill` 查看目前訂單與每個人應付金額\n"
            "`/debt` 查看欠款（預設看自己，會顯示在頻道）\n"
            "`/my` 個人總覽（只自己看）"
        ),
        inline=False,
    )

    embed.add_field(
        name="💰 付款",
        value="`/pay` 標記你已經付款（由收錢的人確認）",
        inline=False,
    )

    embed.add_field(
        name="👑 開團者專用",
        value=(
            "`/open` 開新訂單\n"
            "`/discount` 設定整單折扣（例如 9 折）\n"
            "`/adjust` 設定每人矯正金額（例如 每人 +1）\n"
            "`/lock` 收單（不能再加品項）\n"
            "`/unlock` 重新開放訂單\n"
            "`/cancel` 作廢訂單"
        ),
        inline=False,
    )

    embed.add_field(
        name="🧮 金額怎麼算",
        value="折扣 → 每人矯正金額 → 最終應付金額",
        inline=False,
    )

    embed.add_field(
        name="💡 快速計算",
        value=(
            "在聊天中輸入四則運算並以「=」結尾，機器人會自動回覆結果。\n"
            "範例：\n"
            "100+200=\n"
            "→300\n"
            "(100+200)*3=\n"
            "→900\n"
            ),
        inline=False,
    )

    embed.set_footer(text="這是內部記帳工具，允許人工調整。如有疑問請詢問開團者。")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# -----------------------
# /pay
# -----------------------
@bot.tree.command(name="pay", description="把某人在某張單標記為已付")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號", user="付款的人（可空，預設你自己）", paid_to="付給誰（可空，預設訂單 payer）")
async def pay_cmd(
    interaction: discord.Interaction,
    order_id: int,
    user: Optional[discord.Member] = None,
    paid_to: Optional[discord.Member] = None,
):
    target = user or interaction.user
    try:
        mark_paid(order_id=order_id, user_id=uid(target), paid_to=uid(paid_to) if paid_to else None)
        await interaction.response.send_message(
            f"✅ 已標記付款：`#{order_id}` <@{uid(target)}>",
            ephemeral=False,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 付款失敗：{e}", ephemeral=True)


# -----------------------
# /discount
# -----------------------
@bot.tree.command(name="discount", description="設定整張單折扣（percent，例如 0.9）")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號", percent="折扣比例：0~1，例如 0.9 代表打九折")
async def discount_cmd(interaction: discord.Interaction, order_id: int, percent: float):
    try:
        set_discount_percent(order_id, percent)
        await interaction.response.send_message(f"✅ 已設定訂單 `#{order_id}` 折扣為 {percent}", ephemeral=False)
    except Exception as e:
        await interaction.response.send_message(f"❌ 設定失敗：{e}", ephemeral=True)


# -----------------------
# /lock
# -----------------------
@bot.tree.command(name="lock", description="收單（停止加品項，僅開單者可用）")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號")
async def lock_cmd(interaction: discord.Interaction, order_id: int):
    try:
        lock_order(order_id=order_id, actor_id=uid(interaction.user))

        data = get_bill(order_id)
        order = data["order"]
        parts = data["participants"]

        created_at = order["created_at"].replace("T", " ")[:16]
        payer_id = order.get("payer_id", "")

        embed = discord.Embed(
            title=f"🧾 已收單 #{order['order_id']}｜{order['vendor']}",
            description=(
                f"📅 建立時間：**{created_at}**\n"
                f"付款人：<@{payer_id}>\n"
                f"狀態：**{status_text(order['status'])}**｜折扣：`{order['discount_type']} {order['discount_value']}`"
            ),
        )

        for p in parts:
            u = p["user_id"]
            paid = "✅已付" if p["paid"] else "❌未付"

            lines = []
            for it in p["items"]:
                note = f"（{it['note']}）" if it["note"] else ""
                lines.append(f"- {it['name']} x{it['qty']} @ {it['unit_price']} = {it['line_total']} {note}")
            if not lines:
                lines.append("- （無品項）")

            display_name = await display_name_for(interaction, u)
            value_lines = [f"👤 <@{u}>"] + lines

            embed.add_field(
                name=f"{display_name}｜應付 {money(p['total_due'])}｜{paid}",
                value="\n".join(value_lines),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=False)
    except Exception as e:
        await interaction.response.send_message(f"❌ 收單失敗：{e}", ephemeral=True)


# -----------------------
# /unlock
# -----------------------
@bot.tree.command(name="unlock", description="解鎖訂單（重新開放加品項，僅開單者可用）")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號")
async def unlock_cmd(interaction: discord.Interaction, order_id: int):
    try:
        unlock_order(order_id=order_id, actor_id=uid(interaction.user))
        await interaction.response.send_message(
            f"🔓 已解鎖訂單：`#{order_id}`（此單重新開放加品項）",
            ephemeral=False,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 解鎖失敗：{e}", ephemeral=True)


# -----------------------
# /cancel
# -----------------------
@bot.tree.command(name="cancel", description="作廢訂單（僅開單者可用）")
@app_commands.autocomplete(order_id=order_id_autocomplete)
@app_commands.describe(order_id="訂單編號")
async def cancel_cmd(interaction: discord.Interaction, order_id: int):
    try:
        cancel_order(order_id=order_id, actor_id=uid(interaction.user))
        await interaction.response.send_message(
            f"🗑️ 已作廢訂單：`#{order_id}`（此單不再計入欠款與結算）",
            ephemeral=False,
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ 作廢失敗：{e}", ephemeral=True)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

# -----------------------
# Safe math evaluator
# -----------------------

_ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}

def safe_eval(expr: str) -> float:
    """
    安全的四則運算 evaluator
    只允許 + - * / () 小數
    """
    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.BinOp):
            op = _ALLOWED_OPERATORS.get(type(node.op))
            if not op:
                raise ValueError("Unsupported operator")
            return op(_eval(node.left), _eval(node.right))
        elif isinstance(node, ast.UnaryOp):
            op = _ALLOWED_OPERATORS.get(type(node.op))
            if not op:
                raise ValueError("Unsupported unary operator")
            return op(_eval(node.operand))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Invalid constant")
        else:
            raise ValueError("Invalid expression")

    tree = ast.parse(expr, mode="eval")
    return _eval(tree)

# -----------------------
# Chat math handler
# -----------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    text = message.content.strip()

    # 必須以 "=" 結尾
    if text.endswith("="):
        expr = text[:-1].strip()

        # 只允許四則運算字元
        if re.fullmatch(r"[0-9+\-*/(). ]+", expr):
            try:
                result = safe_eval(expr)
                if isinstance(result, float) and result.is_integer():
                    result = int(result)
                await message.channel.send(f"= {result}")
            except Exception:
                pass


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("請先設定環境變數 DISCORD_BOT_TOKEN")
    bot.run(token)
