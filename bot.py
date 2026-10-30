
import asyncio
import logging
import os
import sys
from contextlib import suppress
from channel_bridge import ChannelBridge
from bookings import BookingShop
from publications import PublicationEditor
from editorial import EditorialControls
from news import NewsIntake
from account_reader import AccountReader
from source_health import SourceMonitor
from alert_rules import AlertControls
from html import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.enums import ChatType
from tickets import TicketStore, STATUS, message_media

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

load_dotenv(Path(__file__).with_name(".env"))
store = TicketStore(Path(__file__).with_name("tickets.sqlite3"))
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN", "").strip()


def read_ids(variable: str) -> set[int]:
    """Читає список Telegram ID з .env."""
    result = set()
    for item in os.getenv(variable, "").split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                logging.warning("Некоректний ID у %s", variable)
    return result


OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
ADMIN_IDS = read_ids("ADMIN_IDS")
MODERATOR_IDS = read_ids("MODERATOR_IDS")

# Головний адміністратор завжди має повний доступ.
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()


def get_role(user_id: int) -> str | None:
    if OWNER_ID and user_id == OWNER_ID:
        return "owner"
    if user_id in ADMIN_IDS:
        return "admin"
    if user_id in MODERATOR_IDS:
        return "moderator"
    return None


def main_menu(role: str, user_id: int = 0) -> ReplyKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"📨 Звернення · {store.one("SELECT count(*) FROM tickets WHERE status='new'")[0]} нових · 📩 {store.unread(user_id)}",
                callback_data="menu:tickets",
            ),
            InlineKeyboardButton(
                text="📝 Публікації",
                callback_data="menu:posts",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📡 Канали",
                callback_data="menu:channels",
            ),
            InlineKeyboardButton(
                text="📊 Статистика",
                callback_data="menu:stats",
            ),
        ],
    ]

    if role in ("owner", "admin"):
        rows.append([
            InlineKeyboardButton(
                text="👥 Користувачі",
                callback_data="menu:users",
            ),
            InlineKeyboardButton(
                text="⚙️ Налаштування",
                callback_data="menu:settings",
            ),
        ])

    if role in ('owner', 'admin'):
        rows.append([InlineKeyboardButton(text='🛍 Бронювання шевронів', callback_data='shop:admin')])
        paused=store.setting('publishing_paused','0')=='1'
        rows.append([InlineKeyboardButton(text='▶️ Відновити всі публікації' if paused else '🛑 Зупинити всі публікації',callback_data=f'control:pause:{0 if paused else 1}')])

    if role == "owner":
        rows.append([
            InlineKeyboardButton(
                text="🛡 Адміністратори",
                callback_data="menu:admins",
            ),
            InlineKeyboardButton(
                text="📋 Журнал",
                callback_data="menu:logs",
            ),
        ])

    rows.append([
        InlineKeyboardButton(
            text="🔄 Оновити меню",
            callback_data="menu:home",
        )
    ])

    return keyboard([[(button.text, button.callback_data) for button in row] for row in rows], user_id)


STAT_PERIODS = {'today': 'Сьогодні', 'week': '7 днів', 'month': '30 днів', 'all': 'Увесь час'}


def statistics_period(period, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('Europe/Kyiv'))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {'today': 0, 'week': 6, 'month': 29}
    start = midnight - timedelta(days=days[period]) if period != 'all' else datetime(1970, 1, 1, tzinfo=timezone.utc)
    # Include messages in the current second; SQLite stores whole seconds.
    end = now.replace(microsecond=0) + timedelta(seconds=1)
    return start.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), now


