import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument, ReplyParameters
from channel_bridge import ChannelBridge
from posts_store import POST_STATUS, EDITABLE, utc_string

TEMPLATES = {
    'alarm': ('🚨 Повітряна тривога', 'Укажіть територію, час оголошення й підтверджене джерело інформації.'),
    'clear': ('🟢 Відбій повітряної тривоги', 'Укажіть територію, час відбою й підтверджене джерело інформації.'),
    'power': ('💡 Інформація про відключення', 'Укажіть населений пункт, адреси, період відключення та джерело інформації.'),
    'notice': ('📢 Оголошення', 'Укажіть суть оголошення, місце, дату й час та контакти за потреби.'),
}


def units(text):
    return len(text.encode('utf-16-le'))//2


def scheduled_time(text, now=None):
    try:
        naive=datetime.strptime(text.strip(),'%d.%m.%Y %H:%M')
    except ValueError:
        raise ValueError('Введіть дату й час: ДД.ММ.РРРР ГГ:ХХ, наприклад 28.10.2026 18:30.') from None
    zone=ZoneInfo('Europe/Kyiv')
    value=naive.replace(tzinfo=zone)
    roundtrip=value.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if roundtrip!=naive:
        raise ValueError('Такого місцевого часу немає через переведення годинника. Оберіть інший час.')
    if value.utcoffset()!=naive.replace(tzinfo=zone,fold=1).utcoffset():
        raise ValueError('Цей час повторюється через переведення годинника. Оберіть час поза переходом.')
    now=now or datetime.now(timezone.utc)
    if value.astimezone(timezone.utc)<now+timedelta(minutes=1):
        raise ValueError('Оберіть час щонайменше на хвилину пізніше за поточний.')
    return utc_string(value.astimezone(timezone.utc))


def link_buttons(text):
    if text.strip()=='-':
        return []
    lines=text.strip().splitlines()
    if not 1<=len(lines)<=6:
        raise ValueError('Додайте від 1 до 6 кнопок, кожну з нового рядка.')
    buttons=[]
    for line in lines:
        if '|' not in line:
            raise ValueError('Формат: Назва кнопки | https://посилання')
        title,url=(part.strip() for part in line.split('|',1))
        parsed=urlparse(url)
        if not 1<=len(title)<=40 or len(url)>2048 or parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password or any(ch.isspace() for ch in url):
            raise ValueError('Перевірте назву (до 40 символів) та повне посилання https://…')
        buttons.append({'text':title,'url':url})
    return buttons


class ActorMessage:
    def __init__(self,message,user):
        self._message=message
        self.from_user=user

    def __getattr__(self,name):
        return getattr(self._message,name)


class PublicationPaused(Exception):
    pass


class PartialPublicationError(Exception):
    def __init__(self,ids):
        super().__init__('Медіа надіслано, але додаткові кнопки не підтверджені. Перевірте канал.')
        self.ids=ids


