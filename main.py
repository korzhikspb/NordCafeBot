import os
import logging
import html  # для экранирования в HTML
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ContentType
)
from aiogram.utils import executor
from dotenv import load_dotenv

from database import (
    init_db, add_registration, create_event, get_all_events, get_event_by_id,
    delete_event, delete_registrations_for_event, get_registrations_by_event, delete_registration
)

# -----------------------------
# НАСТРОЙКИ / ОКРУЖЕНИЕ
# -----------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

def parse_admin_ids(s: str) -> list:
    if not s:
        return []
    parts = [p.strip() for p in s.replace(";", ",").split(",")]
    ids = []
    for p in parts:
        try:
            ids.append(int(p))
        except Exception:
            pass
    return ids

# Резервные ID можно оставить как бэкап
ADMINS = parse_admin_ids(os.getenv("ADMIN_IDS", "")) or [21997374, 650845266]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# -----------------------------
# ФОРМАТЫ ДАТ/ВРЕМЕНИ
# -----------------------------
ISO_FMT = "%Y-%m-%d %H:%M"   # как хранится в БД
DISP_FMT = "%H:%M %d.%m"     # как показываем пользователю (без года)

def iso_to_disp(iso_str: str) -> str:
    """YYYY-MM-DD HH:MM -> HH:MM DD.MM"""
    try:
        return datetime.strptime(iso_str, ISO_FMT).strftime(DISP_FMT)
    except Exception:
        return iso_str  # на всякий случай, если формат неожиданный

# -----------------------------
# ТЕКСТЫ КНОПОК
# -----------------------------
BTN_EVENTS = "📅 Список мероприятий"
BTN_MYREGS = "📝 Мои записи"
BTN_BACK   = "⬅️ Назад"
BTN_CANCEL = "❌ Отмена"
BTN_SEND_PHONE = "📱 Отправить номер"
BTN_SEND_USERNAME = "👤 Отправить юзернейм"

# -----------------------------
# CALLBACK PREFIXES
# -----------------------------
CB_EVENT      = "ev"       # ev:<event_id> — показать карточку
CB_EVENT_LIST = "evlist"   # назад к списку
CB_SIGNUP     = "su"       # su:<event_id> — начало записи
CB_CANCEL_REG = "cancel"   # cancel:<event_id> — отмена записи

# -----------------------------
# "Состояния" простыми словарями
# -----------------------------
STEP_EVENT, STEP_NAME, STEP_SEATS, STEP_PHONE = range(4)
ADMIN_ADD_TITLE, ADMIN_ADD_DATETIME, ADMIN_ADD_PLACE, ADMIN_ADD_DESC = range(4)
ADMIN_DEL_WAIT_ID, ADMIN_DEL_CONFIRM = range(2)

user_states = {}    # per-user: запись на событие
add_states = {}     # per-admin: добавление события
delete_states = {}  # per-admin: удаление события

# -----------------------------
# КЛАВИАТУРЫ
# -----------------------------
def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton(BTN_EVENTS), KeyboardButton(BTN_MYREGS))
    return kb

def admin_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton("📋 Список участников"))
    kb.add(KeyboardButton("➕ Добавить мероприятие"))
    kb.add(KeyboardButton("❌ Удалить мероприятие"))
    kb.add(KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL))
    return kb

def back_cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL))
    return kb

def phone_request_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton(BTN_SEND_PHONE, request_contact=True),
           KeyboardButton(BTN_SEND_USERNAME))
    kb.add(KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL))
    return kb

def seats_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(KeyboardButton("1"), KeyboardButton("2"), KeyboardButton("3"), KeyboardButton("4"))
    kb.add(KeyboardButton(BTN_BACK), KeyboardButton(BTN_CANCEL))
    return kb

def events_inline_kb(events) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for ev in events:
        ev_id, name, desc, dt, place = ev
        title = f"{name} • {iso_to_disp(dt)}"
        kb.add(InlineKeyboardButton(title, callback_data=f"{CB_EVENT}:{ev_id}"))
    return kb

