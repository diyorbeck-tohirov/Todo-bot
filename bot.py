import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv

# --- SOZLAMALAR ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("BOT_TOKEN", "").strip()

DB_PATH = BASE_DIR / "todo.db"
TZ = timezone(timedelta(hours=5))  # Toshkent (UTC+5)
RETRY_DELAY = 5
MAX_TEXT_LEN = 200       # bitta vazifa matnining uzunligi
MAX_PENDING = 50         # bitta foydalanuvchining bajarilmagan vazifalari soni
SHOW_LIMIT = 20          # bir xabarda ko'rsatiladigan vazifalar soni

BTN_ADD = "➕ Vazifa qo'shish"
BTN_LIST = "📋 Vazifalarim"
BTN_DONE = "✅ Bajarilganlar"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8")],
)

dp = Dispatcher(storage=MemoryStorage())


# --- MA'LUMOTLAR BAZASI (sinxron, asyncio.to_thread orqali chaqiriladi) ---
def init_db() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks (user_id, done)")


def _add_task(user_id: int, text: str) -> bool:
    """Vazifa qo'shadi. Limit to'lgan bo'lsa False qaytaradi."""
    created = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND done = 0", (user_id,)
        ).fetchone()[0]
        if count >= MAX_PENDING:
            return False
        conn.execute(
            "INSERT INTO tasks (user_id, text, done, created) VALUES (?, ?, 0, ?)",
            (user_id, text, created),
        )
    return True


def _get_tasks(user_id: int, done: int) -> list[tuple[int, str]]:
    order = "id DESC" if done else "id ASC"
    with closing(sqlite3.connect(DB_PATH)) as conn:
        return conn.execute(
            f"SELECT id, text FROM tasks WHERE user_id = ? AND done = ? ORDER BY {order} LIMIT ?",
            (user_id, done, SHOW_LIMIT),
        ).fetchall()


def _mark_done(user_id: int, task_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        cur = conn.execute(
            "UPDATE tasks SET done = 1 WHERE id = ? AND user_id = ? AND done = 0",
            (task_id, user_id),
        )
        return cur.rowcount > 0


def _delete_task(user_id: int, task_id: int) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
        return cur.rowcount > 0


def _clear_done(user_id: int) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        cur = conn.execute("DELETE FROM tasks WHERE user_id = ? AND done = 1", (user_id,))
        return cur.rowcount


# --- FSM ---
class AddTask(StatesGroup):
    waiting_text = State()


# --- KLAVIATURA VA MATNLAR ---
def main_keyboard() -> types.ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=BTN_ADD)
    builder.button(text=BTN_LIST)
    builder.button(text=BTN_DONE)
    builder.adjust(1, 2)
    return builder.as_markup(resize_keyboard=True)


async def render_pending(user_id: int):
    """Bajarilmagan vazifalar matni va tugmalarini qaytaradi."""
    tasks = await asyncio.to_thread(_get_tasks, user_id, 0)
    if not tasks:
        return "Hozircha bajarilmagan vazifa yo'q 🎉", None

    lines = ["📋 <b>Vazifalaringiz:</b>\n"]
    builder = InlineKeyboardBuilder()
    for n, (task_id, text) in enumerate(tasks, 1):
        lines.append(f"{n}. {html.escape(text)}")
        builder.button(text=f"✅ {n}", callback_data=f"done:{task_id}")
        builder.button(text=f"🗑 {n}", callback_data=f"del:{task_id}")
    builder.adjust(2)
    lines.append("\n✅ — bajarildi, 🗑 — o'chirish")
    return "\n".join(lines), builder.as_markup()


async def render_done(user_id: int):
    """Bajarilgan vazifalar matni va tozalash tugmasini qaytaradi."""
    tasks = await asyncio.to_thread(_get_tasks, user_id, 1)
    if not tasks:
        return "Bajarilgan vazifalar yo'q.", None

    lines = ["✅ <b>Bajarilgan vazifalar:</b>\n"]
    for n, (_, text) in enumerate(tasks, 1):
        lines.append(f"{n}. {html.escape(text)}")
    builder = InlineKeyboardBuilder()
    builder.button(text="🧹 Hammasini tozalash", callback_data="clear")
    return "\n".join(lines), builder.as_markup()


async def refresh_pending(callback: types.CallbackQuery) -> None:
    text, markup = await render_pending(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        logging.debug("Xabarni yangilab bo'lmadi", exc_info=True)


# --- BUYRUQLAR ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    name = html.escape(message.from_user.full_name)
    await message.answer(
        f"Salom, {name}! 👋\n"
        "Men vazifalaringizni yozib boruvchi botman.\n\n"
        "➕ Vazifa qo'shish — yangi vazifa yozing\n"
        "📋 Vazifalarim — ro'yxat, bajarish va o'chirish\n"
        "✅ Bajarilganlar — tugatgan ishlaringiz\n\n"
        "Bekor qilish uchun /cancel.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Bekor qilinadigan narsa yo'q.", reply_markup=main_keyboard())
        return
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_keyboard())


