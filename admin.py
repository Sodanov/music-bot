import html
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import OWNER_ID

router = Router()


class Staff(BaseFilter):
    """Владелец или менеджер."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        uid = event.from_user.id
        return uid == OWNER_ID or await db.is_manager(uid)


class Owner(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user.id == OWNER_ID


# Админка работает только в личке и только для своих
router.message.filter(F.chat.type == "private", Staff())
router.callback_query.filter(Staff())


class AdEdit(StatesGroup):
    text = State()
    button_text = State()
    url = State()
    user_search = State()


async def panel_text() -> str:
    ad = await db.get_ad()
    status = "🟢 включена" if ad["enabled"] else "🔴 выключена"
    return (
        f"<b>Реклама:</b> {status}\n\n"
        f"<b>Текст:</b> {html.escape(ad['text']) or '—'}\n"
        f"<b>Текст кнопки:</b> {html.escape(ad['button_text']) or '—'}\n"
        f"<b>Ссылка:</b> {html.escape(ad['url']) or '—'}"
    )


def panel_kb(is_owner: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Текст", callback_data="adm:text")
    kb.button(text="🔘 Текст кнопки", callback_data="adm:button")
    kb.button(text="🔗 Ссылка", callback_data="adm:url")
    kb.button(text="🔁 Вкл/выкл", callback_data="adm:toggle")
    kb.button(text="👁 Предпросмотр", callback_data="adm:preview")
    if is_owner:
        kb.button(text="📊 Статистика", callback_data="adm:stats")
        kb.button(text="👥 Менеджеры", callback_data="adm:managers")
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено. /admin — панель рекламы.")


@router.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await panel_text(), reply_markup=panel_kb(m.from_user.id == OWNER_ID))


async def _refresh_panel(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_text(await panel_text(), reply_markup=panel_kb(cb.from_user.id == OWNER_ID))
    except TelegramBadRequest:
        pass  # «message is not modified»


@router.callback_query(F.data == "adm:text")
async def ask_text(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdEdit.text)
    await cb.message.answer("Пришлите текст рекламы (до 500 символов). Отмена — /cancel")
    await cb.answer()


@router.callback_query(F.data == "adm:button")
async def ask_button(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdEdit.button_text)
    await cb.message.answer("Пришлите текст кнопки (до 40 символов). Отмена — /cancel")
    await cb.answer()


@router.callback_query(F.data == "adm:url")
async def ask_url(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdEdit.url)
    await cb.message.answer("Пришлите ссылку для кнопки (http://, https:// или tg://). Отмена — /cancel")
    await cb.answer()


@router.message(AdEdit.text, F.text)
async def save_text(m: Message, state: FSMContext):
    if len(m.text) > 500:
        return await m.answer("Слишком длинно, максимум 500 символов.")
    await db.set_ad_field("text", m.text.strip())
    await state.clear()
    await m.answer("✅ Текст сохранён.\n\n" + await panel_text(), reply_markup=panel_kb(m.from_user.id == OWNER_ID))


@router.message(AdEdit.button_text, F.text)
async def save_button(m: Message, state: FSMContext):
    if len(m.text) > 40:
        return await m.answer("Слишком длинно, максимум 40 символов.")
    await db.set_ad_field("button_text", m.text.strip())
    await state.clear()
    await m.answer("✅ Кнопка сохранена.\n\n" + await panel_text(), reply_markup=panel_kb(m.from_user.id == OWNER_ID))


@router.message(AdEdit.url, F.text)
async def save_url(m: Message, state: FSMContext):
    url = m.text.strip()
    if not url.startswith(("http://", "https://", "tg://")) or " " in url:
        return await m.answer("Нужна корректная ссылка, начинающаяся с http://, https:// или tg://")
    await db.set_ad_field("url", url)
    await state.clear()
    await m.answer("✅ Ссылка сохранена.\n\n" + await panel_text(), reply_markup=panel_kb(m.from_user.id == OWNER_ID))


@router.callback_query(F.data == "adm:toggle")
async def toggle(cb: CallbackQuery):
    ad = await db.get_ad()
    if not ad["enabled"] and not ad["text"]:
        return await cb.answer("Сначала задайте текст рекламы", show_alert=True)
    await db.set_ad_field("enabled", 0 if ad["enabled"] else 1)
    await _refresh_panel(cb)
    await cb.answer("Готово")


@router.callback_query(F.data == "adm:preview")
async def preview(cb: CallbackQuery):
    ad = await db.get_ad()
    if not ad["text"]:
        return await cb.answer("Текст рекламы пуст", show_alert=True)
    markup = None
    if ad["button_text"] and ad["url"]:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=ad["button_text"], url=ad["url"])]]
        )
    await cb.message.answer(f"📢 <i>Реклама</i>: {html.escape(ad['text'])}", reply_markup=markup)
    await cb.answer()


# ---------- статистика: только владелец ----------

USERS_PER_PAGE = 10


def stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users:0"),
         InlineKeyboardButton(text="🔎 Найти пользователя", callback_data="adm:usersearch")],
    ])


async def stats_text() -> str:
    s = await db.get_stats()
    by = s["by_source"]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 <b>Пользователи:</b> {s['users_total']}\n"
        f"• новых: {s['users_new_day']} за 24 ч · {s['users_new_week']} за 7 дн\n"
        f"• активных: {s['users_active_day']} за 24 ч · {s['users_active_week']} за 7 дн\n\n"
        f"🔎 <b>Поисков:</b> {s['search_total']} "
        f"(24 ч: {s['search_day']} · 7 дн: {s['search_week']})\n"
        f"⬇️ <b>Скачиваний:</b> {s['download_total']} "
        f"(24 ч: {s['download_day']} · 7 дн: {s['download_week']})\n"
        f"   📺 YouTube: {by.get('youtube', 0)} · 🎶 каталоги: {by.get('catalog', 0)}"
    )
    if s["top_queries"]:
        top = "\n".join(
            f"{i}. {html.escape(q)} — {c}"
            for i, (q, c) in enumerate(s["top_queries"], 1)
        )
        text += f"\n\n🏆 <b>Топ запросов за 7 дней:</b>\n{top}"
    if s.get("campaigns"):
        campaigns = "\n".join(
            f"• <code>{html.escape(str(c['campaign']))}</code> — "
            f"{c['users']} уник. пользователей, запусков: {c['starts']}"
            for c in s["campaigns"]
        )
        text += f"\n\n📣 <b>Рекламные источники:</b>\n{campaigns}"
    else:
        text += (
            "\n\n📣 <b>Рекламные источники:</b> пока нет данных.\n"
            "Ссылка для кампании: <code>https://t.me/ИМЯ_БОТА?start=ads_telegram</code>"
        )
    return text


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M UTC+3")


def users_keyboard(offset: int, total: int, search: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=(
                f"adm:users:{max(0, offset - USERS_PER_PAGE)}"
                + (f":{search[:32]}" if search else "")
            ),
        ))
    if offset + USERS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(
            text="Вперёд ➡️",
            callback_data=(
                f"adm:users:{offset + USERS_PER_PAGE}"
                + (f":{search[:32]}" if search else "")
            ),
        ))
    if nav:
        buttons.append(nav)
    if search is None:
        buttons.append([
            InlineKeyboardButton(text="🔎 Найти пользователя", callback_data="adm:usersearch")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="📋 Все пользователи", callback_data="adm:users:0")
        ])
    buttons.append([
        InlineKeyboardButton(text="↩️ К статистике", callback_data="adm:stats")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def users_text(offset: int = 0, search: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_users(search)
    offset = max(0, min(offset, max(0, total - 1)))
    rows = await db.list_users(offset=offset, limit=USERS_PER_PAGE, search=search)
    if search:
        title = f"🔎 <b>Результаты поиска:</b> {html.escape(search)}"
    else:
        title = "👥 <b>Пользователи</b>"
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = (offset // USERS_PER_PAGE) + 1
    lines = [title, f"Всего: <b>{total}</b> · Страница {page}/{pages}", ""]
    if not rows:
        lines.append("Пользователи не найдены.")
    for row in rows:
        username = f"@{html.escape(row['username'])}" if row["username"] else "нет username"
        first_name = html.escape(row["first_name"] or "Без имени")
        lines.append(
            f"👤 <b>{username}</b> — {first_name}\n"
            f"   ID: <code>{row['user_id']}</code>\n"
            f"   🕒 Первый запуск: {_format_ts(row['first_seen'])}\n"
            f"   🟢 Последняя активность: {_format_ts(row['last_seen'])}"
        )
    return "\n\n".join(lines), users_keyboard(offset, total, search)


@router.message(Command("stats"), Owner())
async def stats_cmd(m: Message):
    await m.answer(await stats_text(), reply_markup=stats_kb())


@router.callback_query(F.data == "adm:stats", Owner())
async def stats_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(await stats_text(), reply_markup=stats_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:users:"), Owner())
async def users_page(cb: CallbackQuery):
    try:
        parts = cb.data.split(":", 3)
        offset = int(parts[2])
        search = parts[3] if len(parts) > 3 else None
    except (ValueError, AttributeError):
        await cb.answer("Не удалось открыть страницу", show_alert=True)
        return
    text, markup = await users_text(offset, search)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


@router.callback_query(F.data == "adm:usersearch", Owner())
async def ask_user_search(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdEdit.user_search)
    await cb.message.answer(
        "Пришлите username (можно с @) или Telegram ID для поиска. Отмена — /cancel"
    )
    await cb.answer()


@router.message(AdEdit.user_search, F.text, Owner())
async def user_search(m: Message, state: FSMContext):
    term = m.text.strip()
    if not term:
        return await m.answer("Введите username или Telegram ID.")
    await state.clear()
    text, markup = await users_text(0, term)
    await m.answer(text, reply_markup=markup)


# ---------- менеджеры: только владелец ----------

@router.callback_query(F.data == "adm:managers", Owner())
async def managers(cb: CallbackQuery):
    ids = await db.list_managers()
    listing = "\n".join(f"• <code>{i}</code>" for i in ids) or "пока никого"
    await cb.message.answer(
        f"<b>Менеджеры:</b>\n{listing}\n\n"
        "Добавить: <code>/addmanager ID</code>\nУдалить: <code>/delmanager ID</code>"
    )
    await cb.answer()


@router.message(Command("addmanager"), Owner())
async def add_manager(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        return await m.answer("Использование: /addmanager 123456789")
    await db.add_manager(int(command.args.strip()))
    await m.answer("✅ Менеджер добавлен.")


@router.message(Command("delmanager"), Owner())
async def del_manager(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        return await m.answer("Использование: /delmanager 123456789")
    ok = await db.remove_manager(int(command.args.strip()))
    await m.answer("✅ Удалён." if ok else "Такого менеджера нет.")
