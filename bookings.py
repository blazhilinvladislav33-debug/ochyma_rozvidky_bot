import asyncio
import logging
import re
import uuid
import time
from decimal import Decimal
from html import escape
from bookings_store import ORDER_STATUS
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest


def money(kop):
    return f'{kop // 100}.{kop % 100:02d} грн'


def parse_price(text):
    if not re.fullmatch(r'[0-9]{1,7}([.,][0-9]{1,2})?', text.strip()):
        raise ValueError('Введіть ціну у гривнях, наприклад 250 або 250,50.')
    price = int(Decimal(text.strip().replace(',','.')) * 100)
    if price<=0:
        raise ValueError('Ціна має бути більшою за нуль.')
    return price


def phone_number(text):
    digits = re.sub(r'[\s()+-]', '', text)
    if len(digits)==10 and digits.startswith('0'):
        digits = '38'+digits
    if not re.fullmatch(r'380[0-9]{9}', digits):
        raise ValueError('Введіть український номер: +380XXXXXXXXX або 0XXXXXXXXX.')
    return '+'+digits


class BookingShop:
    def __init__(self, app):
        self.app = app
        self.menu_wake = asyncio.Event()

    @property
    def store(self):
        return self.app.store

    @property
    def repo(self):
        return self.store.bookings

    def admin(self, user_id):
        return self.app.get_role(user_id) in ('owner','admin')

    def has_orders(self,user_id):
        return self.store.one('SELECT 1 FROM orders WHERE user_id=? LIMIT 1',(user_id,)) is not None

    def buyer_rows(self,user_id):
        rows=[]
        if self.store.setting('bookings_enabled','0')=='1':
            rows.append([('🛍 Шеврони','shop:catalog:0')])
        rows.append([('📨 Запропонувати новину','news:new')])
        if self.has_orders(user_id):
            rows.append([('📦 Мої замовлення','shop:mine:0')])
        rows.append([('💬 Написати команді','shop:contact'),('🔄 Оновити меню','shop:home')])
        return rows

    def buyer_menu(self,user_id):
        return self.app.keyboard(self.buyer_rows(user_id),user_id)

    def setup_rows(self):
        return [[('💳 Реквізити оплати','shop:card')],
                [('➕ Додати шеврон','shop:new'),('🧵 Каталог адміністратора','shop:products:0')],
                [('▶️ Увімкнути бронювання','shop:enable:1'),('⬅️ До бронювань','shop:admin')]]

    async def safe_send(self, user_id, text, **kwargs):
        try:
            await self.app.bot.send_message(user_id,text,**kwargs)
            return True
        except TelegramAPIError:
            logging.warning('Не вдалося сповістити user_id=%s щодо замовлення',user_id)
            return False

    async def notify_admins(self, order, text):
        for admin_id in self.app.ADMIN_IDS:
            await self.safe_send(admin_id, f'📦 Замовлення №{order["id"]}\n{text}',
                reply_markup=self.app.keyboard([[(f'📦 Відкрити №{order["id"]}',f'shop:order:{order["id"]}')], [('🛍 Бронювання','shop:admin')]],admin_id))

    async def dashboard(self, message):
        user_id = message.from_user.id
        enabled = self.store.setting('bookings_enabled','0')=='1'
        counts = {row['status']:row['n'] for row in self.store.db.execute('SELECT status,count(*) n FROM orders GROUP BY status')}
        rows = [[(('⏸ Вимкнути бронювання' if enabled else '▶️ Увімкнути бронювання'),f'shop:enable:{0 if enabled else 1}')],
                [('➕ Додати шеврон','shop:new'),('🧵 Каталог адміністратора','shop:products:0')],
                [('📦 Замовлення','shop:orders:0')], [('💳 Реквізити оплати','shop:card')],
                [('🏠 Меню','menu:home')]]
        card = self.store.setting('booking_card')
        missing=self.repo.readiness()
        ready_note=('\n\n<b>Що потрібно для запуску</b>\n'+'\n'.join('• '+escape(item) for item in missing)) if missing else '\n\n✅ Усе готово до приймання бронювань.'
        version=int(self.store.setting('booking_menu_version','0'))
        delivery={row['status']:row['n'] for row in self.store.db.execute('SELECT status,count(*) n FROM booking_menu_updates WHERE version=? GROUP BY status',(version,))}
        delivery_note=(f"\n\nОновлення меню користувачів: надіслано {delivery.get('sent',0)}, у черзі {delivery.get('pending',0)}, недоступних {delivery.get('failed',0)+delivery.get('skipped',0)}.") if delivery else ''
        await message.answer(
            f"🛍 <b>Бронювання шевронів</b>\n\nПриймання: {'увімкнене' if enabled else 'вимкнене'}\n"
            f"Очікують оплати: {counts.get('awaiting_payment',0)}\nНа перевірці: {counts.get('review',0)}\n"
            f"Оплачені: {counts.get('paid',0)} · відправлені: {counts.get('shipped',0)}\n\n"
            f"<b>Реквізити для нових замовлень</b>\n{escape(card or 'Ще не задано')}{ready_note}{delivery_note}\n\n"
            'Шеврони резервуються після підтвердження даних. На оплату та скриншот — 24 години. '
            'Заявки на перевірці зберігають бронь до рішення адміністратора. '
            'Вимкнення зупиняє нові бронювання; чинні замовлення залишаються доступними.',
            parse_mode='HTML', reply_markup=self.app.keyboard(rows,user_id))

    async def catalog(self, message, page=0, management=False):
        uid = message.from_user.id
        where = 'WHERE deleted=0' if management else 'WHERE deleted=0 AND active=1'
        total = self.store.one(f'SELECT count(*) FROM products {where}')[0]
        page = max(0,min(page,max(0,(total-1)//8)))
        products = self.store.db.execute(f'SELECT * FROM products {where} ORDER BY id DESC LIMIT 8 OFFSET ?', (page*8,)).fetchall()
        prefix = 'product' if management else 'view'
        rows = [[(f"№{p['id']} · {p['title'][:24]} · {money(p['price'])} · {p['stock']} шт.",f"shop:{prefix}:{p['id']}")] for p in products]
        nav = []
        route = 'products' if management else 'catalog'
        if page:
            nav.append(('⬅️ Попередні шеврони',f'shop:{route}:{page-1}'))
        if (page+1)*8<total:
            nav.append(('Наступні шеврони ➡️',f'shop:{route}:{page+1}'))
        if nav:
            rows.append(nav)
        rows.extend([[('⬅️ До бронювань','shop:admin')]] if management else self.buyer_rows(uid))
        await message.answer('🧵 <b>Шеврони</b>\n\n'+(f'Оберіть шеврон унизу. Сторінка {page+1}.' if products else 'Шевронів поки немає.'),parse_mode='HTML',reply_markup=self.app.keyboard(rows,uid))

    async def product_card(self, message, product_id, management=False):
        product = self.repo.product(product_id)
        if not product or (not management and not product['active']):
            await message.answer('Цей шеврон недоступний.',reply_markup=self.app.keyboard([[('🧵 Каталог адміністратора','shop:products:0')]],message.from_user.id) if management else self.buyer_menu(message.from_user.id))
            return
        rows = []
        if management:
            rows = [[(f'➕ Додати залишок №{product_id}',f'shop:restock:{product_id}'),(f'💰 Змінити ціну №{product_id}',f'shop:price:{product_id}')],
                    [(('🙈 Приховати' if product['active'] else '👁 Показати')+f' · №{product_id}',f'shop:visibility:{product_id}:{0 if product["active"] else 1}')],
                    [(f'🔗 Посилання для покупців №{product_id}',f'shop:link:{product_id}')],
                    [(f'🗑 Видалити товар №{product_id}',f'shop:deleteask:{product_id}')],
                    [('🧵 Каталог адміністратора','shop:products:0'),('⬅️ До бронювань','shop:admin')]]
        else:
            if product['stock']>0:
                rows.append([(f'🛒 Замовити шеврон №{product_id}',f'shop:buy:{product_id}')])
            rows.extend(self.buyer_rows(message.from_user.id))
        await message.answer_photo(product['photo_id'],caption=f"<b>{escape(product['title'])}</b>\n\nЦіна: {money(product['price'])}\nДоступно: {product['stock']} шт."+
            (f"\nУ каталозі: {'так' if product['active'] else 'ні'}" if management else ''),parse_mode='HTML',reply_markup=self.app.keyboard(rows,message.from_user.id))

    async def list_orders(self,message,page=0,all_orders=False):
        uid = message.from_user.id
        where,args = ('',()) if all_orders else ('WHERE user_id=?',(uid,))
        total = self.store.one(f'SELECT count(*) FROM orders {where}',args)[0]
        page = max(0,min(page,max(0,(total-1)//8)))
        orders = self.store.db.execute(f"SELECT * FROM orders {where} ORDER BY (status='review') DESC,(status='paid') DESC,id DESC LIMIT 8 OFFSET ?",(*args,page*8)).fetchall()
        rows = [[(f"📦 №{o['id']} · {ORDER_STATUS[o['status']]} · {o['quantity']} шт.",f"shop:order:{o['id']}")] for o in orders]
        route = 'orders' if all_orders else 'mine'
        nav = []
        if page:
            nav.append(('⬅️ Попередні замовлення',f'shop:{route}:{page-1}'))
        if (page+1)*8<total:
            nav.append(('Наступні замовлення ➡️',f'shop:{route}:{page+1}'))
        if nav:
            rows.append(nav)
        rows.extend([[('⬅️ До бронювань','shop:admin')]] if all_orders else self.buyer_rows(uid))
        await message.answer(f'📦 {"Замовлення" if all_orders else "Мої замовлення"} · {total}\nСторінка {page+1}',reply_markup=self.app.keyboard(rows,uid))

    def order_text(self,order):
        text = (f"📦 <b>Замовлення №{order['id']}</b>\n{escape(ORDER_STATUS[order['status']])}\n\n"
                f"<b>{escape(order['product_title'])}</b>\n{order['quantity']} шт. × {money(order['price'])}\n"
                f"<b>До оплати: {money(order['quantity']*order['price'])}</b>\n\n"
                f"Отримувач: {escape(order['recipient'])}\nТелефон: {escape(order['phone'])}\n"
                f"Нова пошта: {escape(order['shipping'])}")
        if order['status']=='awaiting_payment':
            text += f"\n\n<b>Реквізити оплати</b>\n<code>{escape(order['payment_details'])}</code>\n\nПісля оплати надішліть сюди скриншот або натисніть «Надіслати оплату».\nБронь до: {self.local_time(order['expires'])}."
        if order['tracking']:
            text += f"\n\n🚚 ТТН: <code>{escape(order['tracking'])}</code>"
        return text

    @staticmethod
    def local_time(value):
        from datetime import datetime,timezone
        from zoneinfo import ZoneInfo
        return datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m.%Y %H:%M (Київ)')

    async def order_card(self,message,order):
        uid = message.from_user.id
        if not order or (order['user_id']!=uid and not self.admin(uid)):
            await message.answer('Замовлення не знайдено.')
            return
        order_id = order['id']
        if order['user_id']==uid and order['status']=='awaiting_payment':
            self.repo.set_state(uid,{'mode':'receipt','order_id':order_id})
        rows = []
        if self.admin(uid):
            if order['user_id']==uid and order['status']=='awaiting_payment':
                rows.append([(f'🧾 Надіслати оплату №{order_id}',f'shop:receipt:{order_id}')])
            if order['status']=='review':
                rows.append([(f'✅ Підтвердити оплату №{order_id}',f'shop:paid:{order_id}'),(f'↩️ Відхилити оплату №{order_id}',f'shop:reject:{order_id}')])
            if order['receipt_id']:
                rows.append([(f'🧾 Скриншот №{order_id}',f'shop:proof:{order_id}')])
            if order['status']=='paid':
                rows.append([(f'🚚 Додати ТТН №{order_id}',f'shop:shipping:{order_id}')])
            if order['status'] in ('awaiting_payment','review'):
                rows.append([(f'❌ Скасувати замовлення №{order_id}',f'shop:cancelask:{order_id}')])
            rows.append([(f'💬 Чат замовлення №{order_id}',f'shop:chat:{order_id}')])
            rows.append([('📦 Замовлення','shop:orders:0'),('⬅️ До бронювань','shop:admin')])
        else:
            if order['status']=='awaiting_payment':
                rows += [[(f'🧾 Надіслати оплату №{order_id}',f'shop:receipt:{order_id}')],[(f'❌ Скасувати замовлення №{order_id}',f'shop:cancelask:{order_id}')]]
            rows += [[(f'💬 Чат замовлення №{order_id}',f'shop:chat:{order_id}')],[('📦 Мої замовлення','shop:mine:0')]]
        await message.answer(self.order_text(order),parse_mode='HTML',reply_markup=self.app.keyboard(rows,uid))

    async def prompt(self,message,state,text):
        uid = message.from_user.id
        self.repo.set_state(uid,state)
        rows = [[('↩️ Скасувати введення','shop:back')]]
        await message.answer(text,reply_markup=self.app.keyboard(rows,uid))

    async def action(self,callback):
        message,uid = callback.message,callback.from_user.id
        if message.chat.type!='private':
            await callback.answer('Відкрийте бота в особистому чаті.',show_alert=True)
            return
        blocked = self.store.one('SELECT blocked FROM users WHERE user_id=?',(uid,))
        if blocked and blocked[0] and not self.app.get_role(uid):
            await callback.answer('⛔ Доступ обмежено.',show_alert=True)
            return
        parts = callback.data.split(':')
        action = parts[1] if len(parts)>1 else ''
        admin_actions = {'admin','enable','new','products','product','restock','price','visibility','orders','card','paid','reject','proof','shipping','link','deleteask','delete'}
        if action in admin_actions and not self.admin(uid):
            await callback.answer('⛔ Недостатньо прав.',show_alert=True)
            return
        await callback.answer()
        # One input mode at a time; checkout confirmation needs the saved draft.
        if action not in ('confirm',):
            self.repo.clear(uid)
        self.store.cancel(uid,keep_booking=True)
        try:
            value = int(parts[2]) if len(parts)>2 and parts[2].lstrip('-').isdigit() else None
            if action=='admin':
                await self.dashboard(message)
            elif action=='enable' and value in (0,1):
                staff_ids={row['user_id'] for row in self.store.db.execute('SELECT user_id FROM users') if self.app.get_role(row['user_id'])}
                try:
                    changed,version,recipients=self.repo.set_enabled(value,staff_ids)
                except ValueError as exc:
                    await message.answer(str(exc),reply_markup=self.app.keyboard(self.setup_rows(),uid))
                    return
                if changed:
                    self.menu_wake.set()
                    await message.answer(('✅ Бронювання відкрито.' if value else '⏸ Нові бронювання вимкнено.')+
                        f'\nСповіщення та оновлення кнопок поставлено в чергу для {recipients} користувачів.')
                else:
                    await message.answer('Бронювання вже '+('увімкнене.' if value else 'вимкнене.'))
                await self.dashboard(message)
            elif action in ('deleteask','delete') and value is not None:
                product=self.repo.product(value)
                if not product:
                    raise ValueError('Товар уже видалено або не знайдено.')
                if action=='deleteask':
                    await message.answer(f"Видалити товар «{product['title']}» (№{value}) з каталогу?\n\nЙого більше не бачитимуть адміністратори й покупці. Чинні замовлення, квитанції та чати залишаться доступними.",
                        reply_markup=self.app.keyboard([[(f'Так, видалити товар №{value}',f'shop:delete:{value}')],[(f'Залишити товар №{value}',f'shop:product:{value}')]],uid))
                else:
                    self.repo.delete_product(value,uid)
                    await message.answer('🗑 Товар видалено з каталогу. Оформлені замовлення збережено.')
                    await self.catalog(message,management=True)
            elif action=='link' and value is not None:
                if not self.repo.product(value):
                    raise ValueError('Шеврон не знайдено.')
                me = await self.app.bot.get_me()
                await message.answer(f'Посилання для покупців — можна додати до публікації з фото:\nhttps://t.me/{me.username}?start=patch_{value}')
            elif action=='card':
                await self.prompt(message,{'mode':'admin_card'},'Надішліть номер картки та ім’я отримувача для оплати (до 500 символів).\nРеквізити зберігаються в замовленні й не зміняться для вже оформлених заявок.')
            elif action=='new':
                await self.prompt(message,{'mode':'admin_title'},'Введіть назву шеврона (до 80 символів).')
            elif action in ('products','catalog'):
                if action=='catalog' and self.store.setting('bookings_enabled','0')!='1':
                    raise ValueError('Нові бронювання зараз вимкнені. Уже оформлені доступні в «Мої замовлення».')
                await self.catalog(message,value or 0,action=='products')
            elif action in ('view','product') and value is not None:
                if action=='view' and self.store.setting('bookings_enabled','0')!='1':
                    raise ValueError('Нові бронювання зараз вимкнені.')
                await self.product_card(message,value,action=='product')
            elif action in ('restock','price') and value is not None:
                if not self.repo.product(value):
                    raise ValueError('Шеврон не знайдено.')
                await self.prompt(message,{'mode':'admin_'+action,'product_id':value},'Введіть кількість, яку додаємо до вільного залишку.' if action=='restock' else 'Введіть нову ціну за одиницю у гривнях. Чинні замовлення збережуть попередню ціну.')
            elif action=='visibility' and value is not None and len(parts)==4 and parts[3] in ('0','1'):
                with self.store.db:
                    self.store.db.execute('UPDATE products SET active=? WHERE id=? AND deleted=0',(int(parts[3]),value))
                await self.product_card(message,value,True)
            elif action in ('orders','mine'):
                await self.list_orders(message,value or 0,action=='orders')
            elif action=='buy' and value is not None:
                product = self.repo.product(value)
                if self.store.setting('bookings_enabled','0')!='1' or not product or not product['active'] or product['stock']<=0:
                    raise ValueError('Цей шеврон зараз недоступний для бронювання.')
                await self.prompt(message,{'mode':'quantity','product_id':value,'price':product['price'],'key':uuid.uuid4().hex},f"Скільки шевронів «{product['title']}» замовляєте?\nДоступно: {product['stock']} шт. · ціна {money(product['price'])}.\n\nДля відправлення збережемо ПІБ, телефон та відділення Нової пошти у вашому замовленні.")
            elif action=='confirm':
                state = self.repo.state(uid)
                if not state or state.get('mode')!='confirm' or len(parts)!=3 or state.get('key')!=parts[2]:
                    prior = self.store.one('SELECT * FROM orders WHERE request_key=? AND user_id=?',(parts[2] if len(parts)>2 else '',uid))
                    if prior:
                        await self.order_card(message,prior)
                        return
                    raise ValueError('Відкрийте каталог і заповніть дані замовлення.')
                order,added = self.repo.reserve(uid,state)
                self.repo.clear(uid)
                await self.order_card(message,order)
                if added:
                    await self.notify_admins(order,f"Нове бронювання: {order['product_title']} · {order['quantity']} шт.\nСума: {money(order['price']*order['quantity'])}")
            elif action in ('order','receipt','proof','paid','reject','shipping','cancelask','cancel','chat') and value is not None:
                order = self.repo.order(value)
                if not order or (order['user_id']!=uid and not self.admin(uid)):
                    raise ValueError('Замовлення не знайдено.')
                if action=='order':
                    await self.order_card(message,order)
                elif action=='receipt':
                    if order['status']!='awaiting_payment' or order['user_id']!=uid:
                        raise ValueError('Завантаження оплати зараз недоступне.')
                    await self.prompt(message,{'mode':'receipt','order_id':value},f'Надішліть фото скриншота або квитанцію PDF для замовлення №{value}.\nОплату підтверджує адміністратор після перевірки надходження коштів.')
                elif action=='proof':
                    if not order['receipt_id']:
                        raise ValueError('Квитанцію ще не надіслано.')
                    if order['receipt_type']=='photo':
                        await message.answer_photo(order['receipt_id'],caption=f'Квитанція · замовлення №{value}')
                    else:
                        await message.answer_document(order['receipt_id'],caption=f'Квитанція · замовлення №{value}')
                elif action in ('paid','reject'):
                    order = self.repo.transition(value,action,uid)
                    text = ('✅ Оплату підтверджено. Готуємо замовлення до відправлення.' if action=='paid' else 'Квитанцію не підтверджено. Перевірте оплату, зв’яжіться з командою або завантажте правильний скриншот.')
                    await self.safe_send(order['user_id'],f'Замовлення №{value}\n{text}',reply_markup=self.app.keyboard([[(f'📦 Відкрити №{value}',f'shop:order:{value}')]],order['user_id']))
                    await self.order_card(message,order)
                elif action=='shipping':
                    if order['status']!='paid':
                        raise ValueError('Спершу підтвердіть оплату.')
                    await self.prompt(message,{'mode':'admin_shipping','order_id':value},f'Введіть 14 цифр номера ТТН для замовлення №{value}.')
                elif action=='cancelask':
                    if order['status'] not in ('awaiting_payment','review') or (not self.admin(uid) and order['status']!='awaiting_payment'):
                        raise ValueError('Скасування цього замовлення погодьте з командою в чаті.')
                    await message.answer(f'Скасувати замовлення №{value}? Зарезервована кількість повернеться в продаж.\nЯкщо вже оплатили — спершу зв’яжіться з командою.',reply_markup=self.app.keyboard([[(f'Так, скасувати №{value}',f'shop:cancel:{value}')],[(f'Залишити замовлення №{value}',f'shop:order:{value}')]],uid))
                elif action=='cancel':
                    order = self.repo.transition(value,'cancelled',uid,user_id=None if self.admin(uid) else uid)
                    if uid!=order['user_id']:
                        await self.safe_send(order['user_id'],f'Замовлення №{value} скасовано. Якщо ви вже оплатили — зв’яжіться з командою.')
                    else:
                        await self.notify_admins(order,'Покупець скасував бронювання.')
                    await self.order_card(message,order)
                elif action=='chat':
                    ticket_id = self.ensure_ticket(order)
                    if self.admin(uid):
                        await self.app.ticket_callback(self.app.KeyboardAction(message,f'ticket:reply:{ticket_id}'))
                    else:
                        await self.prompt(message,{'mode':'chat','order_id':value,'ticket_id':ticket_id},f'💬 Чат замовлення №{value}\nНадішліть повідомлення або вкладення — команда відповість тут.')
            elif action in ('back','home','contact'):
                if self.admin(uid):
                    await self.dashboard(message)
                else:
                    await message.answer('Напишіть повідомлення команді або оберіть дію внизу.',reply_markup=self.buyer_menu(uid))
            else:
                raise ValueError('Невідома дія.')
        except ValueError as exc:
            await message.answer(str(exc),reply_markup=self.buyer_menu(uid) if not self.admin(uid) else self.app.keyboard([[('⬅️ До бронювань','shop:admin')]],uid))

    def ensure_ticket(self,order):
        with self.store.db:
            if order['ticket_id']:
                ticket_id = order['ticket_id']
                self.store.db.execute("UPDATE tickets SET status='new' WHERE id=? AND status='closed'",(ticket_id,))
            else:
                user = self.store.one('SELECT * FROM users WHERE user_id=?',(order['user_id'],))
                ticket_id = self.store.db.execute('INSERT INTO tickets(user_id,name,username) VALUES(?,?,?)',(order['user_id'],user['name'] if user else order['recipient'],user['username'] if user else None)).lastrowid
                self.store.db.execute('UPDATE orders SET ticket_id=? WHERE id=?',(ticket_id,order['id']))
            return ticket_id

    async def input(self,message):
        uid = message.from_user.id
        state = self.repo.state(uid)
        if not state:
            return False
        mode = state['mode']
        if mode.startswith('admin_') and not self.admin(uid):
            self.repo.clear(uid)
            await message.answer('⛔ Недостатньо прав.')
            return True
        if message.text and message.text.startswith('/'):
            return False
        text = (message.text or '').strip()
        try:
            if mode in ('quantity','recipient','phone','shipping','confirm') and self.store.setting('bookings_enabled','0')!='1':
                self.repo.clear(uid)
                raise ValueError('Нові бронювання вимкнені. Ваші чинні замовлення залишаються доступними.')
            if mode=='chat':
                order = self.repo.order(state['order_id'])
                if not order or order['user_id']!=uid:
                    self.repo.clear(uid)
                    raise ValueError('Замовлення не знайдено.')
                if message.content_type not in {'text','photo','video','document','voice','audio','animation','sticker','video_note','contact','location'}:
                    raise ValueError('Надішліть текст, фото або інше вкладення.')
                await self.app.record_ticket_message(message,state['ticket_id'])
                return True
            if mode=='admin_card':
                if not 10<=len(text)<=500 or not re.search(r'\d',text):
                    raise ValueError('Введіть реквізити оплати: номер картки та ім’я отримувача, до 500 символів.')
                self.store.set_setting('booking_card',text)
                self.repo.clear(uid)
                await message.answer('✅ Реквізити збережено для нових замовлень.')
                await self.dashboard(message)
            elif mode=='admin_title':
                if not 2<=len(text)<=80:
                    raise ValueError('Назва має містити від 2 до 80 символів.')
                state.update(mode='admin_photo',title=text)
                await self.prompt(message,state,'Надішліть фото шеврона як фотографію.')
            elif mode=='admin_photo':
                if not message.photo:
                    raise ValueError('Потрібна фотографія шеврона, не документ.')
                state.update(mode='admin_product_price',photo_id=message.photo[-1].file_id)
                await self.prompt(message,state,'Введіть ціну за один шеврон у гривнях, наприклад 250 або 250,50.')
            elif mode=='admin_product_price':
                state.update(mode='admin_stock',price=parse_price(text))
                await self.prompt(message,state,'Скільки шевронів доступно? Введіть ціле число від 0 до 100000.')
            elif mode=='admin_stock':
                if not text.isascii() or not text.isdigit() or not 0<=int(text)<=100000:
                    raise ValueError('Введіть ціле число від 0 до 100000.')
                product_id = self.repo.add_product(state['title'],state['photo_id'],state['price'],int(text))
                self.repo.clear(uid)
                await message.answer('✅ Шеврон додано.')
                await self.product_card(message,product_id,True)
            elif mode in ('admin_restock','admin_price'):
                product = self.repo.product(state['product_id'])
                if not product:
                    raise ValueError('Шеврон не знайдено.')
                if mode=='admin_restock':
                    if not text.isascii() or not text.isdigit() or not 1<=int(text)<=100000:
                        raise ValueError('Введіть число від 1 до 100000.')
                    with self.store.db:
                        self.store.db.execute('UPDATE products SET stock=stock+? WHERE id=?',(int(text),product['id']))
                else:
                    price = parse_price(text)
                    with self.store.db:
                        self.store.db.execute('UPDATE products SET price=? WHERE id=?',(price,product['id']))
                self.repo.clear(uid)
                await self.product_card(message,product['id'],True)
            elif mode=='quantity':
                product = self.repo.product(state['product_id'])
                if not product or not product['active']:
                    raise ValueError('Шеврон більше недоступний.')
                if product['stock']<=0:
                    raise ValueError('Цей шеврон уже розібрали. Оберіть інший у каталозі.')
                if not text.isascii() or not text.isdigit() or not 1<=int(text)<=min(product['stock'],100000):
                    raise ValueError(f"Введіть кількість від 1 до {min(product['stock'],100000)}.")
                state.update(mode='recipient',quantity=int(text))
                await self.prompt(message,state,'Введіть ПІБ отримувача для Нової пошти.')
            elif mode=='recipient':
                if not 3<=len(text)<=120 or len(text.split())<2:
                    raise ValueError('Введіть ім’я та прізвище отримувача, до 120 символів.')
                state.update(mode='phone',recipient=text)
                await self.prompt(message,state,'Введіть телефон отримувача у форматі +380XXXXXXXXX або 0XXXXXXXXX.')
            elif mode=='phone':
                contact = getattr(message,'contact',None)
                state.update(mode='shipping',phone=phone_number(contact.phone_number if contact else text))
                await self.prompt(message,state,'Вкажіть місто та номер відділення або поштомата Нової пошти.\nНаприклад: Суми, відділення №5.')
            elif mode=='shipping':
                if not 5<=len(text)<=300 or not re.search(r'\d',text):
                    raise ValueError('Вкажіть місто та номер відділення/поштомата, до 300 символів.')
                state.update(mode='confirm',shipping=text)
                self.repo.set_state(uid,state)
                product = self.repo.product(state['product_id'])
                await message.answer(
                    f"📦 <b>Перевірте замовлення</b>\n\n{escape(product['title'])}\n{state['quantity']} шт. × {money(state['price'])}\n"
                    f"<b>Разом: {money(state['quantity']*state['price'])}</b>\n\nОтримувач: {escape(state['recipient'])}\n"
                    f"Телефон: {escape(state['phone'])}\nНова пошта: {escape(text)}\n\n"
                    'Після підтвердження з’являться реквізити. Бронь діятиме 24 години. Вартість доставки не включена.',
                    parse_mode='HTML',reply_markup=self.app.keyboard([[('✅ Підтвердити бронювання',f'shop:confirm:{state["key"]}')],[('↩️ Скасувати введення','shop:back')]],uid))
            elif mode=='confirm':
                raise ValueError('Перевірте дані та натисніть «Підтвердити бронювання» або скасуйте введення.')
            elif mode=='receipt':
                if message.photo:
                    file_id,kind = message.photo[-1].file_id,'photo'
                elif message.document and ((message.document.mime_type or '').startswith('image/') or message.document.mime_type=='application/pdf'):
                    if (message.document.file_size or 0)>20_000_000:
                        raise ValueError('Файл має бути до 20 МБ.')
                    file_id,kind = message.document.file_id,'document'
                else:
                    raise ValueError('Надішліть скриншот як фото або квитанцію файлом JPG, PNG чи PDF.')
                self.repo.receipt(state['order_id'],uid,file_id,kind)
                self.repo.clear(uid)
                order = self.repo.order(state['order_id'])
                await message.answer('🧾 Квитанцію збережено й передано на перевірку. Сам скриншот ще не підтверджує оплату.')
                await self.order_card(message,order)
                await self.notify_admins(order,'🧾 Покупець надіслав квитанцію. Перевірте надходження коштів і підтвердіть або відхиліть оплату.')
            elif mode=='admin_shipping':
                tracking = re.sub(r'\s','',text)
                order = self.repo.transition(state['order_id'],'shipped',uid,tracking=tracking)
                self.repo.clear(uid)
                await self.safe_send(order['user_id'],f"🚚 Замовлення №{order['id']} відправлено.\nНова пошта · ТТН: {tracking}")
                await self.order_card(message,order)
            else:
                self.repo.clear(uid)
                raise ValueError('Відкрийте потрібний розділ заново.')
        except ValueError as exc:
            await message.answer(str(exc))
        return True

    async def expiry_worker(self):
        while True:
            try:
                for order in self.repo.expire():
                    await self.safe_send(order['user_id'],f"⌛ Бронювання №{order['id']} завершилося: скриншот оплати не надійшов за 24 години.\nЯкщо ви вже оплатили — напишіть команді в чаті замовлення; не оплачуйте повторно.")
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Не вдалося перевірити строки бронювань')
            await asyncio.sleep(60)

    async def process_menu_update(self):
        row=self.repo.next_menu_update(time.time())
        if not row:
            return False
        uid,version=row['user_id'],row['version']
        user=self.store.one('SELECT blocked FROM users WHERE user_id=?',(uid,))
        if version!=int(self.store.setting('booking_menu_version','0')) or not user or user['blocked'] or self.app.get_role(uid):
            self.repo.finish_menu_update(uid,version,'skipped','Неактуальне оновлення або користувач недоступний.')
            return True
        if row['enabled']:
            text='🛍 Відкрито бронювання шевронів!\n\nНатисніть «Шеврони» в меню внизу, щоб переглянути фото, ціни та оформити замовлення.'
        else:
            text='⏸ Нові бронювання шевронів тимчасово вимкнено.\n\nКнопку «Шеврони» прибрано з меню.'
            if self.has_orders(uid):
                text+=' Ваші чинні замовлення, оплата й чат із командою залишаються доступними через «Мої замовлення».'
        try:
            await self.app.bot.send_message(uid,text,reply_markup=self.buyer_menu(uid))
        except TelegramRetryAfter as exc:
            self.repo.finish_menu_update(uid,version,'pending',exc.message[:300],time.time()+max(1,exc.retry_after))
        except (TelegramForbiddenError,TelegramBadRequest) as exc:
            self.repo.finish_menu_update(uid,version,'failed',exc.message[:300])
        except TelegramAPIError as exc:
            status='failed' if row['attempts']>=2 else 'pending'
            self.repo.finish_menu_update(uid,version,status,exc.message[:300],time.time()+10*(row['attempts']+1))
        else:
            # A newer toggle during delivery remains pending and updates this keyboard again.
            self.repo.finish_menu_update(uid,version,'sent')
        return True

    async def menu_worker(self):
        while True:
            try:
                if await self.process_menu_update():
                    await asyncio.sleep(0.1)
                    continue
                self.menu_wake.clear()
                try:
                    await asyncio.wait_for(self.menu_wake.wait(),timeout=5)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Не вдалося оновити меню бронювання користувачам')
                await asyncio.sleep(5)