async def show_statistics(callback, period):
    admin_id = callback.from_user.id
    start, end, now = statistics_period(period)
    data = store.statistics(start, end, admin_id, ADMIN_IDS | MODERATOR_IDS)
    status = data['statuses']
    seconds = data['response_seconds']
    if seconds is None:
        response = 'ще немає відповідей'
    elif seconds < 60:
        response = 'менше хвилини'
    elif seconds < 3600:
        response = f'{round(seconds / 60)} хв'
    else:
        response = f'{seconds / 3600:.1f} год'
    answered = f"{data['replied']} із {data['tickets']}"
    text = (
        f"📊 <b>Статистика · {STAT_PERIODS[period]}</b>\n"
        f"Оновлено {now:%d.%m.%Y о %H:%M} · Київ\n\n"
        f"<b>За обраний період</b>\n"
        f"👤 Нових користувачів: {data['new_users']}\n"
        f"💬 Написали користувачів: {data['active_users']}\n"
        f"📨 Створено звернень: {data['tickets']}\n"
        f"📥 Повідомлень від користувачів: {data['incoming']}\n"
        f"📤 Повідомлень від команди: {data['outgoing']}\n\n"
        f"<b>Поточні статуси цих звернень</b>\n"
        f"🆕 Нові: {status['new']}\n"
        f"🛠 У роботі: {status['working']}\n"
        f"⏳ Очікують користувача: {status['waiting']}\n"
        f"✅ Закриті: {status['closed']}\n\n"
        f"<b>Відповіді на ці звернення</b>\n"
        f"Отримали відповідь: {answered}\n"
        f"Середній час першої відповіді: {response}\n\n"
        f"<b>Зараз у боті · за весь час</b>\n"
        f"👥 Користувачів: {data['users']} · заблокованих: {data['blocked']}\n"
        f"📂 Відкритих звернень: {data['open_now']}\n"
        f"📩 Непрочитаних вами повідомлень: {data['unread']}"
    )
    rows = [[('📅 Сьогодні', 'stats:today'), ('🗓 7 днів', 'stats:week')],
            [('📆 30 днів', 'stats:month'), ('📈 Увесь час', 'stats:all')],
            [('🔄 Оновити статистику', f'stats:{period}')], [('🏠 Меню', 'menu:home')]]
    await callback.message.answer(text, parse_mode='HTML', reply_markup=keyboard(rows, admin_id))
    await callback.answer()


@dp.callback_query(F.data.startswith('stats:'))
async def statistics_callback(callback):
    if callback.message.chat.type != ChatType.PRIVATE or not get_role(callback.from_user.id):
        await callback.answer('⛔ Немає доступу.', show_alert=True)
        return
    period = callback.data.split(':', 1)[1]
    if period not in STAT_PERIODS:
        await callback.answer('Невідомий період.')
        return
    store.cancel(callback.from_user.id)
    await show_statistics(callback, period)


DEFAULT_WELCOME = "Вітаю! 👋\nЦе офіційний бот для зв'язку.\nНадішли повідомлення, і команда його опрацює."
DEFAULT_RECEIPT = 'Повідомлення отримано. Команда відповість у цьому чаті.'