class PublicationEditor:
    def __init__(self,app):
        self.app=app
        self.wake=asyncio.Event()

    @property
    def store(self):
        return self.app.store

    @property
    def repo(self):
        return self.store.posts

    @staticmethod
    def actor_message(callback):
        return ActorMessage(callback.message,callback.from_user)

    def admin(self,uid):
        return self.app.get_role(uid) in ('owner','admin')

    def access(self,post,uid):
        return bool(post and self.app.get_role(uid) and (self.admin(uid) or post['author_id']==uid))

    def get(self,post_id,uid,editable=False):
        post=self.repo.get(post_id)
        if not self.access(post,uid):
            raise ValueError('Допис не знайдено або немає доступу.')
        if editable and post['status'] not in EDITABLE:
            raise ValueError('Спершу поверніть допис у чернетки. Опублікований допис можна скопіювати.')
        return post

    @staticmethod
    def rendered(post):
        origin=json.loads(post['origin'])
        text='\n\n'.join(x for x in (post['header'],post['body_text'],origin.get('attribution_text',''),post['footer']) if x)
        html='\n\n'.join(x for x in (escape(post['header']),post['body_html'],origin.get('attribution_html',''),escape(post['footer'])) if x)
        return text,html

    def validate(self,post,target=True):
        if target and json.loads(post['origin']).get('kind')=='template_edit':
            raise ValueError('Це редагування шаблону. Збережіть зміни, а для публікації виберіть «Використати шаблон».')
        text,_=self.rendered(post)
        media=json.loads(post['media'])
        if json.loads(post['origin']).get('kind')=='template' and not post['body_text'].strip():
            raise ValueError('Спершу заповніть перевірені дані шаблону.')
        if not text and not media:
            raise ValueError('Додайте текст або медіа.')
        if units(text)>(1024 if media else 4096):
            raise ValueError('Текст із підписами перевищує ліміт: 1024 символи для медіа або 4096 для тексту. Скоротіть його.')
        if len(media)>10:
            raise ValueError('До допису можна додати не більше 10 фото або відео.')
        for item in media[1:]:
            if units(item.get('caption_text',''))>1024:
                raise ValueError('Підпис одного з вкладень перевищує 1024 символи.')
        if post['edit_of']:self.validate_correction(post)
        if post['related_to']:
            if not post['body_text'].strip() and not media:
                raise ValueError('Додайте текст або медіа уточнення.')
            original=self.repo.get(post['related_to'])
            if not original or original['status']!='published' or not json.loads(original['sent_ids']) or post['target_id']!=original['target_id']:
                raise ValueError('Оригінал уточнення недоступний або канал змінено.')
            if any(item['kind']=='copy' for item in media):
                raise ValueError('Для уточнення використайте текст або завантажене медіа.')
        if any(item['kind']=='copy' for item in media) and (text or json.loads(post['buttons'])):
            raise ValueError('Для цього типу допису доступне лише пересилання без змін. Приберіть текст, підписи та кнопки.')
        if target and not post['target_id']:
            raise ValueError('Оберіть канал для цього допису. Або спершу додайте свій канал у розділі «Канали».')
        if target and self.store.setting('publish_target') and str(post['target_id']) != self.store.setting('publish_target'):
            raise ValueError('Допис прив’язаний до іншого каналу. Застосуйте поточний канал у редакторі допису.')

    async def verify_target(self,reference):
        chat=await self.app.bot.get_chat(reference)
        if chat.type!='channel':
            raise ValueError('Оберіть саме канал Telegram.')
        me=await self.app.bot.get_me()
        member=await self.app.bot.get_chat_member(chat.id,me.id)
        if member.status!='administrator' or not member.can_post_messages:
            raise ValueError('Додайте бота адміністратором цього каналу з правом публікації.')
        return chat

    async def dashboard(self,message,kind=None,page=0):
        uid=message.from_user.id
        if kind is None:
            rows=[[('➕ Створити публікацію','post:new'),('⚡ Шаблони повідомлень','post:templates')],
                  [('📝 Чернетки','post:list:draft:0'),('🔎 На погодженні','post:list:review:0')],
                  [('🗓 Заплановані','post:list:scheduled:0'),('✅ Опубліковані','post:list:published:0')],
                  [('⚠️ Помилки','post:list:failed:0'),('📁 Архів','post:list:archived:0')],
                  [('🗂 Усі дописи','post:list:all:0'),('❔ Перевірити канал','post:list:uncertain:0')]]
            if self.admin(uid):
                paused=self.store.setting('publishing_paused','0')=='1'
                rows.append([(('▶️ Відновити всі публікації' if paused else '🛑 Зупинити всі публікації'),f'control:pause:{0 if paused else 1}'),('📨 Новини від читачів','news:queue:0')])
            rows.append([('🏠 Меню','menu:home')])
            await message.answer('📝 <b>Публікації</b>\n\nСтворіть допис або виберіть потрібний розділ.\nЗбережені шаблони доступні в «Шаблонах повідомлень».',parse_mode='HTML',reply_markup=self.app.keyboard(rows,uid))
            return
        allowed=('all','draft','review','scheduled','published','failed','uncertain','archived')
        if kind not in allowed:
            raise ValueError('Невідомий список.')
        clauses,args=[],[]
        if not self.admin(uid):
            clauses.append('author_id=?');args.append(uid)
        if kind!='all':
            clauses.append('status=?');args.append(kind)
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        total=self.store.one('SELECT count(*) FROM posts'+where,args)[0]
        page=max(0,min(page,max(0,(total-1)//8)))
        posts=self.store.db.execute('SELECT * FROM posts'+where+' ORDER BY id DESC LIMIT 8 OFFSET ?',(*args,page*8)).fetchall()
        rows=[]
        for post in posts:
            label=(post['body_text'] or ('Медіа' if json.loads(post['media']) else 'Без тексту')).replace('\n',' ')[:25]
            rows.append([(f"№{post['id']} · {POST_STATUS[post['status']]} · {label}",f"post:open:{post['id']}")])
        nav=[]
        if page:
            nav.append(('⬅️ Попередні дописи',f'post:list:{kind}:{page-1}'))
        if (page+1)*8<total:
            nav.append(('Наступні дописи ➡️',f'post:list:{kind}:{page+1}'))
        if nav:rows.append(nav)
        rows += [[('⬅️ Публікації','menu:posts')]]
        title='Усі дописи' if kind=='all' else POST_STATUS[kind]
        await message.answer(f'📝 <b>Публікації · {escape(title)}</b>\n\nДописи: {total} · сторінка {page+1}\n'
            'Створіть допис, перегляньте його та оберіть публікацію зараз або за розкладом.'+
            ('' if self.admin(uid) else '\nВаші чернетки перед публікацією погоджує адміністратор.'),parse_mode='HTML',reply_markup=self.app.keyboard(rows,uid))

    @staticmethod
    def local_time(value):
        return datetime.strptime(value,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m.%Y %H:%M')

    async def card(self,message,post,advanced=False):
        uid=message.from_user.id
        post=self.get(post['id'],uid)
        pid=post['id']; revision=post['revision']; state=post['status']
        text,_=self.rendered(post)
        media=json.loads(post['media']); buttons=json.loads(post['buttons'])
        origin=json.loads(post['origin'])
        rows=[[(f'👁 Перегляд №{pid}',f'post:preview:{pid}'),(f'🔄 Оновити №{pid}',f'post:open:{pid}')]]
        if state in EDITABLE:
            rows += [[(f'✏️ Текст №{pid}',f'post:content:{pid}'),(f'🖼 Додати медіа №{pid}',f'post:media:{pid}')],
                     [(f'⬆️ Підпис зверху №{pid}',f'post:header:{pid}'),(f'⬇️ Підпис знизу №{pid}',f'post:footer:{pid}')],
                     [(f'🔗 Кнопки №{pid}',f'post:buttons:{pid}'),(f'📡 Канал №{pid}',f'post:target:{pid}')],
                     [((f'🔕 Без звуку №{pid}' if post['silent'] else f'🔔 Зі звуком №{pid}'),f'post:silent:{pid}:{0 if post["silent"] else 1}')]]
            if media:rows.append([(f'🗑 Прибрати медіа №{pid}',f'post:clearmedia:{pid}')])
            if self.admin(uid):
                rows.append([(f'📤 Опублікувати зараз №{pid}',f'post:send:{pid}:{revision}'),(f'🗓 Запланувати №{pid}',f'post:schedule:{pid}')])
            elif state!='review':
                rows.append([(f'🔎 На погодження №{pid}',f'post:submit:{pid}')])
            rows.append([(f'📁 В архів №{pid}',f'post:archive:{pid}')])
        if post['edit_of']:
            rows=[[(('💾 Застосувати зміни №'+str(pid)) if data.startswith('post:send:') else label,data) for label,data in row if data.split(':')[1] not in ('media','target','clearmedia','silent')] for row in rows]
            rows=[row for row in rows if row]
        if self.admin(uid) and state=='published':
            rows.append([(f'✏️ Виправити в каналі №{pid}',f'post:correct:{pid}')])
        if state=='published' and not post['edit_of']:
            rows.append([(f'🧩 Додати уточнення №{pid}',f'post:clarify:{pid}')])
        if post['related_to']:
            rows.append([(f'📎 Оригінальна новина №{post["related_to"]}',f'post:open:{post["related_to"]}')])
        related=self.store.one('SELECT count(*) FROM posts WHERE related_to=? AND edit_of IS NULL'+('' if self.admin(uid) else ' AND author_id=?'),(pid,) if self.admin(uid) else (pid,uid))[0]
        if related:rows.append([(f'🧩 Уточнення до №{pid} · {related}',f'post:related:{pid}:0')])
        if origin.get('duplicate_of'):
            rows.append([(f'♻️ Схожа новина №{origin["duplicate_of"]}',f'post:open:{origin["duplicate_of"]}')])
        if origin.get('kind')=='source' and origin.get('filter_text'):
            rows.append([(f'📄 Оригінал джерела №{pid}',f'post:original:{pid}')])
        if self.admin(uid) and state in ('scheduled','queued','review','failed'):
            rows.append([(f'↩️ У чернетки №{pid}',f'post:unschedule:{pid}')])
        if self.admin(uid) and state=='uncertain':
            rows.append([(f'🔎 Я перевірив канал №{pid}',f'post:check:{pid}')])
        rows += [[(f'💾 Зберегти шаблон №{pid}',f'post:savetemplate:{pid}')],[(f'📋 Створити копію №{pid}',f'post:clone:{pid}')], [('⬅️ Публікації','menu:posts')]]
        basic={'preview','content','media','send','schedule','submit','correct','clarify','check','original','open','related','unschedule'}
        if origin.get('kind')=='template_edit':
            rows=[[item for item in row if item[1].split(':')[1] not in ('send','schedule','submit')] for row in rows]
            rows=[row for row in rows if row]
            if state in EDITABLE:rows.insert(0,[(f'💾 Зберегти зміни шаблону №{pid}',f'post:updatesaved:{pid}')])
            basic.add('updatesaved')
        if state=='review':basic.discard('unschedule')
        if not advanced:
            rows=[[item for item in row if item[1].startswith('menu:') or item[1].split(':')[1] in basic] for row in rows]
            rows=[row for row in rows if row]
            # The card already shows current state; keep refresh under extra settings.
            rows=[[item for item in row if item[1]!=f'post:open:{pid}'] for row in rows]
            rows=[row for row in rows if row]
            rows.insert(len(rows)-1,[(f'⚙️ Додатково №{pid}',f'post:advanced:{pid}')])
        else:
            rows.insert(0,[(f'⬅️ Основні дії №{pid}',f'post:open:{pid}')])
        note=('\n🛑 Загальна пауза: надсилання зупинене.' if self.store.setting('publishing_paused','0')=='1' else '')
        if post['edit_of']:note+=f'\nВиправлення опублікованого допису №{post["edit_of"]}.'
        if post['related_to']:note+=f'\n🧩 Уточнення до допису №{post["related_to"]}. Буде надіслано відповіддю на оригінал у каналі.'
        if origin.get('kind')=='template':note+='\nШаблон: '+origin.get('template_title','')
        if origin.get('kind')=='source':
            note+='\nДжерело: '+origin.get('source_title','')
            if origin.get('source_date'):note+=' · '+datetime.fromisoformat(origin['source_date']).astimezone(ZoneInfo('Europe/Kyiv')).strftime('%d.%m %H:%M')+' Київ'
            if origin.get('alert',{}).get('reason'):note+='\n🔎 '+origin['alert']['reason']
        if origin.get('duplicate_of'):note+=f'\n♻️ Можливий повтор №{origin["duplicate_of"]}. Перевірте відмінності перед публікацією.'
        note+=f"\nПублікація: {self.local_time(post['due'])} · Київ" if state=='scheduled' and post['due'] else ''
        if post['error']:note+='\n⚠️ '+post['error'][:400]
        if state=='published' and post['sent_ids']!='[]':
            ids=json.loads(post['sent_ids'])
            if str(post['target_id']).startswith('-100'):
                note+=f"\nhttps://t.me/c/{str(post['target_id'])[4:]}/{ids[0]}"
        preview=text[:450]+('…' if len(text)>450 else '')
        await message.answer(f"📝 <b>Допис №{pid}</b> · {escape(POST_STATUS[state])}\n\n"
            f"Канал: {escape(post['target_title'] or 'Не обрано')}\nМедіа: {len(media)} · кнопок: {len(buttons)}\n"
            f"Звук: {'вимкнено' if post['silent'] else 'увімкнено'}\n\n{escape(preview or 'Текст ще не додано')}\n{escape(note)}\n\n"
            + ('Редагування шаблону: натисніть «Зберегти зміни шаблону», коли завершите.' if origin.get('kind')=='template_edit' else 'Зміни зберігаються автоматично. «Опублікувати зараз» одразу ставить допис у чергу.' if self.admin(uid) else 'Зміни зберігаються автоматично. Надішліть готовий допис на погодження.'),
            parse_mode='HTML',reply_markup=self.app.keyboard(rows,uid))

    async def prompt(self,message,mode,post_id,text):
        self.store.cancel(message.from_user.id)
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)',(message.from_user.id,f'post_{mode}:{post_id}'))
        await message.answer(text,reply_markup=self.app.keyboard([[(f'✅ Готово · допис №{post_id}',f'post:open:{post_id}')],[('⬅️ Публікації','menu:posts')]],message.from_user.id))

    async def action(self,callback):
        uid=callback.from_user.id; message=self.actor_message(callback)
        if message.chat.type!='private' or not self.app.get_role(uid):
            await callback.answer('⛔ Немає доступу.',show_alert=True)
            return
        self.store.cancel(uid)
        await callback.answer()
        parts=callback.data.split(':'); action=parts[1] if len(parts)>1 else ''
        try:
            if action=='templates':
                page=int(parts[2]) if len(parts)==3 and parts[2].isdigit() else 0
                total=self.store.one('SELECT count(*) FROM saved_templates WHERE user_id=?',(uid,))[0]
                page=max(0,min(page,max(0,(total-1)//8)))
                rows=[[(title,f'post:template:{key}')] for key,(title,_) in TEMPLATES.items()]
                saved=self.store.db.execute('SELECT id,title FROM saved_templates WHERE user_id=? ORDER BY id DESC LIMIT 8 OFFSET ?',(uid,page*8)).fetchall()
                rows += [[('💾 '+item['title'],f'post:savedcard:{item["id"]}')] for item in saved]
                nav=[]
                if page:nav.append(('⬅️ Попередні шаблони',f'post:templates:{page-1}'))
                if (page+1)*8<total:nav.append(('Наступні шаблони ➡️',f'post:templates:{page+1}'))
                if nav:rows.append(nav)
                rows.append([('⬅️ Публікації','menu:posts')])
                await message.answer(f'⚡ Шаблони повідомлень\n\nВласних збережено: {total}.\nВибір створює окрему чернетку — шаблон залишається для наступного разу.\nЩоб зберегти свій: відкрийте допис → Додатково → Зберегти шаблон.',reply_markup=self.app.keyboard(rows,uid))
                return
            if action in ('savedcard','usesaved','editsaved','renamesaved','deletesaved','deleteok') and len(parts)>=3 and parts[2].isdigit():
                saved=self.store.one('SELECT * FROM saved_templates WHERE id=? AND user_id=?',(int(parts[2]),uid))
                if not saved:raise ValueError('Шаблон не знайдено.')
                tid=saved['id'];version=saved['revision']
                if action=='savedcard':
                    payload=json.loads(saved['payload'])
                    await message.answer('💾 <b>'+escape(saved['title'])+'</b>\n\n'+escape(payload['body_text'][:1800] or 'Медіа без тексту'),parse_mode='HTML',reply_markup=self.app.keyboard([
                        [('📤 Використати шаблон',f'post:usesaved:{tid}')],
                        [('✏️ Редагувати шаблон',f'post:editsaved:{tid}'),('🏷 Змінити назву',f'post:renamesaved:{tid}')],
                        [('🗑 Видалити шаблон',f'post:deletesaved:{tid}')],[('⚡ Шаблони повідомлень','post:templates')]],uid))
                    return
                if action=='renamesaved':
                    with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)',(uid,f'post_template_title:{tid}:{version}'))
                    await message.answer('Введіть нову назву шаблону, до 60 символів.',reply_markup=self.app.keyboard([[('⚡ Шаблони повідомлень','post:templates')]],uid))
                    return
                if action=='deletesaved':
                    await message.answer('Видалити шаблон «'+saved['title']+'»? Створені з нього дописи залишаться.',reply_markup=self.app.keyboard([[('🗑 Так, видалити',f'post:deleteok:{tid}:{version}')],[('↩️ Залишити',f'post:savedcard:{tid}')]],uid))
                    return
                if action=='deleteok':
                    if len(parts)!=4 or parts[3]!=str(version):raise ValueError('Шаблон змінено. Відкрийте його заново.')
                    with self.store.db:self.store.db.execute('DELETE FROM saved_templates WHERE id=? AND user_id=? AND revision=?',(tid,uid,version))
                    await message.answer('✅ Шаблон видалено.',reply_markup=self.app.keyboard([[('⚡ Шаблони повідомлень','post:templates')]],uid))
                    return
                target=self.store.setting('publish_target')
                post=self.repo.create(uid,int(target) if target else None,self.store.setting('publish_target_title'))
                post=self.repo.edit(post['id'],uid,**json.loads(saved['payload']),media=saved['media'])
                if action=='editsaved':
                    with self.store.db:self.store.db.execute('UPDATE posts SET origin=? WHERE id=?',(json.dumps({'kind':'template_edit','template_id':tid,'template_revision':version}),post['id']))
                await self.card(message,post)
                return
            if action=='template' and len(parts)==3:
                if parts[2] not in TEMPLATES:raise ValueError('Шаблон не знайдено.')
                title,prompt=TEMPLATES[parts[2]]
                target=self.store.setting('publish_target')
                post=self.repo.create(uid,int(target) if target else None,self.store.setting('publish_target_title'),self.store.setting('publish_header'),self.store.setting('publish_footer'))
                with self.store.db:self.store.db.execute('UPDATE posts SET origin=? WHERE id=?',(json.dumps({'kind':'template','template_title':title,'template_key':parts[2]},ensure_ascii=False),post['id']))
                await self.prompt(message,'template_details',post['id'],title+'\n\n'+prompt+'\nНадішліть дані одним текстовим повідомленням. Час події вкажіть самостійно після перевірки.')
                return
            if action=='new':
                target=self.store.setting('publish_target')
                post=self.repo.create(uid,int(target) if target else None,self.store.setting('publish_target_title'),self.store.setting('publish_header'),self.store.setting('publish_footer'))
                await self.prompt(message,'content',post['id'],'Надішліть текст допису або фото/відео з підписом.\nТекст із форматуванням Telegram збережеться. Медіа можна додати окремо; для альбому натисніть «Готово» після завантаження.')
                return
            if action=='list' and len(parts)==4 and parts[3].isdigit():
                await self.dashboard(message,parts[2],int(parts[3]));return
            if len(parts)<3 or not parts[2].isdigit():
                raise ValueError('Некоректна дія.')
            pid=int(parts[2]);post=self.get(pid,uid)
            if action=='open':await self.card(message,post)
            elif action=='advanced':await self.card(message,post,advanced=True)
            elif action=='updatesaved':
                self.get(pid,uid,editable=True);self.validate(post,target=False)
                origin=json.loads(post['origin'])
                if origin.get('kind')!='template_edit':raise ValueError('Відкрийте шаблон через «Редагувати шаблон».')
                payload={key:post[key] for key in ('body_text','body_html','header','footer','buttons','silent')}
                with self.store.db:
                    changed=self.store.db.execute('UPDATE saved_templates SET payload=?,media=?,revision=revision+1 WHERE id=? AND user_id=? AND revision=?',
                        (json.dumps(payload,ensure_ascii=False),post['media'],origin['template_id'],uid,origin['template_revision'])).rowcount
                    if not changed:raise ValueError('Шаблон видалено або змінено. Відкрийте його заново.')
                    self.store.db.execute("UPDATE posts SET status='archived',revision=revision+1 WHERE id=?",(pid,))
                await message.answer('✅ Зміни шаблону збережено.',reply_markup=self.app.keyboard([[('⚡ Шаблони повідомлень','post:templates')]],uid))
            elif action=='savetemplate':
                self.validate(post,target=False)
                if any(item['kind']=='copy' for item in json.loads(post['media'])):raise ValueError('Для шаблону додайте власний текст або завантажені фото/відео.')
                await self.prompt(message,'save_template',pid,'💾 Введіть назву шаблону (до 60 символів). Збережуться текст, медіа, підписи, кнопки та звук. Канал підставлятиметься поточний.')
            elif action=='original':
                origin=json.loads(post['origin']);original=origin.get('filter_text')
                if origin.get('kind')!='source' or not original:raise ValueError('Текст оригіналу не збережено.')
                await message.answer('📄 Оригінал із джерела «'+origin.get('source_title','')+'»')
                for offset in range(0,len(original),3500):await message.answer(original[offset:offset+3500])
                await self.card(message,post)
            elif action=='clarify':
                clarification=self.repo.clarification(pid,uid,self.store.setting('publish_header'),self.store.setting('publish_footer'))
                await self.prompt(message,'content',clarification['id'],f'🧩 Уточнення до допису №{pid}. Надішліть текст або медіа з підписом. Воно збережеться окремо й буде опубліковане відповіддю на оригінал після погодження.')
            elif action=='related':
                page=max(0,int(parts[3])) if len(parts)==4 and parts[3].isdigit() else 0
                where='related_to=? AND edit_of IS NULL'+('' if self.admin(uid) else ' AND author_id=?')
                args=(pid,) if self.admin(uid) else (pid,uid)
                total=self.store.one('SELECT count(*) FROM posts WHERE '+where,args)[0]
                page=min(page,max(0,(total-1)//8))
                related=self.store.db.execute('SELECT * FROM posts WHERE '+where+' ORDER BY id DESC LIMIT 8 OFFSET ?',(*args,page*8)).fetchall()
                rows=[[(f'№{item["id"]} · {POST_STATUS[item["status"]]}',f'post:open:{item["id"]}')] for item in related]
                if page:rows.append([('⬅️ Попередні уточнення',f'post:related:{pid}:{page-1}')])
                if (page+1)*8<total:rows.append([('Наступні уточнення ➡️',f'post:related:{pid}:{page+1}')])
                rows.append([(f'📎 До новини №{pid}',f'post:open:{pid}')])
                await message.answer(f'🧩 Уточнення до допису №{pid}: {total}',reply_markup=self.app.keyboard(rows,uid))
            elif action=='preview':
                self.validate(post,target=False)
                await self.send(post,uid,preview=True)
                await self.card(message,post)
            elif action in ('content','media','header','footer','buttons','target','schedule'):
                self.get(pid,uid,editable=True)
                if post['edit_of'] and action in ('media','target'):
                    raise ValueError('У виправленні можна змінити текст, підписи та кнопки. Для іншого медіа або каналу створіть копію.')
                if action=='schedule' and not self.admin(uid):
                    raise ValueError('Запланувати публікацію може адміністратор.')
                if action=='target':
                    target=self.store.setting('publish_target')
                    if not target:raise ValueError('Спершу підключіть ваш єдиний канал у розділі «Канали».')
                    post=self.repo.edit(pid,uid,target_id=int(target),target_title=self.store.setting('publish_target_title'))
                    await self.card(message,post)
                    return
                prompts={
                    'content':'Надішліть новий текст або «-», щоб прибрати текст. Фото/відео з підписом також можна додати тут.',
                    'media':'Надішліть фото, відео або альбом — до 10 вкладень. Потім натисніть «Готово».\nДокумент або анімація можуть бути єдиним вкладенням.',
                    'header':'Введіть підпис зверху, до 300 символів. «-» — прибрати його.',
                    'footer':'Введіть підпис знизу, до 300 символів. «-» — прибрати його.',
                    'buttons':'Надішліть до 6 кнопок, кожну з нового рядка:\nНазва кнопки | https://посилання\n\nНаприклад:\nЗамовити шеврон | https://t.me/your_bot?start=patch_1\n\n«-» — прибрати кнопки. В альбомі кнопки будуть окремим повідомленням після медіа.',
                    'target':'Введіть @username, посилання t.me або числовий ID каналу для цього допису. Бот має бути його адміністратором із правом публікації.',
                    'schedule':'Введіть дату й час за Києвом: ДД.ММ.РРРР ГГ:ХХ.\nНаприклад: 28.10.2026 18:30.\nБот має працювати у запланований час. Дату буде показано для підтвердження.',
                }
                await self.prompt(message,action,pid,prompts[action])
            elif action in ('silent','clearmedia'):
                self.get(pid,uid,editable=True)
                if post['edit_of'] and action=='clearmedia':raise ValueError('Не можна прибрати медіа з уже опублікованого допису.')
                if action=='clearmedia':post=self.repo.edit(pid,uid,media='[]')
                elif len(parts)==4 and parts[3] in ('0','1'):post=self.repo.edit(pid,uid,silent=int(parts[3]))
                else:raise ValueError('Некоректна дія.')
                await self.card(message,post)
            elif action=='correct':
                if not self.admin(uid):raise ValueError('Виправляти опубліковані дописи може адміністратор.')
                await self.card(message,self.repo.correction(pid,uid))
            elif action=='clone':
                clone=self.repo.create(uid,post['target_id'],post['target_title'],post['header'],post['footer'])
                clone=self.repo.edit(clone['id'],uid,body_html=post['body_html'],body_text=post['body_text'],media=post['media'],buttons=post['buttons'],silent=post['silent'])
                await self.card(message,clone)
            elif action=='submit':
                self.get(pid,uid,editable=True);self.validate(post)
                post=self.repo.submit(pid,uid)
                for admin_id in self.app.ADMIN_IDS:
                    await self.notice(admin_id,f'🔎 Допис №{pid} очікує погодження.',pid)
                await self.card(message,post)
            elif action in ('publish','send','scheduleok','unschedule','check','checked'):
                if not self.admin(uid):raise ValueError('Публікувати та планувати дописи може адміністратор.')
                if action in ('publish','send','scheduleok'):
                    self.get(pid,uid,editable=True);self.validate(post)
                    await self.verify_target(post['target_id'])
                if action=='publish':
                    await self.send(post,uid,preview=True)
                    await message.answer((f"Оновити текст/підпис допису №{post['edit_of']} у каналі «{post['target_title']}» без нової публікації?" if post['edit_of'] else f"Опублікувати допис №{pid} у канал «{post['target_title']}» зараз?"),reply_markup=self.app.keyboard([[((f'✅ Застосувати зміни №{pid}' if post['edit_of'] else f'✅ Так, публікувати №{pid}'),f'post:send:{pid}:{post["revision"]}')],[(f'↩️ Редагувати №{pid}',f'post:open:{pid}')]],uid))
                elif action in ('send','scheduleok'):
                    if len(parts)!=(4 if action=='send' else 5) or not parts[3].isdigit():raise ValueError('Оновіть картку допису.')
                    due=None
                    if action=='scheduleok':
                        due=datetime.fromtimestamp(int(parts[4]),timezone.utc)
                        if due<datetime.now(timezone.utc)+timedelta(seconds=5):raise ValueError('Запланований час уже минув. Оберіть новий.')
                        due=utc_string(due)
                    post=self.repo.queue(pid,int(parts[3]),uid,due)
                    self.wake.set();await self.card(message,post)
                elif action=='unschedule':await self.card(message,self.repo.draft(pid,uid))
                elif action=='check':
                    if post['status']!='uncertain':raise ValueError('Оновіть картку допису.')
                    await message.answer('Перевірте канал. Якщо допис уже з’явився, не надсилайте його повторно.\nПовернути в чернетки лише якщо ви готові до можливого дублювання?',reply_markup=self.app.keyboard([[(f'Повернути після перевірки №{pid}',f'post:checked:{pid}')],[(f'Залишити без повтору №{pid}',f'post:open:{pid}')]],uid))
                elif action=='checked':await self.card(message,self.repo.draft(pid,uid,from_uncertain=True))
            elif action=='archive':
                self.repo.archive(pid,uid)
                if json.loads(post['origin']).get('kind')=='reader':await self.app.news.rejected(post)
                await self.dashboard(message)
            else:raise ValueError('Невідома дія.')
        except (ValueError,TelegramAPIError) as exc:
            text=str(exc) if isinstance(exc,ValueError) else 'Telegram не виконав дію: '+exc.message[:300]
            await message.answer(text,reply_markup=self.app.keyboard([[('⬅️ Публікації','menu:posts')]],uid))

    async def input(self,message,mode):
        uid=message.from_user.id
        if not self.app.get_role(uid):
            self.store.cancel(uid)
            await message.answer('⛔ Немає доступу до редактора.')
            return True
        try:
            field,raw_id=mode.removeprefix('post_').split(':',1)
            if field=='template_title':
                tid,version=map(int,raw_id.split(':'))
                title=(message.text or '').strip()
                if not 1<=len(title)<=60:raise ValueError('Назва має містити від 1 до 60 символів.')
                with self.store.db:
                    changed=self.store.db.execute('UPDATE saved_templates SET title=?,revision=revision+1 WHERE id=? AND user_id=? AND revision=?',(title,tid,uid,version)).rowcount
                if not changed:raise ValueError('Шаблон видалено або змінено. Відкрийте його заново.')
                self.store.cancel(uid)
                await message.answer('✅ Назву змінено.',reply_markup=self.app.keyboard([[('⚡ Шаблони повідомлень','post:templates')]],uid))
                return True
            post=self.get(int(raw_id),uid,editable=field!='save_template')
            if field=='save_template':
                title=(message.text or '').strip()
                if not 1<=len(title)<=60:raise ValueError('Введіть назву шаблону від 1 до 60 символів.')
                self.validate(post,target=False)
                if any(item['kind']=='copy' for item in json.loads(post['media'])):raise ValueError('Для шаблону використайте завантажені медіа.')
                payload={key:post[key] for key in ('body_text','body_html','header','footer','buttons','silent')}
                with self.store.db:self.store.db.execute('INSERT INTO saved_templates(user_id,title,payload,media) VALUES(?,?,?,?)',(uid,title,json.dumps(payload,ensure_ascii=False),post['media']))
                self.store.cancel(uid)
                await message.answer('✅ Шаблон збережено. Він доступний у «Шаблонах повідомлень» і залишиться після публікації.',reply_markup=self.app.keyboard([[('⚡ Шаблони повідомлень','post:templates')],[('⬅️ Публікації','menu:posts')]],uid))
                return True
            pid=post['id'];text=(message.text or '').strip()
            if post['edit_of'] and (field in ('media','target') or (field=='content' and message.text is None)):
                raise ValueError('У виправленні надсилайте лише текст. Медіа та канал зберігаються з оригіналу.')
            if field=='template_details':
                origin=json.loads(post['origin'])
                if origin.get('kind')!='template' or not text or text=='-':raise ValueError('Надішліть текст із перевіреними даними для шаблону.')
                title=origin['template_title']
                post=self.repo.edit(pid,uid,body_text=title+'\n\n'+message.text,body_html='<b>'+escape(title)+'</b>\n\n'+message.html_text)
            elif field in ('content','media'):
                media=json.loads(post['media'])
                if message.text is not None and field=='content':
                    post=self.repo.edit(pid,uid,body_html='' if text=='-' else message.html_text,body_text='' if text=='-' else message.text)
                else:
                    item=None
                    if message.photo:item={'kind':'photo','file_id':message.photo[-1].file_id}
                    elif message.video:item={'kind':'video','file_id':message.video.file_id}
                    elif message.animation:item={'kind':'animation','file_id':message.animation.file_id}
                    elif message.document:item={'kind':'document','file_id':message.document.file_id}
                    if not item:raise ValueError('Надішліть фото або відео. Для тексту скористайтеся кнопкою «Текст».')
                    origin=f'{message.chat.id}:{message.message_id}'
                    if any(x.get('origin')==origin for x in media):return True
                    if len(media)>=10:raise ValueError('До допису можна додати не більше 10 вкладень.')
                    if media and (item['kind'] not in ('photo','video') or any(x['kind'] not in ('photo','video') for x in media)):
                        raise ValueError('Документ або анімація можуть бути єдиним вкладенням. Для альбому використайте фото/відео.')
                    item['origin']=origin
                    fields={}
                    if message.caption:
                        if not media:
                            if post['body_text'] and field=='media':
                                fields.update(body_text=post['body_text']+'\n\n'+message.caption,body_html=post['body_html']+'\n\n'+message.html_text)
                            else:fields.update(body_text=message.caption,body_html=message.html_text)
                        else:item.update(caption_text=message.caption,caption_html=message.html_text)
                    media.append(item)
                    post=self.repo.edit(pid,uid,media=json.dumps(media),**fields)
                    # Keep this mode open so all messages of a Telegram album are received.
                    with self.store.db:
                        self.store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)',(uid,f'post_media:{pid}'))
                    if not message.media_group_id:
                        self.store.cancel(uid)
                        await self.card(message,post)
                    return True
            elif field in ('header','footer'):
                if not text or len(text)>300:raise ValueError('Введіть від 1 до 300 символів або «-», щоб прибрати підпис.')
                post=self.repo.edit(pid,uid,**{field:'' if text=='-' else text})
            elif field=='buttons':
                post=self.repo.edit(pid,uid,buttons=json.dumps(link_buttons(text),ensure_ascii=False))
            elif field=='target':
                chat=await self.verify_target(ChannelBridge.channel_reference(text))
                if str(chat.id)!=self.store.setting('publish_target'):
                    raise ValueError('Для всіх дописів використовується один канал. Змініть його в розділі «Канали».')
                post=self.repo.edit(pid,uid,target_id=chat.id,target_title=chat.title)
            elif field=='schedule':
                if not self.admin(uid):raise ValueError('Недостатньо прав для планування.')
                self.validate(post)
                due=scheduled_time(text)
                epoch=int(datetime.strptime(due,'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp())
                self.store.cancel(uid)
                await message.answer(f"Запланувати допис №{pid} у канал «{post['target_title']}»?\nДата: {self.local_time(due)} · Київ",
                    reply_markup=self.app.keyboard([[(f'✅ Підтвердити розклад №{pid}',f'post:scheduleok:{pid}:{post["revision"]}:{epoch}')],[(f'↩️ Редагувати №{pid}',f'post:open:{pid}')]],uid))
                return True
            else:raise ValueError('Невідомий режим редактора.')
            self.store.cancel(uid)
            await self.card(message,post)
        except (ValueError,TelegramAPIError) as exc:
            text=str(exc) if isinstance(exc,ValueError) else 'Не вдалося перевірити канал у Telegram: '+exc.message[:250]
            await message.answer(text)
        return True

    def validate_correction(self,post):
        original=self.repo.get(post['edit_of'])
        if not original or original['status']!='published' or not json.loads(original['sent_ids']):
            raise ValueError('Оригінал допису недоступний для редагування.')
        if post['status']!='published' and original['revision']!=post['base_revision']:
            raise ValueError('Оригінал уже оновлено іншим виправленням. Створіть виправлення з його останньої версії.')
        if original['target_id']!=post['target_id'] or original['media']!=post['media']:
            raise ValueError('У виправленні не можна змінити канал або медіа.')
        media=json.loads(post['media'])
        if any(item['kind']=='copy' for item in media):
            raise ValueError('Цей тип пересланого повідомлення не підтримує виправлення тексту.')
        if len(media)>1 and len(json.loads(original['sent_ids']))<=len(media) and json.loads(post['buttons']):
            raise ValueError('До вже опублікованого альбому без кнопок не можна додати новий блок кнопок. Змініть лише підпис або створіть новий допис.')
        return original

    async def apply_update(self,post):
        original=self.validate_correction(post)
        ids=json.loads(original['sent_ids']);media=json.loads(post['media'])
        buttons=json.loads(post['buttons'])
        markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**item)] for item in buttons]) if buttons else None
        _,html=self.rendered(post)
        changed=False
        async def edit(function,**kwargs):
            nonlocal changed
            if self.store.setting('publishing_paused','0')=='1':
                if changed:
                    error=PartialPublicationError(ids);error.args=('Частину виправлень застосовано до паузи. Перевірте допис у каналі.',);raise error
                raise PublicationPaused()
            try:await function(**kwargs)
            except TelegramBadRequest as exc:
                if 'message is not modified' not in exc.message:raise
            changed=True
        try:
            if not media:
                await edit(self.app.bot.edit_message_text,chat_id=post['target_id'],message_id=ids[0],text=html,parse_mode='HTML',reply_markup=markup)
            else:
                await edit(self.app.bot.edit_message_caption,chat_id=post['target_id'],message_id=ids[0],caption=html or '',parse_mode='HTML',reply_markup=markup if len(media)==1 else None)
                if len(media)>1 and post['buttons']!=original['buttons']:
                    await edit(self.app.bot.edit_message_reply_markup,chat_id=post['target_id'],message_id=ids[-1],reply_markup=markup)
        except (PublicationPaused,PartialPublicationError):raise
        except Exception as exc:
            if changed:
                error=PartialPublicationError(ids);error.args=('Частину виправлень застосовано. Перевірте допис перед повтором.',);raise error from exc
            raise
        return ids

    async def send(self,post,chat_id,preview=False):
        if not preview and self.store.setting('publishing_paused','0')=='1':raise PublicationPaused()
        self.validate(post,target=False)
        _,html=self.rendered(post)
        media=json.loads(post['media']);buttons=json.loads(post['buttons'])
        if json.loads(post['origin']).get('kind')=='reader' and not buttons:
            buttons=await self.app.news.public_buttons(post['target_id'])
            if not preview:
                with self.store.db:self.store.db.execute('UPDATE posts SET buttons=? WHERE id=?',(json.dumps(buttons,ensure_ascii=False),post['id']))
        if not preview and self.store.setting('publishing_paused','0')=='1':raise PublicationPaused()
        markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**item)] for item in buttons]) if buttons else None
        if json.loads(post['origin']).get('kind')=='reader' and buttons:
            markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**item) for item in buttons]])
        options={'parse_mode':'HTML','disable_notification':True if preview else bool(post['silent'])}
        reply=None
        if post['related_to'] and not preview:
            original=self.repo.get(post['related_to'])
            reply=ReplyParameters(message_id=json.loads(original['sent_ids'])[0],allow_sending_without_reply=False)
            options['reply_parameters']=reply
        if not media:
            result=await self.app.bot.send_message(chat_id,html,reply_markup=markup,**options)
            return [result.message_id]
        if len(media)==1:
            item=media[0]
            if item['kind']=='copy':
                result=await self.app.bot.forward_message(chat_id,item['source_id'],item['message_id'])
                return [result.message_id]
            function=getattr(self.app.bot,'send_'+item['kind'])
            from account_reader import input_file
            result=await function(chat_id,**{item['kind']:input_file(item['file_id'])},caption=html or None,reply_markup=markup,**options)
            return [result.message_id]
        album=[]
        for index,item in enumerate(media):
            caption=html if index==0 else item.get('caption_html','')
            cls={'photo':InputMediaPhoto,'video':InputMediaVideo,'audio':InputMediaAudio,'document':InputMediaDocument}[item['kind']]
            from account_reader import input_file
            album.append(cls(media=input_file(item['file_id']),caption=caption or None,parse_mode='HTML'))
        album_options={'disable_notification':options['disable_notification']}
        if reply:album_options['reply_parameters']=reply
        results=await self.app.bot.send_media_group(chat_id,media=album,**album_options)
        ids=[result.message_id for result in results]
        if markup:
            if not preview and self.store.setting('publishing_paused','0')=='1':
                error=PartialPublicationError(ids);error.args=('Медіа опубліковано перед паузою, кнопки не надіслано. Перевірте канал.',);raise error
            try:
                result=await self.app.bot.send_message(chat_id,'Посилання до допису 👇',reply_markup=markup,disable_notification=options['disable_notification'])
                ids.append(result.message_id)
            except Exception as exc:
                if preview:
                    raise ValueError('Медіа перегляду надіслано, але кнопки — ні. Спробуйте перегляд ще раз.') from exc
                raise PartialPublicationError(ids) from exc
        return ids

    async def notice(self,user_id,text,post_id):
        if not self.app.get_role(user_id):return
        try:
            await self.app.bot.send_message(user_id,text,reply_markup=self.app.keyboard([[(f'📝 Відкрити допис №{post_id}',f'post:open:{post_id}')],[('⬅️ Публікації','menu:posts')]],user_id))
        except TelegramAPIError:
            logging.warning('Не вдалося сповістити user_id=%s про допис %s',user_id,post_id)

    async def process_one(self):
        if self.store.setting('publishing_paused','0')=='1':return False
        post=self.repo.claim()
        if not post:return False
        pid=post['id'];actor=post['approved_by']
        try:
            if not self.admin(actor):raise ValueError('Адміністратор, який підтвердив публікацію, більше не має доступу.')
            if post['auto_source'] and self.store.alerts.enabled:
                await self.app.alerts.refresh_target(post['target_id'])
                if self.app.alerts.apply_gate(post):return True
            if post['due'] and (datetime.now(timezone.utc)-datetime.strptime(post['due'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc))>timedelta(minutes=15):
                raise ValueError('Час публікації пропущено більш ніж на 15 хвилин. Перевірте актуальність і підтвердіть надсилання знову.')
            origin=json.loads(post['origin'])
            if post['auto_source'] and origin.get('kind')=='source':
                source=self.store.one('SELECT * FROM channel_sources WHERE chat_id=?',(origin['source_id'],))
                if not source or (source['transport']=='reader' and self.store.setting('reader_enabled','1')!='1') or source['mode']!='instant' or not self.store.editorial.matches(source,origin.get('filter_text',post['body_text'])):
                    with self.store.db:self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE id=?",(pid,))
                    return True
                stamp=datetime.fromisoformat(origin['source_date'])
                if datetime.now(timezone.utc)-stamp>timedelta(minutes=15):
                    with self.store.db:self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1,error='Новині понад 15 хвилин. Перевірте актуальність.' WHERE id=?",(pid,))
                    await self.notice(actor,f'🔎 Новину №{pid} повернуто на перевірку актуальності.',pid)
                    return True
            self.validate(post)
            await self.verify_target(post['target_id'])
            if post['auto_source']:
                source=self.store.one('SELECT * FROM channel_sources WHERE chat_id=?',(origin.get('source_id'),))
                if self.store.setting('autopublish','0')!='1' or not source or (source['transport']=='reader' and self.store.setting('reader_enabled','1')!='1') or source['mode']!='instant' or not self.store.editorial.matches(source,origin.get('filter_text',post['body_text'])):
                    with self.store.db:self.store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE id=?",(pid,))
                    return True
            if self.store.setting('publishing_paused','0')=='1':
                self.repo.release(pid);return False
            if post['auto_source'] and self.app.alerts.apply_gate(post):return True
        except (ValueError,TelegramAPIError) as exc:
            error=str(exc) if isinstance(exc,ValueError) else exc.message
            self.repo.finish(pid,'failed',error[:500])
            await self.notice(actor,f'⚠️ Допис №{pid} не опубліковано.\n{error[:300]}',pid)
            return True
        status='published';error=None;ids=[]
        try:
            if not post['edit_of']:self.store.alerts.sending(post,self.rendered(post)[0])
            ids=await self.apply_update(post) if post['edit_of'] else await self.send(post,post['target_id'])
        except PublicationPaused:
            self.repo.release(pid);return False
        except TelegramRetryAfter as exc:
            self.repo.finish(pid,'scheduled',retry_after=max(1,exc.retry_after))
            return True
        except PartialPublicationError as exc:
            status,error,ids='uncertain',str(exc),exc.ids
        except (TelegramBadRequest,TelegramForbiddenError) as exc:
            status,error='failed',exc.message
        except TelegramAPIError as exc:
            status,error='uncertain','Telegram не підтвердив результат надсилання. Перевірте канал перед повтором. '+exc.message[:250]
        except Exception:
            logging.exception('Невизначений результат надсилання допису %s',pid)
            status,error='uncertain','Результат надсилання невідомий. Перевірте канал перед повтором.'
        if status=='published' and post['edit_of']:
            try:self.repo.apply_correction(post)
            except Exception:
                logging.exception('Не вдалося зберегти результат виправлення %s',pid)
                status,error='uncertain','Зміни надіслано в Telegram, але локальну версію не оновлено. Перевірте канал.'
        self.repo.finish(pid,status,error,ids)
        if status=='published' and not post['edit_of']:
            self.store.alerts.sent(post,self.rendered(post)[0],ids)
        with self.store.db:self.store.db.execute('UPDATE channel_deliveries SET status=?,error=? WHERE post_id=?',( 'sent' if status=='published' else status,error,pid))
        if status=='published':self.store.editorial.remember(post['edit_of'] or pid,post['target_id'],post['body_text'])
        if status=='published' and json.loads(post['origin']).get('kind')=='reader':
            await self.app.news.published(post)
        label=(f"✅ Допис №{post['edit_of']} оновлено в каналі «{post['target_title']}»." if post['edit_of'] else f"✅ Допис №{pid} опубліковано в «{post['target_title']}».") if status=='published' else f'⚠️ Допис №{pid}: {POST_STATUS[status]}.\n{error}'
        for uid in {actor,post['author_id']}:
            await self.notice(uid,label,pid)
        return True

    async def worker(self):
        self.repo.recover()
        while True:
            try:
                if await self.process_one():continue
                self.wake.clear()
                try:await asyncio.wait_for(self.wake.wait(),timeout=5)
                except asyncio.TimeoutError:pass
            except asyncio.CancelledError:raise
            except Exception:
                logging.exception('Помилка черги публікацій')
                await asyncio.sleep(5)
