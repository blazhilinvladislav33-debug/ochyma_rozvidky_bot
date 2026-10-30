import asyncio
import logging
import time
import json
from editorial import normalized
from html import escape
from urllib.parse import urlparse
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramNetworkError


class ChannelBridge:
    def __init__(self, app):
        self.app = app
        self.wake = asyncio.Event()
        self.reader_busy = set()

    @property
    def store(self):
        return self.app.store

    def select_target(self, chat):
        """Keep one destination; never silently redirect already approved posts."""
        if self.store.one('SELECT chat_id FROM channel_sources WHERE chat_id=?', (chat.id,)):
            raise ValueError('Цей канал уже є джерелом. Оберіть інший канал для публікацій.')
        old = self.store.setting('publish_target')
        with self.store.db:
            if old and old != str(chat.id):
                self.store.db.execute("UPDATE channel_deliveries SET status='cancelled' WHERE status IN ('pending','failed')")
                self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,due=NULL,error='Канал публікацій змінено. Перевірте канал допису.',revision=revision+1 WHERE status IN ('queued','scheduled')")
            self.store.set_setting('publish_target', str(chat.id))
            self.store.set_setting('publish_target_title', chat.title or 'Мій канал')

    async def membership(self, event):
        if event.chat.type != 'channel':
            return
        member = event.new_chat_member
        writable = member.status == 'administrator' and getattr(member, 'can_post_messages', False)
        current = self.store.setting('publish_target')
        if current == str(event.chat.id):
            if writable:
                self.select_target(event.chat)
            else:
                self.store.set_setting('autopublish', '0')
            return
        if current or not writable or self.app.get_role(event.from_user.id) not in ('owner', 'admin'):
            return
        if self.store.one('SELECT chat_id FROM channel_sources WHERE chat_id=?', (event.chat.id,)):
            return
        self.select_target(event.chat)
        try:
            await self.app.bot.send_message(event.from_user.id,
                f'✅ <b>Канал підключено</b>\n\n📢 {escape(event.chat.title or "Мій канал")}\n\n'
                'Нові дописи автоматично отримуватимуть цей канал. Джерела й автопублікацію налаштуйте в розділі «Канали».', parse_mode='HTML')
        except TelegramAPIError:
            pass

    async def dashboard(self, callback):
        sources = self.store.db.execute('SELECT * FROM channel_sources ORDER BY chat_id').fetchall()
        target = self.store.setting('publish_target')
        title = self.store.setting('publish_target_title', 'Не обрано')
        enabled = self.store.setting('autopublish', '0') == '1'
        pending = self.store.one("SELECT count(*) FROM channel_deliveries WHERE status='pending'")[0]
        failed = self.store.one("SELECT count(*) FROM channel_deliveries WHERE status='failed'")[0]
        last_error = self.store.one("SELECT error FROM channel_deliveries WHERE status='failed' ORDER BY id DESC LIMIT 1")
        error_note = ('\n\n⚠️ Остання помилка: '+escape(last_error[0][:300])) if last_error else ''
        review=self.store.one("SELECT count(DISTINCT p.id) FROM posts p JOIN channel_deliveries d ON d.post_id=p.id WHERE p.status='review'")[0]
        sent = self.store.one("SELECT count(*) FROM channel_deliveries WHERE status='sent'")[0]
        lines = '\n'.join('• '+escape(row['title'])+(' · акаунт' if row['transport']=='reader' else ' · бот') for row in sources) or 'Ще не додано'
        rows = [[('📥 Додати джерело', 'bridge:source'), (('📤 Змінити мій канал' if target else '📤 Підключити мій канал'), 'bridge:target')],
                [('✏️ Підпис зверху', 'bridge:header'), ('✏️ Підпис знизу', 'bridge:footer')],
                [('👁 Приклад публікації', 'bridge:preview')],
                [(('⏸ Призупинити' if enabled else '▶️ Увімкнути автопублікацію'), f'bridge:enable:{0 if enabled else 1}')]]
        rows.append([('🔎 Новини на погодженні','post:list:review:0')])
        rows.append([('👤 Джерело через акаунт', 'bridge:source_reader')])
        rows.append([('🚨 Правила загроз і таймери','alert:home')])
        attribution = self.store.setting('publish_attribution', '1') == '1'
        rows.append([(('🏷 Джерело й час: так' if attribution else '🏷 Джерело й час: ні'), f'bridge:attribution:{0 if attribution else 1}')])
        reader_enabled = self.store.setting('reader_enabled', '1') == '1'
        rows.append([(('⏸ Читання акаунтом' if reader_enabled else '▶️ Читання акаунтом'), f'bridge:reader_enable:{0 if reader_enabled else 1}')])
        reader = getattr(self.app, 'reader', None)
        reader_status = escape(reader.status if reader else 'Не підключено')
        reader_errors = self.store.db.execute('SELECT s.title,r.error FROM reader_offsets r JOIN channel_sources s ON s.chat_id=r.chat_id WHERE s.transport=\'reader\' AND r.error IS NOT NULL').fetchall()
        reader_note = '\n'.join(escape(row['title'])+': '+escape(row['error']) for row in reader_errors)
        for number,source in enumerate(sources,1):
            rows.append([(f"⚙️ {number}. {source['title'][:25]}",f"bridge:sourcecard:{source['chat_id']}")])
            rows.append([(f"🗑 Прибрати {number}. {source['title'][:25]}", f"bridge:remove:{source['chat_id']}")])
        if failed:
            rows.append([('🔁 Повторити невдалі публікації', 'bridge:retry')])
        rows += [[('🔄 Оновити канали', 'menu:channels'), ('🏠 Меню', 'menu:home')]]
        await callback.message.answer(
            f"📡 <b>Канали та автопублікація</b>\n\n<b>Джерела</b>\n{lines}\n\n"
            f"<b>Публікувати в</b>\n{escape(title)}" + (f' · {target}' if target else '') +
            f"\n\nАвтопублікація: {'увімкнена' if enabled else 'вимкнена'}\n"
            f"Опубліковано: {sent} · у черзі: {pending} · на погодженні: {review} · помилок: {failed}\n\n"
            f"<b>Підпис зверху</b>\n{escape(self.store.setting('publish_header') or 'Не задано')}\n\n"
            f"<b>Підпис знизу</b>\n{escape(self.store.setting('publish_footer') or 'Не задано')}\n\n"
            'Для джерел через бота додайте бота до джерела. Для джерел через акаунт — підпишіть підключений акаунт на канал. '
            'У каналі публікації бот має бути адміністратором. Захищені дописи не копіюються.\n\n'
            f'👤 Читання акаунтом: {"увімкнене" if reader_enabled else "вимкнене"}\n{reader_status}' + ('\n'+reader_note if reader_note else '') + error_note,
            parse_mode='HTML', reply_markup=self.app.keyboard(rows, callback.from_user.id))
        await callback.answer()

    async def action(self, callback):
        if callback.data.split(':')[1] in ('sourcecard','mode','topics','places','alarmtrust'):
            await self.app.editorial.source_action(callback)
            return
        admin_id = callback.from_user.id
        if callback.message.chat.type != 'private' or self.app.get_role(admin_id) not in ('owner', 'admin'):
            await callback.answer('⛔ Недостатньо прав.', show_alert=True)
            return
        self.store.cancel(admin_id)
        parts = callback.data.split(':')
        action = parts[1]
        if action in ('source', 'source_reader', 'target', 'header', 'footer'):
            prompts = {
                'source': 'Надішліть @username, посилання t.me або ID каналу-джерела.\nНаприклад: @sumyliketop\nБот повинен мати доступ до цього каналу.',
                'source_reader': 'Надішліть @username, посилання t.me або ID каналу. Спершу підпишіть підключений акаунт на цей канал у Telegram.\nДля входу в акаунт запустіть Connect-Reader.cmd на хості. Старі дописи до моменту додавання джерела не завантажуються.',
                'target': 'Надішліть @username, посилання t.me або ID вашого каналу для публікації.\nСпершу додайте бота адміністратором із правом публікувати.',
                'header': 'Надішліть текст підпису зверху (до 300 символів).\nОдин символ «-» — прибрати підпис.',
                'footer': 'Надішліть текст підпису знизу (до 300 символів).\nОдин символ «-» — прибрати підпис.',
            }
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)', (admin_id, 'bridge_'+action))
            await callback.message.answer(prompts[action], reply_markup=self.app.keyboard([[('⬅️ Канали', 'menu:channels'), ('🏠 Меню', 'menu:home')]], admin_id))
            await callback.answer()
            return
        if action == 'attribution' and len(parts) == 3 and parts[2] in ('0', '1'):
            self.store.set_setting('publish_attribution', parts[2])
        elif action == 'reader_enable' and len(parts) == 3 and parts[2] in ('0', '1'):
            self.store.set_setting('reader_enabled', parts[2])
            self.app.reader.wake.set()
            if parts[2] == '0':
                with self.store.db:
                    self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE auto_source=1 AND status IN ('queued','scheduled') AND id IN (SELECT d.post_id FROM channel_deliveries d JOIN channel_sources s ON s.chat_id=d.source_id WHERE s.transport='reader')")
        elif action == 'enable' and len(parts) == 3 and parts[2] in ('0', '1'):
            if parts[2] == '1' and (not self.store.setting('publish_target') or not self.store.one('SELECT chat_id FROM channel_sources LIMIT 1')):
                await callback.answer('Спершу додайте джерело та канал для публікації.', show_alert=True)
                return
            self.store.set_setting('autopublish', parts[2])
            if parts[2]=='0':
                with self.store.db:self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE auto_source=1 AND status IN ('queued','scheduled')")
            self.wake.set()
        elif action == 'remove' and len(parts) == 3:
            try:
                chat_id = int(parts[2])
            except ValueError:
                return
            with self.store.db:
                self.store.db.execute('DELETE FROM channel_sources WHERE chat_id=?', (chat_id,))
                self.store.db.execute('DELETE FROM reader_offsets WHERE chat_id=?', (chat_id,))
                self.store.db.execute('DELETE FROM source_health WHERE chat_id=?', (chat_id,))
                self.store.db.execute('DELETE FROM source_alerts WHERE chat_id=?', (chat_id,))
                self.store.db.execute("UPDATE channel_deliveries SET status='cancelled' WHERE source_id=? AND status IN ('pending','failed')", (chat_id,))
        elif action == 'retry':
            with self.store.db:
                self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE status='failed' AND id IN (SELECT post_id FROM channel_deliveries WHERE status='failed')")
                self.store.db.execute("UPDATE channel_deliveries SET status='review' WHERE status='failed' AND post_id IS NOT NULL")
                self.store.db.execute("UPDATE channel_deliveries SET status='pending',attempts=0 WHERE status='failed' AND post_id IS NULL")
            self.wake.set()
        elif action == 'preview':
            await callback.message.answer(self.render('Приклад повідомлення з джерела.')[0], parse_mode='HTML')
        else:
            await callback.answer('Невідома дія.')
            return
        await self.dashboard(callback)

    @staticmethod
    def channel_reference(text):
        text = text.strip()
        if text.lstrip('-').isdigit():
            value = int(text)
            if value >= 0:
                raise ValueError('Потрібен ID каналу, який починається з -100.')
            return value
        if text.startswith(('https://', 'http://', 't.me/')):
            url = urlparse(text if '://' in text else 'https://'+text)
            if url.hostname not in ('t.me', 'telegram.me'):
                raise ValueError('Надішліть посилання на Telegram-канал.')
            text = url.path.strip('/')
        text = text.lstrip('@')
        if not text or '/' in text or not text.replace('_', '').isascii() or not text.replace('_', '').isalnum():
            raise ValueError('Для приватного каналу використайте його числовий ID. Для публічного — @username.')
        return '@'+text

    async def input(self, message, mode):
        action = mode.removeprefix('bridge_')
        text = (message.text or '').strip()
        if not text:
            await message.answer('Надішліть текст. /cancel — скасувати.')
            return True
        if action in ('header', 'footer'):
            if len(text)>300:
                await message.answer('Підпис має бути до 300 символів.')
                return True
            self.store.set_setting('publish_'+action, '' if text == '-' else text)
        elif action == 'source_reader':
            try:
                reference = self.channel_reference(text)
                chat_id, title, cursor = await self.app.reader.resolve(reference)
                self.app.reader.add_source(chat_id, title, cursor, reference.lstrip('@') if isinstance(reference,str) else None)
            except ValueError as exc:
                await message.answer(str(exc))
                return True
            except Exception as exc:
                logging.warning('Reader source lookup: %s', type(exc).__name__)
                await message.answer('Не вдалося підключити канал через акаунт. Перевірте посилання, з’єднання та підписку акаунта на канал. Якщо Telegram просить зачекати — спробуйте пізніше.')
                return True
        elif action in ('source', 'target'):
            try:
                reference = self.channel_reference(text)
                chat = await self.app.bot.get_chat(reference)
                if chat.type != 'channel':
                    raise ValueError('Потрібен саме канал Telegram.')
                me = await self.app.bot.get_me()
                member = await self.app.bot.get_chat_member(chat.id, me.id)
                if member.status not in ('member', 'administrator', 'creator'):
                    raise ValueError('Додайте бота до каналу, щоб він отримував дописи.')
                if action == 'target' and (member.status != 'administrator' or not member.can_post_messages):
                    raise ValueError('Надайте боту права адміністратора каналу та право публікувати повідомлення.')
                if action == 'source' and str(chat.id) == self.store.setting('publish_target'):
                    raise ValueError('Джерело та канал публікації мають бути різними.')
                if action == 'target' and self.store.one('SELECT chat_id FROM channel_sources WHERE chat_id=?', (chat.id,)):
                    raise ValueError('Цей канал уже є джерелом. Оберіть інший, щоб уникнути повторних публікацій.')
                if action == 'source':
                    if self.store.one("SELECT chat_id FROM channel_sources WHERE chat_id=? AND transport='reader'", (chat.id,)):
                        raise ValueError('Цей канал уже підключений через акаунт. Спершу видаліть його, щоб змінити спосіб читання.')
                    if getattr(chat, 'has_protected_content', False):
                        raise ValueError('Джерело забороняє копіювання дописів.')
                    with self.store.db:
                        self.store.db.execute('INSERT INTO channel_sources(chat_id,title,mode) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title', (chat.id, chat.title, 'review'))
                        self.store.db.execute('UPDATE channel_sources SET username=? WHERE chat_id=?', (getattr(chat,'username',None),chat.id))
                else:
                    self.select_target(chat)
            except ValueError as exc:
                await message.answer(str(exc))
                return True
            except TelegramNetworkError:
                await message.answer('Немає зв’язку з Telegram. Налаштування не змінено — спробуйте пізніше.')
                return True
            except TelegramAPIError as exc:
                logging.warning('Telegram не підтвердив доступ до каналу: %s', exc.message)
                await message.answer('Telegram не підтвердив доступ до каналу.\n'
                    'Перевірте @username. Власник або адміністратор джерела має додати бота до каналу. '
                    'У вашому каналі боту також потрібне право публікації.\n'
                    'Якщо джерело чуже і ви не можете додати туди бота, цей спосіб підключення не працюватиме.\n'
                    'Відповідь Telegram: '+exc.message[:200])
                return True
        self.store.cancel(message.from_user.id)
        await message.answer('✅ Збережено. Автопублікацію вмикайте після перевірки джерел і підписів.')
        await self.dashboard(self.app.KeyboardAction(message, ''))
        return True

    def render(self, body):
        header = escape(self.store.setting('publish_header'))
        footer = escape(self.store.setting('publish_footer'))
        full = '\n\n'.join(part for part in (header, body, footer) if part)
        return full, header, footer

    async def receive(self, message):
        if self.store.setting('autopublish', '0') != '1':
            return
        source = self.store.one('SELECT * FROM channel_sources WHERE chat_id=?', (message.chat.id,))
        target = self.store.setting('publish_target')
        if not source or source['transport'] != 'bot' or not target or str(message.chat.id) == target:
            return
        # Do not republish old backlog as current alerts when a source is first connected.
        if message.date.strftime('%Y-%m-%d %H:%M:%S') < source['added']:
            return
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO channel_deliveries(source_id,message_id,target_id,payload,group_id,queued) VALUES(?,?,?,?,?,?)',
                                  (message.chat.id, message.message_id, int(target), message.model_dump_json(), message.media_group_id, time.time()))
            self.store.db.execute('''INSERT INTO source_health(chat_id,last_message) VALUES(?,?)
                ON CONFLICT(chat_id) DO UPDATE SET last_message=excluded.last_message''', (message.chat.id,message.date.strftime('%Y-%m-%d %H:%M:%S')))
        self.wake.set()

    async def deliver(self, row):
        if self.store.setting('publishing_paused','0')=='1':
            raise ValueError('Усі публікації призупинено.')
        from aiogram.types import Message
        message = Message.model_validate_json(row['payload'])
        if message.has_protected_content:
            raise ValueError('Джерело забороняє копіювання.')
        body = message.html_text
        full, header, footer = self.render(body)
        if not header and not footer:
            await self.app.bot.forward_message(row['target_id'], row['source_id'], row['message_id'])
        elif message.text:
            if len(full)>3900:
                raise ValueError('Текст разом із підписами надто довгий. Скоротіть підписи та повторіть публікацію.')
            await self.app.bot.send_message(row['target_id'], full, parse_mode='HTML')
        elif message.photo or message.video or message.animation or message.document or message.audio or message.voice:
            if len(full)>1000:
                raise ValueError('Опис медіа з підписами надто довгий. Скоротіть підписи та повторіть публікацію.')
            await self.app.bot.copy_message(row['target_id'], row['source_id'], row['message_id'], caption=full, parse_mode='HTML')
        else:
            raise ValueError('Підписи підтримуються для тексту, фото, відео, документів, аудіо та голосових.')

    async def deliver_album(self, rows):
        if self.store.setting('publishing_paused','0')=='1':
            raise ValueError('Усі публікації призупинено.')
        from aiogram.types import Message, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
        messages = [Message.model_validate_json(row['payload']) for row in rows]
        if any(message.has_protected_content for message in messages):
            raise ValueError('Джерело забороняє копіювання альбому.')
        if not self.store.setting('publish_header') and not self.store.setting('publish_footer'):
            await self.app.bot.forward_messages(rows[0]['target_id'], rows[0]['source_id'], message_ids=[row['message_id'] for row in rows])
            return
        media = []
        for index, message in enumerate(messages):
            caption = self.render(message.html_text)[0] if index == 0 else message.html_text
            if len(caption)>1000:
                raise ValueError('Опис альбому з підписами надто довгий. Скоротіть підписи.')
            options = dict(caption=caption or None, parse_mode='HTML')
            if message.photo:
                item = InputMediaPhoto(media=message.photo[-1].file_id, **options)
            elif message.video:
                item = InputMediaVideo(media=message.video.file_id, **options)
            elif message.audio:
                item = InputMediaAudio(media=message.audio.file_id, **options)
            elif message.document:
                item = InputMediaDocument(media=message.document.file_id, **options)
            else:
                raise ValueError('Непідтримуваний тип вкладення в альбомі.')
            media.append(item)
        await self.app.bot.send_media_group(rows[0]['target_id'], media=media)

    def prepare(self, rows):
        from aiogram.types import Message
        source=self.store.one('SELECT * FROM channel_sources WHERE chat_id=?',(rows[0]['source_id'],))
        if not source:
            with self.store.db:self.store.db.executemany("UPDATE channel_deliveries SET status='cancelled' WHERE id=?",[(row['id'],) for row in rows])
            return None,None
        messages=[]
        for row in rows:
            payload=json.loads(row['payload'])
            if payload.get('reader'):
                if payload.get('blocked'):
                    with self.store.db:self.store.db.executemany("UPDATE channel_deliveries SET status='failed',error=? WHERE id=?",[(payload['blocked'],item['id']) for item in rows])
                    return None,None
                data={'message_id':payload['id'],'date':payload['date'],'chat':{'id':payload['source'],'type':'channel'}}
                item=payload.get('media')
                if item:
                    kind=item['kind'];value={'file_id':item['file_id'],'file_unique_id':item.get('fingerprint',item['file_id'])}
                    if kind=='photo':value.update(width=1,height=1);data['photo']=[value]
                    else:
                        if kind in ('video','animation'):value.update(width=1,height=1,duration=0)
                        elif kind in ('audio','voice'):value['duration']=0
                        data[kind]=value
                    data['caption']=payload['text'] or None
                else:data['text']=payload['text']
                messages.append(Message.model_validate(data))
            else:messages.append(Message.model_validate_json(row['payload']))
        if any(message.has_protected_content for message in messages):
            with self.store.db:self.store.db.executemany("UPDATE channel_deliveries SET status='failed',error='Джерело забороняє копіювання.' WHERE id=?",[(row['id'],) for row in rows])
            return None,None
        text='\n\n'.join(message.text or message.caption or '' for message in messages).strip()
        event=None
        if self.store.alerts.enabled:
            first=messages[0]
            event_key=f"source:{source['chat_id']}:{first.media_group_id or first.message_id}:{rows[0]['target_id']}"
            event=self.store.alerts.analyse(text,source,rows[0]['target_id'],first.date,event_key)
        if not self.store.editorial.matches(source,text):
            with self.store.db:self.store.db.executemany("UPDATE channel_deliveries SET status='filtered' WHERE id=?",[(row['id'],) for row in rows])
            return None,None
        authors=[uid for uid in self.app.ADMIN_IDS if self.app.get_role(uid) in ('owner','admin')]
        if not authors:
            with self.store.db:self.store.db.executemany("UPDATE channel_deliveries SET status='failed',error='Немає адміністратора для погодження публікації.' WHERE id=?",[(row['id'],) for row in rows])
            return None,None
        author=self.app.OWNER_ID if self.app.OWNER_ID in authors else min(authors)
        media=[];fingerprints=[]
        for index,message in enumerate(messages):
            if message.text:continue
            item=None
            for kind in ('photo','video','animation','document','audio','voice'):
                value=getattr(message,kind,None)
                if value:
                    value=value[-1] if kind=='photo' else value
                    item={'kind':kind,'file_id':value.file_id};fingerprints.append(value.file_unique_id)
                    break
            if not item:item={'kind':'copy','source_id':message.chat.id,'message_id':message.message_id}
            if index and message.caption:item.update(caption_text=message.caption,caption_html=message.html_text)
            media.append(item)
        first=messages[0]
        body_text=first.text or first.caption or ''
        body_html=first.html_text
        signature=text or 'media '+' '.join(fingerprints) if fingerprints or text else ''
        duplicate=self.store.editorial.duplicate(rows[0]['target_id'],signature)
        key=f"source:{source['chat_id']}:{first.media_group_id or first.message_id}:{rows[0]['target_id']}"
        old=self.store.one('SELECT * FROM posts WHERE origin_key=?',(key,))
        if old:
            old_origin=json.loads(old['origin'])
            if set(row['message_id'] for row in rows)<=set(old_origin.get('message_ids',[])):
                with self.store.db:self.store.db.executemany('UPDATE channel_deliveries SET status=?,post_id=?,duplicate_of=? WHERE id=?', [('sent' if old['status']=='published' else old['status'],old['id'],old_origin.get('duplicate_of'),row['id']) for row in rows])
                return old['id'],None
            # A late album attachment is retained for review, never silently lost.
            key+=f":late:{first.message_id}"
            old=self.store.one('SELECT * FROM posts WHERE origin_key=?',(key,))
            duplicate=duplicate or old_origin.get('duplicate_of') or self.store.one('SELECT id FROM posts WHERE origin_key=?',(key.split(':late:')[0],))[0]
        post_id=old['id'] if old else None
        status='review' if source['mode']=='review' or duplicate else 'queued'
        origin={'kind':'source','source_id':source['chat_id'],'source_title':source['title'],'source_date':first.date.isoformat(),'filter_text':text,'duplicate_of':duplicate,'message_ids':[row['message_id'] for row in rows]}
        if self.store.setting('publish_attribution','1')=='1' and not any(item['kind']=='copy' for item in media):
            stamp=first.date.astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m.%Y %H:%M')+' · Київ'
            origin['attribution_text']=f'Джерело: {source["title"]}\nЧас допису: {stamp}'
            title=escape(source['title'])
            username=source['username']
            if username and username.replace('_','').isalnum() and username.isascii():
                title=f'<a href="https://t.me/{username}/{first.message_id}">{title}</a>'
            origin['attribution_html']=f'Джерело: {title}\nЧас допису: {stamp}'
        if self.store.alerts.enabled:
            origin['alert']=event
            if event['reason']:
                status='review'
            elif event['urgent']:
                if any(item['kind']=='copy' for item in media):
                    event['reason']='Тип вкладення не дозволяє змінити стиль. Перевірте допис.'
                    event['urgent']=False;status='review'
                else:body_text,body_html=self.store.alerts.render(event)
        with self.store.db:
            if not post_id:
                post_id=self.store.db.execute('''INSERT INTO posts(author_id,target_id,target_title,header,footer,body_text,body_html,media,origin,origin_key,status,approved_by,auto_source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (author,rows[0]['target_id'],self.store.setting('publish_target_title'),self.store.setting('publish_header'),self.store.setting('publish_footer'),body_text,body_html,json.dumps(media),json.dumps(origin,ensure_ascii=False),key,status,author if status=='queued' else None,int(status=='queued'))).lastrowid
                if normalized(signature):self.store.db.execute('INSERT OR IGNORE INTO news_fingerprints(post_id,target_id,normalized) VALUES(?,?,?)',(post_id,rows[0]['target_id'],normalized(signature)))
            self.store.db.executemany('UPDATE channel_deliveries SET status=?,post_id=?,duplicate_of=? WHERE id=?', [('duplicate' if duplicate else status,post_id,duplicate,row['id']) for row in rows])
        notice=(f'♻️ Можливий повтор допису №{duplicate}. Новину з «{source["title"]}» збережено окремо на погодження.' if duplicate else f'🔎 Новина з «{source["title"]}» очікує погодження.') if status=='review' and not old else None
        return post_id,notice

    async def worker(self):
        while True:
            try:
                if self.store.setting('autopublish', '0') == '1':
                    row = self.store.one("SELECT * FROM channel_deliveries WHERE status='pending' ORDER BY id LIMIT 1")
                    if row:
                        if row['source_id'] in self.reader_busy:
                            await asyncio.sleep(0.2)
                            continue
                        rows = [row]
                        if row['group_id']:
                            rows = self.store.db.execute("SELECT * FROM channel_deliveries WHERE source_id=? AND target_id=? AND group_id=? AND status='pending' ORDER BY message_id", (row['source_id'],row['target_id'],row['group_id'])).fetchall()
                            # Telegram delivers each album attachment as an individual update.
                            remaining = 1.5 - (time.time() - max(item['queued'] for item in rows))
                            if remaining > 0:
                                await asyncio.sleep(min(remaining, 1.5))
                                continue
                        post_id,notice = self.prepare(rows)
                        if post_id:
                            self.app.publications.wake.set()
                            if notice:
                                for admin_id in self.app.ADMIN_IDS:
                                    await self.app.publications.notice(admin_id,notice,post_id)
                        continue
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Помилка обробки черги публікацій')
                await asyncio.sleep(5)