def details_inline_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Записаться", callback_data=f"{CB_SIGNUP}:{event_id}"))
    kb.add(InlineKeyboardButton("⬅️ К списку", callback_data=CB_EVENT_LIST))
    return kb

def myregs_back_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton(BTN_BACK))
    return kb

# -----------------------------
# HELPERS
# -----------------------------
def esc(s: str) -> str:
    """Экранируем текст для HTML parse_mode."""
    return html.escape(s or "")

async def send_lines_html(message: types.Message, lines, reply_markup=None):
    """
    Безопасно отправляет большой список в нескольких сообщениях (HTML parse_mode).
    """
    max_len = 4000  # запас к лимиту 4096
    buf = ""
    first = True
    for line in lines:
        part = (("\n" if buf else "") + line)
        if len(buf) + len(part) > max_len:
            await message.answer(buf, parse_mode="HTML",
                                 reply_markup=reply_markup if first else None)
            first = False
            buf = line
        else:
            buf += part
    if buf:
        await message.answer(buf, parse_mode="HTML",
                             reply_markup=reply_markup if first else None)
def reset_user_state(user_id: int):
    user_states.pop(user_id, None)

def reset_admin_states(user_id: int):
    add_states.pop(user_id, None)
    delete_states.pop(user_id, None)

def reg_seats_safe(reg_tuple) -> int:
    """Достаёт seats из кортежа регистрации (если есть), иначе 1."""
    try:
        return int(reg_tuple[3])
    except Exception:
        return 1

async def show_events_list(target) -> None:
    """
    Показать список будущих мероприятий (текст + инлайн кнопки).
    target: types.Message ИЛИ chat_id (int)
    """
    if isinstance(target, types.Message):
        chat_id = target.chat.id
        uid = target.from_user.id
    else:
        chat_id = int(target)
        uid = chat_id

    events = await get_all_events()
    now_str = datetime.now().strftime(ISO_FMT)
    upcoming_events = [ev for ev in events if ev[3] >= now_str]

    if not upcoming_events:
        await bot.send_message(chat_id, "📭 В настоящее время нет доступных мероприятий.", reply_markup=main_menu_kb())
        return

    user_states[uid] = {'step': STEP_EVENT, 'events': upcoming_events}
    await bot.send_message(chat_id, "Выберите мероприятие из списка:", reply_markup=back_cancel_kb())
    await bot.send_message(chat_id, "События:", reply_markup=events_inline_kb(upcoming_events))

# -----------------------------
# КОМАНДЫ: /start, /help, /whoami, /admin
# -----------------------------
@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    reset_user_state(message.from_user.id)
    reset_admin_states(message.from_user.id)
    await message.answer(
        "Привет! Я бот для записи на мероприятия NORD Coffee Base 💚\n"
        "Я помогу выбрать мероприятие, дату, время, узнать стоимость и записаться 📝\n"
        "Важно ☝️ Места бронируются только после предоплаты — с вами дополнительно свяжутся.\n"
        "Выберите действие в меню ниже:",
        reply_markup=main_menu_kb()
    )

@dp.message_handler(commands=['whoami'])
async def whoami(message: types.Message):
    await message.reply(f"Ваш Telegram ID: {message.from_user.id}")

@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMINS:
        return await message.reply("Эта команда доступна только администраторам.")
    reset_admin_states(message.from_user.id)
    await message.answer("Режим администрирования:\nВыберите действие.", reply_markup=admin_menu_kb())

# -----------------------------
# ПОЛЬЗОВАТЕЛЬСКИЙ ФЛОУ
# -----------------------------
@dp.message_handler(lambda m: m.text == BTN_EVENTS)
async def user_list_events(message: types.Message):
    await show_events_list(message)

