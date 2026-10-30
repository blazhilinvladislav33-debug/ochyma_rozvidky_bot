import json
from html import escape
from aiogram.exceptions import TelegramAPIError


NEWS_STATUS={'draft':'Не надіслано','submitted':'На перевірці','published':'Опубліковано','rejected':'Відхилено'}


class NewsIntake:
    def __init__(self,app):self.app=app

    @property
    def store(self):return self.app.store

    def admin(self,uid):return self.app.get_role(uid) in ('owner','admin')

    def row(self,news_id):return self.store.one('SELECT * FROM reader_news WHERE id=?',(news_id,))

    def state(self,uid):return self.store.one('SELECT * FROM news_states WHERE user_id=?',(uid,))

    def set_step(self,uid,news_id,step):
        with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO news_states VALUES(?,?,?)',(uid,news_id,step))

    def menu(self,uid):
        return self.app.main_menu(self.app.get_role(uid),uid) if self.app.get_role(uid) else self.app.shop.buyer_menu(uid)

    async def prompt(self,message,news_id,step):
        self.set_step(message.from_user.id,news_id,step)
        prompts={
            'content':'📨 Запропонуйте новину\n\nНадішліть текст або фото/відео з підписом. Можна додати альбом до 10 вкладень. Потім натисніть «Далі».\nНовина потрапить на перевірку команді й не публікується автоматично.',
            'place':'📍 Де сталася подія? Вкажіть населений пункт і місце, до 180 символів.',
            'time':'🕒 Коли сталася подія? Вкажіть дату та час за Києвом або напишіть, коли саме це було (до 100 символів).',
        }
        rows=[[('↩️ Скасувати пропозицію','news:cancel')]]
        if step=='content':rows.insert(0,[('✅ Далі — місце події','news:next')])
        await message.answer(prompts[step],reply_markup=self.app.keyboard(rows,message.from_user.id))

    async def summary(self,message,row):
        text=row['body_text'];text=text[:700]+('…' if len(text)>700 else '')
        await message.answer(f"📨 <b>Пропозиція №{row['id']}</b>\n\n{escape(text or 'Медіа без тексту')}\n\n"
            f"Медіа: {len(json.loads(row['media']))}\nМісце: {escape(row['place'])}\nЧас зі слів автора: {escape(row['event_time'])}\n\n"
            'Надіслати команді на перевірку?',parse_mode='HTML',reply_markup=self.app.keyboard([[('📨 Надіслати новину','news:submit')],[('↩️ Скасувати пропозицію','news:cancel')]],message.from_user.id))

    async def action(self,callback):
        uid=callback.from_user.id;message=self.app.publications.actor_message(callback)
        if message.chat.type!='private':await callback.answer('Відкрийте бота в особистому чаті.');return
        blocked=self.store.one('SELECT blocked FROM users WHERE user_id=?',(uid,))
        if blocked and blocked[0] and not self.app.get_role(uid):await callback.answer('⛔ Доступ обмежено.');return
        parts=callback.data.split(':');action=parts[1] if len(parts)>1 else ''
        await callback.answer()
        try:
            if action=='new':
                self.store.cancel(uid)
                pending=self.store.one("SELECT count(*) FROM reader_news WHERE user_id=? AND status='submitted'",(uid,))[0]
                if pending>=3:raise ValueError('На перевірці вже є три ваші новини. Дочекайтеся відповіді команди.')
                with self.store.db:
                    news_id=self.store.db.execute('INSERT INTO reader_news(user_id) VALUES(?)',(uid,)).lastrowid
                await self.prompt(message,news_id,'content')
            elif action=='cancel':
                self.store.cancel(uid)
                await message.answer('Пропозицію не надіслано. Можна написати команді або обрати іншу дію.',reply_markup=self.menu(uid))
            elif action in ('next','submit'):
                state=self.state(uid)
                row=self.row(state['news_id']) if state else None
                if not row or row['user_id']!=uid or row['status']!='draft':raise ValueError('Почніть нову пропозицію через «Запропонувати новину».')
                if action=='next':
                    if not row['body_text'] and not json.loads(row['media']):raise ValueError('Спершу надішліть текст або медіа.')
                    await self.prompt(message,row['id'],'place')
                else:
                    if state['step']!='confirm' or not row['place'] or not row['event_time']:raise ValueError('Спершу заповніть місце та час події.')
                    post_id=self.submit(row)
                    self.store.cancel(uid)
                    await message.answer(f"✅ Новину №{row['id']} передано на перевірку. Дякуємо!",reply_markup=self.menu(uid))
                    for admin_id in self.app.ADMIN_IDS:
                        await self.app.publications.notice(admin_id,f"📨 Нова пропозиція читача №{row['id']}. Перевірте зміст, місце й час перед публікацією.",post_id)
            elif action in ('queue','open','reject'):
                if not self.admin(uid):raise ValueError('⛔ Недостатньо прав.')
                self.store.cancel(uid)
                if len(parts)<3 or not parts[2].isdigit():raise ValueError('Некоректна дія.')
                value=int(parts[2])
                if action=='queue':
                    total=self.store.one("SELECT count(*) FROM reader_news WHERE status='submitted'")[0]
                    page=max(0,min(value,max(0,(total-1)//8)))
                    rows=self.store.db.execute("SELECT * FROM reader_news WHERE status='submitted' ORDER BY id DESC LIMIT 8 OFFSET ?",(page*8,)).fetchall()
                    keyboard=[[(f"📨 Пропозиція №{row['id']} · {row['place'][:25]}",f"news:open:{row['id']}")] for row in rows]
                    nav=[]
                    if page:nav.append(('⬅️ Попередні пропозиції',f'news:queue:{page-1}'))
                    if (page+1)*8<total:nav.append(('Наступні пропозиції ➡️',f'news:queue:{page+1}'))
                    if nav:keyboard.append(nav)
                    keyboard.append([('⬅️ Публікації','menu:posts')])
                    await message.answer(f'📨 Новини від читачів на перевірці: {total}\nСторінка {page+1}',reply_markup=self.app.keyboard(keyboard,uid))
                else:
                    row=self.row(value)
                    if not row:raise ValueError('Пропозицію не знайдено.')
                    if action=='reject':
                        post=self.store.posts.get(row['post_id'])
                        if post and post['status'] not in ('draft','review','failed','archived'):raise ValueError('Допис уже в черзі або опублікований. Спершу перевірте його стан у «Публікаціях».')
                        with self.store.db:
                            changed=self.store.db.execute("UPDATE reader_news SET status='rejected' WHERE id=? AND status='submitted'",(value,)).rowcount
                            if changed and post:self.store.db.execute("UPDATE posts SET status='archived',revision=revision+1 WHERE id=?",(post['id'],))
                        if changed:
                            try:await self.app.bot.send_message(row['user_id'],f'Пропозицію новини №{value} розглянуто. Команда вирішила її не публікувати. Дякуємо за повідомлення.')
                            except TelegramAPIError:pass
                        await message.answer('Пропозицію відхилено.',reply_markup=self.app.keyboard([[('📨 Новини від читачів','news:queue:0')]],uid))
                    else:
                        user=self.store.one('SELECT name FROM users WHERE user_id=?',(row['user_id'],))
                        await message.answer(f"📨 <b>Пропозиція №{value}</b>\nВід: {escape(user[0] if user else str(row['user_id']))}\n"
                            f"Місце: {escape(row['place'])}\nЧас зі слів автора: {escape(row['event_time'])}\nСтатус: {NEWS_STATUS[row['status']]}\n\n{escape(row['body_text'][:1500])}",
                            parse_mode='HTML',reply_markup=self.app.keyboard([[(f'📝 Відкрити чернетку №{row["post_id"]}',f'post:open:{row["post_id"]}')],[(f'❌ Відхилити пропозицію №{value}',f'news:reject:{value}')],[('📨 Новини від читачів','news:queue:0')]],uid))
            else:raise ValueError('Невідома дія.')
        except ValueError as exc:await message.answer(str(exc))

    def submit(self,row):
        authors=[uid for uid in self.app.ADMIN_IDS if self.admin(uid)]
        if not authors:raise ValueError('Команда ще не налаштувала адміністратора. Пропозицію збережено; спробуйте пізніше.')
        author=self.app.OWNER_ID if self.app.OWNER_ID in authors else min(authors)
        target=self.store.setting('publish_target')
        target=int(target) if target else None
        duplicate=self.store.editorial.duplicate(target,row['body_text']) if target else None
        text=f"Нам пишуть:\n\n{row['body_text']}"
        html=f"<b>Нам пишуть:</b>\n\n{row['body_html']}"
        origin={'kind':'reader','suggestion_id':row['id'],'duplicate_of':duplicate}
        with self.store.db:
            current=self.row(row['id'])
            if current['post_id']:return current['post_id']
            post_id=self.store.db.execute('INSERT INTO posts(author_id,target_id,target_title,body_text,body_html,media,origin,status) VALUES(?,?,?,?,?,?,?,?)',
                (author,target,self.store.setting('publish_target_title'),text,html,row['media'],json.dumps(origin), 'review')).lastrowid
            self.store.db.execute("UPDATE reader_news SET status='submitted',post_id=?,submitted=CURRENT_TIMESTAMP WHERE id=? AND status='draft'",(post_id,row['id']))
        return post_id

    async def public_buttons(self,target_id):
        if not target_id:raise ValueError('Спершу підключіть канал для публікації.')
        channel=await self.app.bot.get_chat(target_id)
        username=getattr(channel,'username',None)
        link='https://t.me/'+username if isinstance(username,str) and username else getattr(channel,'invite_link',None)
        if not isinstance(link,str) or not link.startswith('https://t.me/'):
            raise ValueError('Для кнопки «Канал» потрібне публічне посилання або наявне запрошення каналу. Налаштуйте посилання в Telegram.')
        me=await self.app.bot.get_me()
        if not isinstance(getattr(me,'username',None),str) or not me.username:
            raise ValueError('Не вдалося отримати посилання на бота. Спробуйте ще раз.')
        return [{'text':'Канал','url':link},{'text':'Бот','url':'https://t.me/'+me.username}]

    async def input(self,message):
        uid=message.from_user.id;state=self.state(uid)
        if not state:return False
        if message.text and message.text.startswith('/'):return False
        row=self.row(state['news_id'])
        if not row or row['user_id']!=uid or row['status']!='draft':
            self.store.cancel(uid);return False
        try:
            step=state['step'];text=(message.text or '').strip()
            if step=='content':
                media=json.loads(row['media'])
                if message.text is not None:
                    if not text or len(text)>3500:raise ValueError('Введіть текст від 1 до 3500 символів.')
                    with self.store.db:self.store.db.execute('UPDATE reader_news SET body_text=?,body_html=? WHERE id=?',(message.text,message.html_text,row['id']))
                    await self.prompt(message,row['id'],'place')
                else:
                    if message.photo:item={'kind':'photo','file_id':message.photo[-1].file_id}
                    elif message.video:item={'kind':'video','file_id':message.video.file_id}
                    else:raise ValueError('Надішліть текст, фото або відео.')
                    origin=f'{message.chat.id}:{message.message_id}'
                    if any(m.get('origin')==origin for m in media):return True
                    if len(media)>=10:raise ValueError('До пропозиції можна додати 10 вкладень.')
                    item['origin']=origin
                    body,html=row['body_text'],row['body_html']
                    if message.caption:
                        if not media:body,html=message.caption,message.html_text
                        else:item.update(caption_text=message.caption,caption_html=message.html_text)
                    media.append(item)
                    with self.store.db:self.store.db.execute('UPDATE reader_news SET body_text=?,body_html=?,media=? WHERE id=?',(body,html,json.dumps(media),row['id']))
                    if not message.media_group_id:await message.answer('Медіа додано. Можна додати ще або натиснути «Далі».')
            elif step=='place':
                if not 3<=len(text)<=180:raise ValueError('Вкажіть місце події від 3 до 180 символів.')
                with self.store.db:self.store.db.execute('UPDATE reader_news SET place=? WHERE id=?',(text,row['id']))
                await self.prompt(message,row['id'],'time')
            elif step=='time':
                if not 3<=len(text)<=100:raise ValueError('Вкажіть час події від 3 до 100 символів.')
                with self.store.db:self.store.db.execute('UPDATE reader_news SET event_time=? WHERE id=?',(text,row['id']))
                self.set_step(uid,row['id'],'confirm');await self.summary(message,self.row(row['id']))
            else:raise ValueError('Натисніть «Надіслати новину» або скасуйте пропозицію.')
        except ValueError as exc:await message.answer(str(exc))
        return True

    async def published(self,post):
        origin=json.loads(post['origin']);news_id=origin.get('suggestion_id')
        row=self.row(news_id)
        if not row:return
        with self.store.db:changed=self.store.db.execute("UPDATE reader_news SET status='published' WHERE id=? AND status!='published'",(news_id,)).rowcount
        if changed:
            try:await self.app.bot.send_message(row['user_id'],f'✅ Вашу новину №{news_id} перевірено й опубліковано. Дякуємо за внесок!')
            except TelegramAPIError:pass

    async def rejected(self,post):
        origin=json.loads(post['origin']);news_id=origin.get('suggestion_id')
        row=self.row(news_id)
        if not row:return
        with self.store.db:changed=self.store.db.execute("UPDATE reader_news SET status='rejected' WHERE id=? AND status='submitted'",(news_id,)).rowcount
        if changed:
            try:await self.app.bot.send_message(row['user_id'],f'Пропозицію новини №{news_id} розглянуто. Команда вирішила її не публікувати. Дякуємо за повідомлення.')
            except TelegramAPIError:pass