async def show_users(callback, page=0):
    admin_id = callback.from_user.id
    query = store.setting(f'search:{admin_id}')
    where = "WHERE instr(casefold(u.name),?)>0 OR instr(casefold(u.username),?)>0 OR CAST(u.user_id AS TEXT)=?"
    args = (query.casefold().lstrip('@'), query.casefold().lstrip('@'), query)
    total = store.one(f'SELECT count(*) FROM users u {where}', args)[0]
    page = max(0, min(page, max(0, (total-1)//8)))
    users = store.db.execute(f'SELECT u.* FROM users u {where} ORDER BY last_seen DESC,user_id LIMIT 8 OFFSET ?', (*args, page*8)).fetchall()
    rows = [[(f"{'🚫' if u['blocked'] else '👤'} {u['name'][:24]} · {u['user_id']}", f"users:open:{u['user_id']}")] for u in users]
    nav = []
    if page:
        nav.append(('⬅️ Попередні користувачі', f'users:list:{page-1}'))
    if (page+1)*8 < total:
        nav.append(('Наступні користувачі ➡️', f'users:list:{page+1}'))
    if nav:
        rows.append(nav)
    rows += [[('🔎 Знайти користувача', 'users:search')], [('👥 Усі користувачі', 'menu:users'), ('🏠 Меню', 'menu:home')]]
    summary = f'👥 Користувачі · {total}\n\n'
    summary += f'Пошук: {query}\n' if query else ''
    summary += f'Сторінка {page+1}. Оберіть людину внизу.' if users else 'Користувачів не знайдено.'
    await callback.message.answer(summary, reply_markup=keyboard(rows, admin_id))
    await callback.answer()


async def show_user(callback, user_id):
    user = store.one('SELECT * FROM users WHERE user_id=?', (user_id,))
    if not user:
        await callback.answer('Користувача не знайдено.', show_alert=True)
        return
    counts = store.one("SELECT count(*),coalesce(sum(status!='closed'),0) FROM tickets WHERE user_id=?", (user_id,))
    contact = '@'+user['username'] if user['username'] else 'Без username'
    rows = [[('📨 Звернення користувача', f'users:tickets:{user_id}:0')]]
    if not get_role(user_id):
        rows.append([(('🔓 Розблокувати' if user['blocked'] else '🚫 Заблокувати') + f' · {user_id}', f"users:{'unblock' if user['blocked'] else 'block'}:{user_id}")])
    rows.append([('⬅️ Користувачі', 'users:list:0'), ('🏠 Меню', 'menu:home')])
    await callback.message.answer(
        f"<b>{escape(user['name'])}</b>\n{escape(contact)} · ID {user_id}\n\n"
        f"Статус: {'Заблокований' if user['blocked'] else 'Активний'}\n"
        f"Звернень: {counts[0]} · відкритих: {counts[1]}\n"
        f"Остання активність: {user['last_seen'][:16]} UTC",
        parse_mode='HTML', reply_markup=keyboard(rows, callback.from_user.id))
    await callback.answer()


async def show_settings(callback):
    admin_id = callback.from_user.id
    enabled = store.setting(f'notify:{admin_id}', '1') == '1'
    accepting = store.setting('accepting', '1') == '1'
    rows = [[(f"🔔 Мої сповіщення: {'увімкнено' if enabled else 'вимкнено'}", f"settings:notify:{0 if enabled else 1}")],
            [(f"📨 Приймання: {'відкрито' if accepting else 'призупинено'}", f"settings:accepting:{0 if accepting else 1}")],
            [('✏️ Змінити привітання', 'settings:edit:welcome')],
            [('✏️ Змінити підтвердження', 'settings:edit:receipt')],
            [('🛍 Бронювання: '+('увімкнено' if store.setting('bookings_enabled','0')=='1' else 'вимкнено'), 'shop:admin')],
            [('🏠 Меню', 'menu:home')]]
    await callback.message.answer(
        '⚙️ <b>Налаштування</b>\n\n'
        f"<b>Привітання /start</b>\n{escape(store.setting('welcome', DEFAULT_WELCOME))}\n\n"
        f"<b>Підтвердження звернення</b>\n{escape(store.setting('receipt', DEFAULT_RECEIPT))}\n\n"
        'Сповіщення змінюються лише для вас. Приймання та тексти — для всього бота.',
        parse_mode='HTML', reply_markup=keyboard(rows, admin_id))
    await callback.answer()


@dp.callback_query(F.data.startswith('users:') | F.data.startswith('settings:'))
async def management_callback(callback):
    admin_id = callback.from_user.id
    if callback.message.chat.type != ChatType.PRIVATE or get_role(admin_id) not in ('owner', 'admin'):
        await callback.answer('⛔ Недостатньо прав.', show_alert=True)
        return
    parts = callback.data.split(':')
    store.cancel(admin_id)
    if parts[0] == 'settings':
        if len(parts) != 3:
            await callback.answer('Некоректна дія.')
            return
        action, value = parts[1:]
        if action == 'notify' and value in ('0', '1'):
            store.set_setting(f'notify:{admin_id}', value)
        elif action == 'accepting' and value in ('0', '1'):
            store.set_setting('accepting', value)
        elif action == 'edit' and value in ('welcome', 'receipt'):
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)', (admin_id, value))
            await callback.message.answer('Надішліть новий текст (до 1200 символів).\n/cancel — скасувати.', reply_markup=keyboard([[('⬅️ Налаштування', 'menu:settings'), ('🏠 Меню', 'menu:home')]], admin_id))
            await callback.answer()
            return
        else:
            await callback.answer('Некоректна дія.')
            return
        await show_settings(callback)
        return
    action = parts[1] if len(parts)>1 else ''
    if action == 'search':
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)', (admin_id, 'search'))
        await callback.message.answer('Введіть ім’я, @username або Telegram ID.', reply_markup=keyboard([[('⬅️ Користувачі', 'menu:users'), ('🏠 Меню', 'menu:home')]], admin_id))
        await callback.answer()
        return
    if len(parts)<3 or not parts[2].isdigit():
        await callback.answer('Некоректна дія.')
        return
    value = int(parts[2])
    if action == 'list':
        await show_users(callback, value)
    elif action in ('open', 'block', 'unblock'):
        if action in ('block', 'unblock'):
            if get_role(value):
                await callback.answer('Не можна блокувати адміністрацію.', show_alert=True)
                return
            with store.db:
                store.db.execute('UPDATE users SET blocked=? WHERE user_id=?', (int(action=='block'), value))
        await show_user(callback, value)
    elif action == 'tickets':
        page = int(parts[3]) if len(parts)>3 and parts[3].isdigit() else 0
        total = store.one('SELECT count(*) FROM tickets WHERE user_id=?', (value,))[0]
        page = max(0, min(page, max(0, (total-1)//8)))
        tickets = store.db.execute('SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 8 OFFSET ?', (value,page*8)).fetchall()
        rows = [[(f"№{t['id']} · {STATUS[t['status']]}", f"ticket:open:{t['id']}")] for t in tickets]
        nav = []
        if page:
            nav.append(('⬅️ Попередні звернення', f'users:tickets:{value}:{page-1}'))
        if (page+1)*8 < total:
            nav.append(('Наступні звернення ➡️', f'users:tickets:{value}:{page+1}'))
        if nav:
            rows.append(nav)
        rows.append([('⬅️ Картка користувача', f'users:open:{value}'), ('🏠 Меню', 'menu:home')])
        await callback.message.answer(f'📨 Звернення користувача {value} · {total}\nСторінка {page+1}', reply_markup=keyboard(rows, admin_id))
        await callback.answer()
    else:
        await callback.answer('Невідома дія.')


async def handle_admin_input(message):
    admin_id = message.from_user.id
    pending = store.one('SELECT mode FROM admin_inputs WHERE admin_id=?', (admin_id,))
    if not pending:
        return False
    if pending['mode'].startswith('filter_'):
        return await editorial.filter_input(message,pending['mode'])
    if pending['mode'].startswith('post_'):
        return await publications.input(message,pending['mode'])
    if pending['mode'].startswith('alerts_'):
        return await alerts.input(message,pending['mode'])
    if get_role(admin_id) not in ('owner', 'admin'):
        store.cancel(admin_id)
        await message.answer('⛔ Недостатньо прав.')
        return True
    if message.text and message.text.startswith('/'):
        return False
    if pending['mode'].startswith('bridge_'):
        return await bridge.input(message, pending['mode'])
    if not message.text or not message.text.strip() or len(message.text)>1200:
        await message.answer('Потрібен текст від 1 до 1200 символів. /cancel — скасувати.')
        return True
    mode = pending['mode']
    if mode == 'search':
        store.set_setting(f'search:{admin_id}', message.text.strip())
        store.cancel(admin_id)
        await show_users(KeyboardAction(message, ''), 0)
    elif mode in ('welcome', 'receipt'):
        store.set_setting(mode, message.text.strip())
        store.cancel(admin_id)
        await message.answer('✅ Текст збережено.')
        await show_settings(KeyboardAction(message, ''))
    return True


@dp.message(Command("start"))
async def start_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    user_id = message.from_user.id
    store.cancel(user_id)
    store.touch_user(user_id, message.from_user.full_name, message.from_user.username)
    role = get_role(user_id)
    argument = (message.text or '').split(maxsplit=1)
    if len(argument)>1 and argument[1].startswith('patch_') and argument[1][6:].isdigit():
        await shop.action(KeyboardAction(message,'shop:view:'+argument[1][6:]))
        return

    if role:
        await message.answer(
            "Вітаю! 👋\n"
            "Ти маєш доступ до системи керування.\n\n"
            "Обери потрібний розділ на клавіатурі внизу.",
            reply_markup=main_menu(role, user_id)
        )
    else:
        await message.answer(
            store.setting("welcome", DEFAULT_WELCOME), reply_markup=shop.buyer_menu(user_id)
        )


@dp.message(Command("myid"))
async def myid_handler(message: Message):
    await message.answer(f"Твій Telegram ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Відкрийте /admin в особистому чаті з ботом.")
        return
    store.cancel(message.from_user.id)
    role = get_role(message.from_user.id)

    if not role:
        await message.answer("⛔ У тебе немає доступу до адмінменю.")
        logging.warning(
            "Відхилено доступ до /admin для user_id=%s",
            message.from_user.id,
        )
        return

    role_names = {
        "owner": "Головний адміністратор",
        "admin": "Адміністратор",
        "moderator": "Модератор",
    }

    await message.answer(
        f"🛡 Панель керування\n"
        f"Роль: {role_names[role]}\n\n"
        "Обери потрібний розділ:",
        reply_markup=main_menu(role, message.from_user.id),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def menu_handler(callback: CallbackQuery):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("Відкрийте бота в особистому чаті.", show_alert=True)
        return
    role = get_role(callback.from_user.id)

    # Повторна перевірка доступу на кожне натискання.
    if not role:
        await callback.answer(
            "⛔ Немає доступу.",
            show_alert=True,
        )
        return

    section = callback.data.split(":", 1)[1]

    if section == "tickets":
        store.cancel(callback.from_user.id)
        await show_list(callback, 0)
        return

    if section == "home":
        store.cancel(callback.from_user.id)
        await callback.message.answer(
            "🛡 Панель керування\n\nОбери потрібний розділ:",
            reply_markup=main_menu(role, callback.from_user.id),
        )
        await callback.answer()
        return

    # Додатковий захист розділів.
    if section in ("users", "settings", "channels") and role not in ("owner", "admin"):
        await callback.answer("⛔ Недостатньо прав.", show_alert=True)
        return

    if section in ("admins", "logs") and role != "owner":
        await callback.answer("⛔ Доступний лише головному адміністратору.",
                              show_alert=True)
        return

    store.cancel(callback.from_user.id)
    if section == 'posts':
        await publications.dashboard(publications.actor_message(callback))
        await callback.answer()
        return
    if section == 'channels':
        await bridge.dashboard(callback)
        return
    if section == 'stats':
        await show_statistics(callback, 'today')
        return
    if section == 'users':
        store.set_setting(f'search:{callback.from_user.id}', '')
        await show_users(callback, 0)
        return
    if section == 'settings':
        await show_settings(callback)
        return

    sections = {
        "tickets": (
            "📨 Звернення",
            "Тут буде список звернень користувачів, їхні статуси "
            "та відповіді через бота."
        ),
        "posts": (
            "📝 Публікації",
            "Тут буде редактор тексту, підписів, кнопок, "
            "чернеток і запланованих публікацій."
        ),
        "channels": (
            "📡 Канали",
            "Тут налаштовуватимуться канали для публікацій."
        ),
        "stats": (
            "📊 Статистика",
            "Тут з'являться дані про користувачів, звернення "
            "та публікації."
        ),
        "users": (
            "👥 Користувачі",
            "Тут буде керування користувачами та доступами."
        ),
        "settings": (
            "⚙️ Налаштування",
            "Тут будуть налаштування системи."
        ),
        "admins": (
            "🛡 Адміністратори",
            "Список адміністраторів поки налаштовується через файл .env."
        ),
        "logs": (
            "📋 Журнал",
            "Тут буде журнал адміністративних дій."
        ),
    }

    title, description = sections.get(
        section,
        ("Розділ", "Цей розділ ще не налаштований."),
    )

    section_keyboard = keyboard([[('⬅️ До головного меню', 'menu:home')]], callback.from_user.id)

    await callback.message.answer(
        f"{title}\n\n{description}",
        reply_markup=section_keyboard,
    )
    await callback.answer()


def keyboard(rows, user_id):
    buttons = []
    with store.db:
        for row in rows:
            rendered = []
            for text, data in row:
                # Bind ticket actions visibly to their ticket, including in notifications.
                parts = data.split(':')
                if parts[0] == 'ticket' and parts[1] != 'list' and not text.startswith(('№', '#')):
                    text = f"{text} #{parts[2]}"
                store.db.execute(
                    'INSERT OR REPLACE INTO keyboard_choices VALUES(?,?,?)',
                    (user_id, text, data),
                )
                rendered.append(KeyboardButton(text=text))
            buttons.append(rendered)
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True,
                               is_persistent=True, input_field_placeholder='Обери дію або напиши повідомлення')


class KeyboardAction:
    """Use the same permission-checked actions for bottom and legacy inline buttons."""
    def __init__(self, message, data):
        self.message = message
        self.from_user = message.from_user
        self.data = data

    async def answer(self, text=None, **kwargs):
        if text:
            await self.message.answer(text)


async def handle_keyboard(message):
    if not message.text:
        return False
    choice = store.one('SELECT action FROM keyboard_choices WHERE user_id=? AND label=?',
                       (message.from_user.id, message.text))
    if not choice:
        return False
    action = KeyboardAction(message, choice[0])
    if choice[0].startswith('news:'):
        await news.action(action)
        return True
    if choice[0].startswith('shop:'):
        await shop.action(action)
        return True
    if not get_role(message.from_user.id):
        return False
    if choice[0].startswith('menu:'):
        await menu_handler(action)
    elif choice[0].startswith('control:'):
        await editorial.action(action)
    elif choice[0].startswith('post:'):
        await publications.action(action)
    elif choice[0].startswith('bridge:'):
        await bridge.action(action)
    elif choice[0].startswith('alert:'):
        await alerts.action(action)
    elif choice[0].startswith('stats:'):
        await statistics_callback(action)
    elif choice[0].startswith(('users:', 'settings:')):
        await management_callback(action)
    else:
        await ticket_callback(action)
    return True


async def edit(callback, text, markup):
    try:
        await callback.message.answer(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def show_list(callback, page):
    total = store.one("SELECT count(*) FROM tickets")[0]
    page = max(0, min(page, max(0, (total - 1) // 10)))
    items = store.db.execute("SELECT * FROM tickets ORDER BY (status='closed'), id DESC LIMIT 10 OFFSET ?", (page * 10,)).fetchall()
    rows = []
    for ticket in items:
        count = store.unread(callback.from_user.id, ticket['id'])
        rows.append([(f"№{ticket['id']} · {ticket['name'][:22]}" + (f" · 📩 {count}" if count else ""), f"ticket:open:{ticket['id']}")])
    nav = []
    if page:
        nav.append(("⬅️", f"ticket:list:{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(("➡️", f"ticket:list:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [[("🔄 Оновити", f"ticket:list:{page}")], [("⬅️ Меню", "menu:home")]]
    await edit(callback, f"📨 Звернення: {total}\n📩 Непрочитаних повідомлень: {store.unread(callback.from_user.id)}\nСторінка {page+1}\nОберіть звернення на клавіатурі внизу.", keyboard(rows, callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data.startswith("ticket:"))
async def ticket_callback(callback: CallbackQuery):
    admin_id = callback.from_user.id
    if not get_role(admin_id) or callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer("⛔ Немає доступу.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        await callback.answer("Некоректна дія.")
        return
    action, ticket_id = parts[1], int(parts[2])
    store.bookings.clear(admin_id)
    with store.db:
        store.db.execute('DELETE FROM admin_inputs WHERE admin_id=?', (admin_id,))
    if action == "list":
        store.cancel(admin_id)
        await show_list(callback, ticket_id)
        return
    ticket = store.ticket(ticket_id)
    if ticket is None:
        await callback.answer("Звернення не знайдено.", show_alert=True)
        return
    if action == "reply":
        if store.one('SELECT blocked FROM users WHERE user_id=?', (ticket['user_id'],)) and store.one('SELECT blocked FROM users WHERE user_id=?', (ticket['user_id'],))[0]:
            await callback.answer('Користувача заблоковано. Спершу розблокуйте його.', show_alert=True)
            return
        if ticket['status'] == 'closed':
            await callback.answer("Спершу відкрийте звернення знову.", show_alert=True)
            return
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)', (admin_id, ticket_id))
            store.db.execute("UPDATE tickets SET status='working' WHERE id=?", (ticket_id,))
        await callback.message.answer(f"✍️ Відповідь для {ticket['name']} · №{ticket_id}\n\nНадішліть текст або вкладення.\nКоли закінчите — натисніть «Завершити відповідь».", reply_markup=keyboard([[("✅ Завершити відповідь", "menu:home")], [("⬅️ Список звернень", "ticket:list:0")]], admin_id))
        await callback.answer()
        return
    if action in ('new', 'working', 'waiting', 'closed'):
        with store.db:
            store.db.execute('UPDATE tickets SET status=? WHERE id=?', (action, ticket_id))
            if action == 'closed':
                store.db.execute('DELETE FROM sessions WHERE ticket_id=?', (ticket_id,))
        ticket = store.ticket(ticket_id)
    elif action not in ('open', 'history'):
        await callback.answer("Невідома дія.")
        return
    store.cancel(admin_id)
    await callback.answer()
    before = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    messages = []
    if action == 'history':
        messages = store.db.execute(
            'SELECT * FROM messages WHERE ticket_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 10',
            (ticket_id, before, before),
        ).fetchall()
        await send_history(callback.message, admin_id, ticket_id, messages)
    latest = store.one("SELECT * FROM messages WHERE ticket_id=? AND direction='in' ORDER BY id DESC LIMIT 1", (ticket_id,))
    rows = []
    if ticket['status'] != 'closed':
        rows += [[("✍️ Відповісти", f"ticket:reply:{ticket_id}")],
                 [("🛠 У роботі", f"ticket:working:{ticket_id}"), ("✅ Закрити", f"ticket:closed:{ticket_id}")]]
    else:
        rows.append([("🔓 Відкрити знову", f"ticket:new:{ticket_id}")])
    rows.append([("💬 Історія", f"ticket:history:{ticket_id}")])
    if messages and store.one('SELECT id FROM messages WHERE ticket_id=? AND id<? LIMIT 1', (ticket_id, messages[-1]['id'])):
        rows.append([("⬅️ Раніше", f"ticket:history:{ticket_id}:{messages[-1]['id']}")])
    rows.append([("⬅️ Звернення", "ticket:list:0"), ("🏠 Меню", "menu:home")])
    unread = store.unread(admin_id, ticket_id)
    await callback.message.answer(ticket_card(ticket, latest, unread), parse_mode='HTML', reply_markup=keyboard(rows, admin_id))


def ticket_card(ticket, latest, unread):
    order = store.one('SELECT id FROM orders WHERE ticket_id=?', (ticket['id'],))
    order_label = f'📦 Замовлення №{order[0]}\n' if order else ''
    contact = '@' + ticket['username'] if ticket['username'] else 'Без username'
    preview = 'Відкрийте історію, щоб переглянути попередні повідомлення.'
    if latest and latest['body']:
        preview = latest['body'][:600] + ('…' if len(latest['body']) > 600 else '')
    elif latest and latest['content_type']:
        preview = '📎 Вкладення — доступне в історії.'
    return (f"<b>Звернення №{ticket['id']}</b> · {escape(STATUS[ticket['status']])}\n{order_label}\n"
            f"<b>{escape(ticket['name'])}</b>\n{escape(contact)} · ID {ticket['user_id']}\n\n"
            f"<b>Останнє повідомлення</b>\n{escape(preview)}\n\n"
            f"📩 Непрочитаних: {unread}")


async def send_history(message, admin_id, ticket_id, messages):
    chunk = f'💬 Історія звернення №{ticket_id}\nЧас повідомлень: UTC\n'
    delivered = []
    for item in reversed(messages):
        who = '👤 Користувач' if item['direction'] == 'in' else '💬 Команда'
        header = f"\n{who} · {item['created'][:16]}\n"
        if item['body'] is not None:
            block = header + item['body'] + '\n'
            # Telegram text limit: split long messages without discarding content.
            while block:
                available = 1800 - len(chunk)
                chunk += block[:available]
                block = block[available:]
                if len(chunk) >= 1800:
                    await message.answer(chunk)
                    chunk = ''
        if item['content_type'] != 'text':
            if chunk:
                await message.answer(chunk)
                chunk = ''
            try:
                await bot.copy_message(admin_id, item['chat_id'], item['message_id'])
            except TelegramAPIError:
                await message.answer('⚠️ Попереднє повідомлення недоступне в Telegram.')
                continue
        delivered.append(item['id'])
    if chunk:
        await message.answer(chunk)
    store.mark_read(admin_id, delivered)



@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    store.cancel(message.from_user.id)
    role = get_role(message.from_user.id)
    await message.answer("Введення завершено. Збережені чернетки й замовлення залишилися в боті.", reply_markup=main_menu(role, message.from_user.id) if role else shop.buyer_menu(message.from_user.id))


@dp.message(Command('shop', 'orders'))
async def shop_command(message: Message):
    action = 'shop:mine:0' if (message.text or '').split()[0].split('@')[0]=='/orders' else 'shop:catalog:0'
    await shop.action(KeyboardAction(message,action))


@dp.message(Command('news'))
async def news_command(message: Message):
    await news.action(KeyboardAction(message,'news:new'))


@dp.message()
async def user_message_handler(message: Message):
    if message.chat.type != ChatType.PRIVATE or not message.from_user:
        return
    user_id = message.from_user.id
    if await handle_keyboard(message):
        return
    if await handle_admin_input(message):
        return
    store.touch_user(user_id, message.from_user.full_name, message.from_user.username)
    if not get_role(user_id):
        user = store.one('SELECT blocked FROM users WHERE user_id=?', (user_id,))
        if user['blocked']:
            await message.answer('⛔ Надсилання звернень для вашого облікового запису обмежено.')
            return
    if await news.input(message):
        return
    if await shop.input(message):
        return
    if not get_role(user_id) and store.setting('accepting','1')!='1':
        await message.answer('Приймання звернень тимчасово призупинено. Спробуйте пізніше.')
        return
    if message.text and message.text.startswith('/'):
        await message.answer("Невідома команда. /start — довідка, /cancel — завершити відповідь.")
        return
    if message.content_type not in {'text', 'photo', 'video', 'document', 'voice', 'audio', 'animation', 'sticker', 'video_note', 'contact', 'location', 'venue', 'poll', 'dice'}:
        await message.answer("Надішліть текст, фото, відео, документ або голосове повідомлення.")
        return
    if get_role(user_id):
        session = store.session(user_id)
        if not session:
            await message.answer("Для відповіді відкрийте /admin → Звернення → Відповісти.")
            return
        ticket = store.ticket(session[0])
        if not ticket or ticket['status'] == 'closed':
            store.cancel(user_id)
            await message.answer("Звернення закрите. Відкрийте його знову через меню.")
            return
        blocked = store.one('SELECT blocked FROM users WHERE user_id=?', (ticket['user_id'],))
        if blocked and blocked[0]:
            store.cancel(user_id)
            await message.answer('Користувача заблоковано. Відповідь не надіслано.')
            return
        try:
            sent = await bot.copy_message(ticket['user_id'], message.chat.id, message.message_id)
        except TelegramAPIError:
            logging.warning("Не вдалося доставити відповідь на звернення %s", ticket['id'])
            await message.answer("⚠️ Відповідь не доставлена. Користувач міг заблокувати бота. Спробуйте пізніше.")
            return
        store.reply(ticket['id'], ticket['user_id'], sent.message_id, message.text or message.caption, str(message.content_type),media=message_media(message))
        await message.answer(f"✅ Відповідь на #{ticket['id']} доставлено. Можна надіслати ще повідомлення або /cancel.")
        return
    await record_ticket_message(message)


async def record_ticket_message(message, ticket_id=None):
    user_id = message.from_user.id
    ticket_id, added = store.receive(user_id, message.from_user.full_name, message.from_user.username, message.chat.id, message.message_id, message.text or message.caption, str(message.content_type), ticket_id=ticket_id,media=message_media(message))
    if not added:
        return
    await message.answer(f"✅ Звернення №{ticket_id}\n" + store.setting("receipt", DEFAULT_RECEIPT))
    for admin_id in ADMIN_IDS | MODERATOR_IDS:
        if store.setting(f'notify:{admin_id}', '1') != '1':
            continue
        try:
            await bot.send_message(admin_id, f"🔔 Нове повідомлення у зверненні #{ticket_id}\nВід: {message.from_user.full_name}\n📩 Непрочитаних у зверненні: {store.unread(admin_id, ticket_id)}", reply_markup=keyboard([[("📨 Відкрити звернення", f"ticket:open:{ticket_id}"), ("✍️ Відповісти", f"ticket:reply:{ticket_id}")], [("🛡 Меню", "menu:home")]], admin_id))
        except TelegramAPIError:
            logging.warning("Не вдалося сповістити адміністратора %s про #%s", admin_id, ticket_id)


bridge = ChannelBridge(sys.modules[__name__])
reader = AccountReader(sys.modules[__name__])
source_monitor = SourceMonitor(sys.modules[__name__])
alerts = AlertControls(sys.modules[__name__])
shop = BookingShop(sys.modules[__name__])
publications = PublicationEditor(sys.modules[__name__])
editorial = EditorialControls(sys.modules[__name__])
news = NewsIntake(sys.modules[__name__])


@dp.callback_query(F.data.startswith("news:"))
async def news_callback(callback: CallbackQuery):
    await news.action(callback)


@dp.callback_query(F.data.startswith("control:"))
async def editorial_callback(callback: CallbackQuery):
    await editorial.action(callback)


@dp.callback_query(F.data.startswith("post:"))
async def publication_callback(callback: CallbackQuery):
    await publications.action(callback)


@dp.callback_query(F.data.startswith("shop:"))
async def shop_callback(callback: CallbackQuery):
    await shop.action(callback)


@dp.callback_query(F.data.startswith('bridge:'))
async def bridge_callback(callback: CallbackQuery):
    await bridge.action(callback)


@dp.my_chat_member()
async def channel_membership_handler(event):
    await bridge.membership(event)


@dp.channel_post()
async def channel_post_handler(message: Message):
    await alerts.observe(message)
    await bridge.receive(message)


@dp.edited_channel_post()
async def edited_channel_post_handler(message: Message):
    await alerts.observe(message)


@dp.callback_query(F.data.startswith('alert:'))
async def alert_callback(callback: CallbackQuery):
    await alerts.action(callback)


async def main():
    if not TOKEN:
        raise RuntimeError(
            "Не знайдено BOT_TOKEN. Перевір файл D:\\TelegramBot\\.env"
        )

    if not OWNER_ID and not ADMIN_IDS and not MODERATOR_IDS:
        logging.warning(
            "Адміністратори не налаштовані. "
            "Додай OWNER_ID або ADMIN_IDS у .env."
        )

    logging.info("Бот запускається...")
    await bot.delete_webhook(drop_pending_updates=False)

    publisher = asyncio.create_task(bridge.worker())
    booking_expiry = asyncio.create_task(shop.expiry_worker())
    booking_menus = asyncio.create_task(shop.menu_worker())
    post_publisher = asyncio.create_task(publications.worker())
    account_reader = asyncio.create_task(reader.worker())
    source_health = asyncio.create_task(source_monitor.worker())
    try:
        await dp.start_polling(bot)
    finally:
        publisher.cancel()
        booking_expiry.cancel()
        booking_menus.cancel()
        post_publisher.cancel()
        account_reader.cancel()
        source_health.cancel()
        with suppress(asyncio.CancelledError):
            await publisher
        with suppress(asyncio.CancelledError):
            await booking_expiry
        with suppress(asyncio.CancelledError):
            await booking_menus
        with suppress(asyncio.CancelledError):
            await post_publisher
        with suppress(asyncio.CancelledError):
            await account_reader
        with suppress(asyncio.CancelledError):
            await source_health
        await bot.session.close()


if __name__ == "__main__":
    from host_runtime import AlreadyRunning, single_instance
    try:
        with single_instance(Path(__file__).with_name('.runtime') / 'bot.lock'):
            asyncio.run(main())
    except AlreadyRunning as exc:
        logging.warning('%s', exc)
    finally:
        store.db.close()