@dp.message_handler(lambda m: m.text == BTN_BACK)
async def go_back(message: types.Message):
    uid = message.from_user.id
    # если админ в подшаге — вернём админ-меню
    if uid in add_states or uid in delete_states:
        reset_admin_states(uid)
        if uid in ADMINS:
            return await message.answer("Режим администрирования:\nВыберите действие.", reply_markup=admin_menu_kb())

    st = user_states.get(uid)
    if st and st.get('step') in (STEP_NAME, STEP_SEATS, STEP_PHONE, STEP_EVENT):
        return await show_events_list(message.chat.id)
    await message.answer("Возвращаемся в главное меню.", reply_markup=main_menu_kb())

@dp.message_handler(lambda m: m.text == BTN_CANCEL)
async def cancel_everything(message: types.Message):
    reset_user_state(message.from_user.id)
    reset_admin_states(message.from_user.id)
    # если это админ — после отмены тоже вернём в админ-меню
    if message.from_user.id in ADMINS:
        return await message.answer("Действие отменено. Вы в админ-меню.", reply_markup=admin_menu_kb())
    await message.answer("Действие отменено. Что дальше?", reply_markup=main_menu_kb())

# Fallback: пользователь ввел название вручную
@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == STEP_EVENT)
async def choose_event_fallback(message: types.Message):
    if message.text in (BTN_BACK, BTN_CANCEL):
        return
    st = user_states.get(message.from_user.id, {})
    events_list = st.get('events')
    if not events_list:
        return await show_events_list(message.chat.id)

    chosen_event = None
    for ev in events_list:
        ev_id, ev_name, _, _, _ = ev
        if message.text.strip().lower() == ev_name.lower() or message.text.strip().startswith(str(ev_id)):
            chosen_event = ev
            break
    if not chosen_event:
        return await message.reply("Пожалуйста, выберите мероприятие из списка кнопок.")

    ev_id, ev_name, ev_desc, ev_dt, ev_place = chosen_event
    lines = [f"🗓 {ev_name}", f"• Дата/время: {iso_to_disp(ev_dt)}", f"• Место: {ev_place or '(не указано)'}"]
    if ev_desc:
        lines.append(f"• Описание: {ev_desc}")
    await message.answer("\n".join(lines), reply_markup=details_inline_kb(ev_id))

# Инлайн: карточка события
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(f"{CB_EVENT}:"))
async def show_event_details_cb(call: types.CallbackQuery):
    try:
        event_id = int(call.data.split(":")[1])
    except Exception:
        return await call.answer("Неверный идентификатор события.", show_alert=True)

    ev = await get_event_by_id(event_id)
    if not ev:
        return await call.answer("Событие не найдено.", show_alert=True)

    ev_id, ev_name, ev_desc, ev_dt, ev_place = ev
    lines = [f"🗓 {ev_name}", f"• Дата/время: {iso_to_disp(ev_dt)}", f"• Место: {ev_place or '(не указано)'}"]
    if ev_desc:
        lines.append(f"• Описание: {ev_desc}")
    await call.message.answer("\n".join(lines), reply_markup=details_inline_kb(ev_id))
    await call.answer()

# Инлайн: назад к списку
@dp.callback_query_handler(lambda c: c.data == CB_EVENT_LIST)
async def back_to_event_list_cb(call: types.CallbackQuery):
    await show_events_list(call.message.chat.id)
    await call.answer()

# Инлайн: начать запись
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(f"{CB_SIGNUP}:"))
async def signup_cb(call: types.CallbackQuery):
    try:
        event_id = int(call.data.split(":")[1])
    except Exception:
        return await call.answer("Неверный идентификатор события.", show_alert=True)

    ev = await get_event_by_id(event_id)
    if not ev:
        return await call.answer("Событие не найдено.", show_alert=True)

    regs = await get_registrations_by_event(event_id)
    if any(reg[0] == call.from_user.id for reg in regs):
        return await call.answer("Вы уже записаны на это мероприятие.", show_alert=True)

    ev_id, ev_name, ev_desc, ev_dt, ev_place = ev
    user_states[call.from_user.id] = {'step': STEP_NAME, 'event_id': ev_id, 'event_name': ev_name}
    await call.message.answer(f"Отлично! Вы выбрали: \"{ev_name}\"\nКак вас зовут?", reply_markup=back_cancel_kb())
    await call.answer()

