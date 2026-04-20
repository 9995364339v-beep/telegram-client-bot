import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН")
DB_PATH = os.path.join(os.path.dirname(__file__), "clients.db")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '📌',
                keywords TEXT NOT NULL DEFAULT '',
                UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER,
                text TEXT NOT NULL,
                amount REAL,
                created_at TEXT NOT NULL
            );
            """
        )


DEFAULT_CATEGORIES = [
    ("Яндекс Директ", "🟡", "яндекс,директ,директе,yandex"),
    ("Сайты", "🌐", "сайт,сайты,лендинг,landing"),
    ("Google Ads", "🔵", "google,гугл,адс,ads"),
    ("Таргет", "🎯", "таргет,таргетинг,инстаграм,instagram,facebook,фб"),
    ("SEO", "🔍", "seo,сео,продвижение"),
    ("Прочее", "📌", ""),
]


def ensure_default_categories(user_id: int):
    with db() as con:
        cur = con.execute("SELECT COUNT(*) c FROM categories WHERE user_id=?", (user_id,))
        if cur.fetchone()["c"] == 0:
            con.executemany(
                "INSERT INTO categories (user_id,name,emoji,keywords) VALUES (?,?,?,?)",
                [(user_id, n, e, k) for n, e, k in DEFAULT_CATEGORIES],
            )


def get_categories(user_id: int):
    with db() as con:
        return con.execute(
            "SELECT * FROM categories WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()


def parse_amount(text: str):
    """Берём последнее число из текста. Поддержка 10к/10тыс/10 000."""
    t = text.lower().replace("\xa0", " ")
    m = re.findall(r"(\d[\d\s]*[.,]?\d*)\s*(к|k|тыс[а-я]*|млн|m)?", t)
    if not m:
        return None
    num_str, suf = m[-1]
    num_str = num_str.replace(" ", "").replace(",", ".")
    try:
        val = float(num_str)
    except ValueError:
        return None
    if suf in ("к", "k") or suf.startswith("тыс"):
        val *= 1000
    elif suf in ("млн", "m"):
        val *= 1_000_000
    return val


def guess_category(user_id: int, text: str):
    t = text.lower()
    cats = get_categories(user_id)
    best = None
    best_score = 0
    for c in cats:
        score = 0
        for kw in [k.strip() for k in (c["keywords"] or "").split(",") if k.strip()]:
            if kw in t:
                score += len(kw)
        if c["name"].lower() in t:
            score += len(c["name"])
        if score > best_score:
            best_score = score
            best = c
    return best


def main_menu():
    kb = [
        [InlineKeyboardButton("➕ Новая запись", callback_data="new")],
        [InlineKeyboardButton("📋 Записи", callback_data="list")],
        [InlineKeyboardButton("🗂 Категории", callback_data="cats")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(kb)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_default_categories(user_id)
    await update.message.reply_text(
        "Привет! Я бот для учёта клиентов.\n\n"
        "Просто напиши сообщение вида:\n"
        "<code>реклама в яндекс директ 10 тыс</code>\n\n"
        "Я сам определю категорию (Яндекс) и сумму (10000).",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    ensure_default_categories(user_id)

    state = ctx.user_data.get("state")
    if state == "add_cat_name":
        ctx.user_data["new_cat_name"] = text
        ctx.user_data["state"] = "add_cat_emoji"
        await update.message.reply_text("Отправь эмодзи для категории (или - чтобы пропустить):")
        return
    if state == "add_cat_emoji":
        ctx.user_data["new_cat_emoji"] = text if text != "-" else "📌"
        ctx.user_data["state"] = "add_cat_keywords"
        await update.message.reply_text(
            "Ключевые слова через запятую (по ним я буду угадывать категорию). Или - чтобы пропустить:"
        )
        return
    if state == "add_cat_keywords":
        kws = "" if text == "-" else text.lower()
        try:
            with db() as con:
                con.execute(
                    "INSERT INTO categories (user_id,name,emoji,keywords) VALUES (?,?,?,?)",
                    (user_id, ctx.user_data["new_cat_name"], ctx.user_data["new_cat_emoji"], kws),
                )
            await update.message.reply_text("✅ Категория добавлена.", reply_markup=main_menu())
        except sqlite3.IntegrityError:
            await update.message.reply_text("⚠️ Такая категория уже есть.", reply_markup=main_menu())
        ctx.user_data.clear()
        return

    amount = parse_amount(text)
    guessed = guess_category(user_id, text)
    ctx.user_data["pending_text"] = text
    ctx.user_data["pending_amount"] = amount

    cats = get_categories(user_id)
    rows = []
    row = []
    for c in cats:
        mark = "✅ " if guessed and c["id"] == guessed["id"] else ""
        row.append(InlineKeyboardButton(f"{mark}{c['emoji']} {c['name']}", callback_data=f"pick:{c['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ Новая категория", callback_data="cat_add")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])

    sum_str = f"{amount:g} ₽" if amount else "не распознана"
    suggest = f"\n\nПредлагаю: {guessed['emoji']} <b>{guessed['name']}</b>" if guessed else ""
    await update.message.reply_text(
        f"📝 <b>{text}</b>\n💰 Сумма: {sum_str}{suggest}\n\nВыбери категорию:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    data = q.data

    if data == "new":
        await q.edit_message_text("Просто напиши, например:\n<code>реклама в яндекс директ 10 тыс</code>", parse_mode="HTML")
        return

    if data == "cancel":
        ctx.user_data.pop("pending_text", None)
        ctx.user_data.pop("pending_amount", None)
        await q.edit_message_text("Отменено.", reply_markup=main_menu())
        return

    if data.startswith("pick:"):
        cat_id = int(data.split(":")[1])
        text = ctx.user_data.get("pending_text")
        amount = ctx.user_data.get("pending_amount")
        if not text:
            await q.edit_message_text("Нет данных. Напиши заново.", reply_markup=main_menu())
            return
        with db() as con:
            con.execute(
                "INSERT INTO records (user_id,category_id,text,amount,created_at) VALUES (?,?,?,?,?)",
                (user_id, cat_id, text, amount, datetime.now().isoformat(timespec="seconds")),
            )
            cat = con.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
        ctx.user_data.clear()
        await q.edit_message_text(
            f"✅ Сохранено в {cat['emoji']} <b>{cat['name']}</b>\n💰 {amount:g} ₽" if amount
            else f"✅ Сохранено в {cat['emoji']} <b>{cat['name']}</b>",
            reply_markup=main_menu(),
            parse_mode="HTML",
        )
        return

    if data == "cats":
        cats = get_categories(user_id)
        rows = [[InlineKeyboardButton(f"{c['emoji']} {c['name']}", callback_data=f"cat_view:{c['id']}")] for c in cats]
        rows.append([InlineKeyboardButton("➕ Добавить", callback_data="cat_add")])
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
        await q.edit_message_text("🗂 Твои категории:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("cat_view:"):
        cid = int(data.split(":")[1])
        with db() as con:
            c = con.execute("SELECT * FROM categories WHERE id=? AND user_id=?", (cid, user_id)).fetchone()
        if not c:
            await q.edit_message_text("Категория не найдена.", reply_markup=main_menu())
            return
        kb = [
            [InlineKeyboardButton("🗑 Удалить", callback_data=f"cat_del:{cid}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="cats")],
        ]
        await q.edit_message_text(
            f"{c['emoji']} <b>{c['name']}</b>\nКлючевые слова: {c['keywords'] or '—'}",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
        return

    if data.startswith("cat_del:"):
        cid = int(data.split(":")[1])
        with db() as con:
            con.execute("DELETE FROM categories WHERE id=? AND user_id=?", (cid, user_id))
        await q.edit_message_text("🗑 Удалено.", reply_markup=main_menu())
        return

    if data == "cat_add":
        ctx.user_data["state"] = "add_cat_name"
        await q.edit_message_text("Название новой категории:")
        return

    if data == "list":
        with db() as con:
            rows = con.execute(
                """SELECT r.*, c.name cname, c.emoji cemoji FROM records r
                   LEFT JOIN categories c ON c.id=r.category_id
                   WHERE r.user_id=? ORDER BY r.id DESC LIMIT 15""",
                (user_id,),
            ).fetchall()
        if not rows:
            await q.edit_message_text("Записей пока нет.", reply_markup=main_menu())
            return
        lines = ["📋 <b>Последние записи:</b>\n"]
        for r in rows:
            cat = f"{r['cemoji']} {r['cname']}" if r["cname"] else "—"
            amt = f" — {r['amount']:g} ₽" if r["amount"] else ""
            d = r["created_at"][5:16].replace("T", " ")
            lines.append(f"{d} · {cat}{amt}\n<i>{r['text']}</i>")
        await q.edit_message_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]]),
            parse_mode="HTML",
        )
        return

    if data == "stats":
        with db() as con:
            rows = con.execute(
                """SELECT c.name, c.emoji, COUNT(r.id) cnt, COALESCE(SUM(r.amount),0) total
                   FROM categories c LEFT JOIN records r ON r.category_id=c.id AND r.user_id=c.user_id
                   WHERE c.user_id=? GROUP BY c.id ORDER BY total DESC""",
                (user_id,),
            ).fetchall()
        lines = ["📊 <b>Статистика по категориям:</b>\n"]
        total_all = 0
        for r in rows:
            total_all += r["total"] or 0
            lines.append(f"{r['emoji']} {r['name']}: {r['cnt']} зап. · {r['total']:g} ₽")
        lines.append(f"\n<b>Итого: {total_all:g} ₽</b>")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]]),
            parse_mode="HTML",
        )
        return

    if data == "back":
        await q.edit_message_text("Главное меню:", reply_markup=main_menu())
        return


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logging.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