# --- VAZIFA QO'SHISH ---
@dp.message(F.text == BTN_ADD)
@dp.message(Command("add"))
async def ask_task(message: types.Message, state: FSMContext):
    await state.set_state(AddTask.waiting_text)
    await message.answer(
        f"Vazifa matnini yozing (eng ko'pi bilan {MAX_TEXT_LEN} belgi).\nBekor qilish: /cancel"
    )


@dp.message(AddTask.waiting_text, F.text, ~F.text.startswith("/"), ~F.text.in_({BTN_ADD, BTN_LIST, BTN_DONE}))
async def save_task(message: types.Message, state: FSMContext):
    text = " ".join(message.text.split())  # ortiqcha bo'shliq va qator uzilishlarini tozalaydi
    if not text:
        await message.answer("Matn bo'sh bo'lmasligi kerak. Qayta yozing:")
        return
    if len(text) > MAX_TEXT_LEN:
        await message.answer(f"Matn juda uzun ({len(text)} belgi). {MAX_TEXT_LEN} tagacha qisqartiring:")
        return

    added = await asyncio.to_thread(_add_task, message.from_user.id, text)
    await state.clear()
    if added:
        await message.answer("✅ Vazifa qo'shildi!", reply_markup=main_keyboard())
    else:
        await message.answer(
            f"Bajarilmagan vazifalar soni {MAX_PENDING} taga yetdi. Avval bir nechtasini bajaring yoki o'chiring.",
            reply_markup=main_keyboard(),
        )


# --- RO'YXATLAR ---
@dp.message(F.text == BTN_LIST)
@dp.message(Command("list"))
async def show_pending(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = await render_pending(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(F.text == BTN_DONE)
@dp.message(Command("done"))
async def show_done(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = await render_done(message.from_user.id)
    await message.answer(text, reply_markup=markup)


# Eng oxirida: faqat matn bo'lmagan xabarlar (rasm, stiker...) uchun
@dp.message(AddTask.waiting_text)
async def waiting_text_invalid(message: types.Message):
    await message.answer("Iltimos, vazifani oddiy matn ko'rinishida yozing yoki /cancel bosing.")


# --- TUGMALAR (callback) ---
def _parse_id(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: types.CallbackQuery):
    task_id = _parse_id(callback.data)
    if task_id is None or not isinstance(callback.message, types.Message):
        await callback.answer("Noto'g'ri so'rov.", show_alert=True)
        return
    ok = await asyncio.to_thread(_mark_done, callback.from_user.id, task_id)
    await callback.answer("Bajarildi! 🎉" if ok else "Bu vazifa topilmadi.")
    await refresh_pending(callback)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: types.CallbackQuery):
    task_id = _parse_id(callback.data)
    if task_id is None or not isinstance(callback.message, types.Message):
        await callback.answer("Noto'g'ri so'rov.", show_alert=True)
        return
    ok = await asyncio.to_thread(_delete_task, callback.from_user.id, task_id)
    await callback.answer("O'chirildi 🗑" if ok else "Bu vazifa topilmadi.")
    await refresh_pending(callback)


@dp.callback_query(F.data == "clear")
async def cb_clear(callback: types.CallbackQuery):
    if not isinstance(callback.message, types.Message):
        await callback.answer("Xabar endi mavjud emas.", show_alert=True)
        return
    count = await asyncio.to_thread(_clear_done, callback.from_user.id)
    await callback.answer(f"{count} ta vazifa tozalandi.")
    try:
        await callback.message.edit_text("Bajarilgan vazifalar yo'q.")
    except TelegramBadRequest:
        logging.debug("Xabarni yangilab bo'lmadi", exc_info=True)


# --- UMUMIY XATO USHLOVCHI ---
@dp.error()
async def on_error(event: ErrorEvent) -> bool:
    logging.error("Handlerda xatolik: %s", event.exception, exc_info=event.exception)
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer("Xatolik yuz berdi. Qayta urinib ko'ring.", show_alert=True)
        elif update.message:
            await update.message.answer("Xatolik yuz berdi. Qayta urinib ko'ring.")
    except Exception:
        logging.debug("Xato haqida foydalanuvchiga xabar berib bo'lmadi", exc_info=True)
    return True  # xato qayta ko'tarilmaydi, bot ishlashda davom etadi


# --- ASOSIY FUNKSIYA ---
async def main():
    if not TOKEN or TOKEN == "bu_yerga_bot_tokeningizni_yozing":
        raise SystemExit("BOT_TOKEN topilmadi. .env faylni tekshiring (namuna: .env.example).")

    init_db()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        while True:
            try:
                logging.info("Bot ishga tushdi...")
                await dp.start_polling(bot)
                break  # to'xtatish signali (Ctrl+C) bilan toza chiqildi
            except TelegramUnauthorizedError:
                raise SystemExit("Token noto'g'ri. BotFather'dan olingan tokenni .env ga qayta yozing.")
            except Exception:
                logging.exception("Polling to'xtadi, %s soniyadan keyin qayta uriniladi", RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot to'xtatildi.")