# Шаги записи: имя -> места -> телефон
@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == STEP_NAME)
async def step_name(message: types.Message):
    if message.text == BTN_BACK:
        return await show_events_list(message.chat.id)
    if message.text == BTN_CANCEL:
        return await cancel_everything(message)

    name = message.text.strip()
    user_states[message.from_user.id]['name'] = name
    user_states[message.from_user.id]['step'] = STEP_SEATS
    await message.answer("Сколько мест бронируете?\nВыберите 1–4:", reply_markup=seats_kb())

@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == STEP_SEATS)
async def step_seats(message: types.Message):
    if message.text == BTN_BACK:
        user_states[message.from_user.id]['step'] = STEP_NAME
        return await message.answer("Как вас зовут?", reply_markup=back_cancel_kb())
    if message.text == BTN_CANCEL:
        return await cancel_everything(message)

    try:
        seats = int(message.text.strip())
    except Exception:
        return await message.answer("Пожалуйста, выберите число от 1 до 4 кнопкой ниже.", reply_markup=seats_kb())

    if seats < 1 or seats > 4:
        return await message.answer("Можно выбрать от 1 до 4 мест.", reply_markup=seats_kb())

    user_states[message.from_user.id]['seats'] = seats
    user_states[message.from_user.id]['step'] = STEP_PHONE
    await message.answer("Укажите ваш номер телефона или @username:", reply_markup=phone_request_kb())

@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == STEP_PHONE,
                    content_types=[ContentType.TEXT, ContentType.CONTACT])
async def step_phone(message: types.Message):
    # обработка навигации
    if message.content_type == ContentType.TEXT:
        if message.text == BTN_BACK:
            user_states[message.from_user.id]['step'] = STEP_SEATS if 'seats' in user_states.get(message.from_user.id, {}) else STEP_NAME
            return await message.answer("Сколько мест бронируете? (1–4)" if user_states[message.from_user.id]['step'] != STEP_NAME else "Как вас зовут?",
                                        reply_markup=seats_kb() if user_states[message.from_user.id]['step'] != STEP_NAME else back_cancel_kb())
        if message.text == BTN_CANCEL:
            return await cancel_everything(message)

    st = user_states.get(message.from_user.id, {})
    if not st:
        return await message.answer("Сессия сброшена. Начните заново.", reply_markup=main_menu_kb())

    # получаем значение контакта: номер / @username / текст
    if message.content_type == ContentType.CONTACT and message.contact:
        contact_value = message.contact.phone_number
    elif message.content_type == ContentType.TEXT and message.text == BTN_SEND_USERNAME:
        uname = message.from_user.username
        if not uname:
            return await message.answer(
                "У вас не установлен @username в Telegram. Укажите его в настройках или введите номер/ник вручную:",
                reply_markup=phone_request_kb()
            )
        contact_value = f"@{uname}"
    else:
        contact_value = message.text.strip()

    event_id = st.get('event_id')
    name = st.get('name')
    seats = st.get('seats', 1)

    # защита от дубля
    registrations = await get_registrations_by_event(event_id)
    if any(reg[0] == message.from_user.id for reg in registrations):
        reset_user_state(message.from_user.id)
        return await message.answer("Вы уже записаны на это мероприятие.", reply_markup=main_menu_kb())

    # запись в БД (колонка seats уже есть)
    await add_registration(event_id, message.from_user.id, name, contact_value, seats)

    # уведомления админам
    for admin_id in ADMINS:
        try:
            await bot.send_message(
                admin_id,
                "💥 Новая запись на мероприятие:\n"
                f"• Мероприятие: {st.get('event_name')}\n"
                f"• Имя: {name}\n"
                f"• Контакт: {contact_value}\n"
                f"• Мест: {seats}"
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    reset_user_state(message.from_user.id)
    await message.answer(
        f"Спасибо, {name}! Вы зарегистрированы на \"{st.get('event_name')}\" (мест: {seats}).\n"
        "Администратор свяжется с вами в ближайшее время для подтверждения бронирования.",
        reply_markup=main_menu_kb()
    )

# -----------------------------
# «МОИ ЗАПИСИ» и отмена
# -----------------------------
@dp.message_handler(lambda m: m.text == BTN_MYREGS)
async def user_list_registrations(message: types.Message):
    events = await get_all_events()
    user_id = message.from_user.id
    user_regs = []
    for ev in events:
        ev_id, name, desc, dt, place = ev
        regs = await get_registrations_by_event(ev_id)
        for reg in regs:
            if reg[0] == user_id:
                seats = reg_seats_safe(reg)
                user_regs.append((ev_id, name, dt, place, seats))

    if not user_regs:
        return await message.answer("📭 У вас пока нет записей.", reply_markup=main_menu_kb())

    lines = ["Ваши записи:"]
    kb_inline = InlineKeyboardMarkup()
    for idx, (ev_id, ev_name, ev_dt, ev_place, seats) in enumerate(user_regs, start=1):
        lines.append(f"{idx}. {ev_name} – {iso_to_disp(ev_dt)} @ {ev_place or '(место не указано)'} — мест: {seats}")
        kb_inline.add(InlineKeyboardButton(f"❌ Отмена {idx}", callback_data=f"{CB_CANCEL_REG}:{ev_id}"))
    await message.answer("\n".join(lines), reply_markup=myregs_back_kb())
    await message.answer("Для отмены записи нажмите кнопку под соответствующим пунктом:", reply_markup=kb_inline)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith(f"{CB_CANCEL_REG}:"))
async def cancel_registration_callback(call: types.CallbackQuery):
    """Отмена записи пользователем; уведомляем админов."""
    user_id = call.from_user.id
    try:
        event_id = int(call.data.split(":")[1])
    except Exception:
        return await call.answer("Произошла ошибка при отмене записи.", show_alert=True)

    ev = await get_event_by_id(event_id)
    regs = await get_registrations_by_event(event_id)
    this_reg = next((r for r in regs if r[0] == user_id), None)

    await delete_registration(event_id, user_id)

    if ev and this_reg:
        _, ev_name, _, ev_dt, ev_place = ev
        _, reg_name, reg_phone = this_reg[0:3]
        seats = reg_seats_safe(this_reg)
        # уведомление админам
        for admin_id in ADMINS:
            try:
                await bot.send_message(
                    admin_id,
                    "❎ Отмена записи:\n"
                    f"• Мероприятие: {ev_name} ({iso_to_disp(ev_dt)}, {ev_place or 'место не указано'})\n"
                    f"• Имя: {reg_name}\n"
                    f"• Телефон: {reg_phone}\n"
                    f"• Мест: {seats}"
                )
            except Exception as e:
                logging.warning(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    # обновим пользователю список
    events = await get_all_events()
    still = []
    for ev2 in events:
        e_id, name, desc, dt, place = ev2
        rs = await get_registrations_by_event(e_id)
        for r in rs:
            if r[0] == user_id:
                still.append((e_id, name, dt, place, reg_seats_safe(r)))

    try:
        await call.message.edit_reply_markup()
    except Exception:
        pass

    if not still:
        await bot.send_message(user_id, "📭 У вас пока нет записей.", reply_markup=main_menu_kb())
    else:
        lines = ["Ваши записи:"]
        kb_inline = InlineKeyboardMarkup()
        for idx, (e_id, ev_name2, ev_dt2, ev_place2, seats2) in enumerate(still, start=1):
            lines.append(f"{idx}. {ev_name2} – {iso_to_disp(ev_dt2)} @ {ev_place2 or '(место не указано)'} — мест: {seats2}")
            kb_inline.add(InlineKeyboardButton(f"❌ Отмена {idx}", callback_data=f"{CB_CANCEL_REG}:{e_id}"))
        await bot.send_message(user_id, "\n".join(lines), reply_markup=myregs_back_kb())
        await bot.send_message(user_id, "Для отмены записи нажмите кнопку под соответствующим пунктом:", reply_markup=kb_inline)

    await call.answer("Запись отменена.")

# -----------------------------
# АДМИН: список участников / добавление / удаление
# -----------------------------
@dp.message_handler(lambda m: m.text == "📋 Список участников")
async def admin_list_participants(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    events = await get_all_events()
    if not events:
        return await message.answer("📭 Событий пока нет.", reply_markup=admin_menu_kb())

    lines = ["<b>📋 Список участников на каждое мероприятие:</b>"]
    for ev in events:
        ev_id, name, desc, dt, place = ev
        lines.append(f"<b>{esc(name)}</b> ({iso_to_disp(dt)}, {esc(place) if place else 'место не указано'})")
        regs = await get_registrations_by_event(ev_id)
        total = 0
        if regs:
            for reg in regs:
                # поддерживаем схему с seats (если есть)
                _, reg_name, reg_contact = reg[0:3]
                try:
                    seats = int(reg[3])
                except Exception:
                    seats = 1
                total += seats
                # Контакт в <code> — подчёркивания не сломают формат
                lines.append(f"• {esc(reg_name)} — <code>{esc(reg_contact)}</code> (мест: {seats})")
        else:
            lines.append("• (нет записей)")
        lines.append(f"Итого мест: {total}")
        lines.append("")  # пустая строка-разделитель

    await send_lines_html(message, lines, reply_markup=admin_menu_kb())

@dp.message_handler(lambda m: m.text == "➕ Добавить мероприятие")
async def admin_add_event_menu(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    add_states[message.from_user.id] = {'step': ADMIN_ADD_TITLE}
    await message.answer("🆕 Введите название мероприятия:", reply_markup=back_cancel_kb())

@dp.message_handler(lambda m: add_states.get(m.from_user.id, {}).get('step') == ADMIN_ADD_TITLE)
async def admin_add_title(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        reset_admin_states(message.from_user.id)
        return await cmd_admin(message)

    add_states[message.from_user.id]['title'] = message.text.strip()
    add_states[message.from_user.id]['step'] = ADMIN_ADD_DATETIME
    await message.answer("Введите дату и время в формате YYYY-MM-DD HH:MM:", reply_markup=back_cancel_kb())

@dp.message_handler(lambda m: add_states.get(m.from_user.id, {}).get('step') == ADMIN_ADD_DATETIME)
async def admin_add_datetime(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        add_states[message.from_user.id]['step'] = ADMIN_ADD_TITLE
        return await message.answer("Введите название мероприятия:", reply_markup=back_cancel_kb())

    dt_text = message.text.strip()
    try:
        dt_parsed = datetime.strptime(dt_text, ISO_FMT)
    except Exception:
        return await message.answer("❗ Неверный формат. Введите YYYY-MM-DD HH:MM:", reply_markup=back_cancel_kb())

    add_states[message.from_user.id]['date_time'] = dt_parsed.strftime(ISO_FMT)
    add_states[message.from_user.id]['step'] = ADMIN_ADD_PLACE
    await message.answer("Введите место проведения:", reply_markup=back_cancel_kb())

@dp.message_handler(lambda m: add_states.get(m.from_user.id, {}).get('step') == ADMIN_ADD_PLACE)
async def admin_add_place(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        add_states[message.from_user.id]['step'] = ADMIN_ADD_DATETIME
        return await message.answer("Введите дату и время YYYY-MM-DD HH:MM:", reply_markup=back_cancel_kb())

    add_states[message.from_user.id]['place'] = message.text.strip()
    add_states[message.from_user.id]['step'] = ADMIN_ADD_DESC
    await message.answer("Введите описание (или '-' если без описания):", reply_markup=back_cancel_kb())

@dp.message_handler(lambda m: add_states.get(m.from_user.id, {}).get('step') == ADMIN_ADD_DESC)
async def admin_add_description(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        add_states[message.from_user.id]['step'] = ADMIN_ADD_PLACE
        return await message.answer("Введите место проведения:", reply_markup=back_cancel_kb())

    st = add_states.pop(message.from_user.id, None)
    if st is None:
        return await message.answer("Сессия добавления сброшена.", reply_markup=admin_menu_kb())

    desc_text = message.text.strip()
    if desc_text == '-' or desc_text == '':
        desc_text = ''
    await create_event(st['title'], desc_text, st['date_time'], st['place'])

    await message.answer(
        f"✅ Событие \"{st['title']}\" создано:\n"
        f" • Дата/время: {iso_to_disp(st['date_time'])}\n"
        f" • Место: {st['place'] or '(не указано)'}\n"
        f" • Описание: {desc_text or '(не указано)'}",
        reply_markup=admin_menu_kb()
    )

@dp.message_handler(lambda m: m.text == "❌ Удалить мероприятие")
async def admin_delete_event_menu(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    delete_states[message.from_user.id] = {'step': ADMIN_DEL_WAIT_ID}
    events = await get_all_events()
    if not events:
        delete_states.pop(message.from_user.id, None)
        return await message.answer("Нет мероприятий для удаления.", reply_markup=admin_menu_kb())
    lst = "\n".join([f"{ev[0]}. {ev[1]} ({iso_to_disp(ev[3])})" for ev in events])
    await message.answer("Введите ID мероприятия, которое нужно удалить:\n" + lst, reply_markup=back_cancel_kb())

@dp.message_handler(lambda m: delete_states.get(m.from_user.id, {}).get('step') == ADMIN_DEL_WAIT_ID)
async def admin_delete_event_get_id(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        reset_admin_states(message.from_user.id)
        return await cmd_admin(message)

    try:
        event_id = int(message.text.strip())
    except Exception:
        return await message.answer("Пожалуйста, введите числовой ID мероприятия.", reply_markup=back_cancel_kb())

    ev = await get_event_by_id(event_id)
    if not ev:
        return await message.answer("Событие с таким ID не найдено. Попробуйте другой ID.", reply_markup=back_cancel_kb())

    delete_states[message.from_user.id] = {'step': ADMIN_DEL_CONFIRM, 'event_id': event_id, 'event_name': ev[1]}
    await message.answer(
        f"⚠️ Удалить \"{ev[1]}\"?\nВведите **ДА** для подтверждения или любой другой текст для отмены.",
        parse_mode='Markdown', reply_markup=back_cancel_kb()
    )

@dp.message_handler(lambda m: delete_states.get(m.from_user.id, {}).get('step') == ADMIN_DEL_CONFIRM)
async def admin_delete_event_confirm(message: types.Message):
    if message.text == BTN_CANCEL:
        reset_admin_states(message.from_user.id)
        return await message.answer("Действие отменено.", reply_markup=admin_menu_kb())
    if message.text == BTN_BACK:
        delete_states[message.from_user.id]['step'] = ADMIN_DEL_WAIT_ID
        return await admin_delete_event_menu(message)

    st = delete_states.pop(message.from_user.id, None)
    if not st:
        return await message.answer("Сессия удаления сброшена.", reply_markup=admin_menu_kb())

    if message.text.strip().lower() not in ["да", "yes"]:
        return await message.answer("Удаление отменено.", reply_markup=admin_menu_kb())

    event_id = st['event_id']
    await delete_registrations_for_event(event_id)
    await delete_event(event_id)
    await message.answer("🗑 Готово. Мероприятие и все связанные записи удалены.", reply_markup=admin_menu_kb())


# -----------------------------
# СТАРТ
# -----------------------------
async def on_startup(dp):
    await init_db()
    logging.info("База данных готова, бот запущен.")

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup)