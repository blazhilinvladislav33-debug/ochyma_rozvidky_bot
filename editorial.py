import json
import re
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime,timezone,timedelta
from html import escape
from aiogram.exceptions import TelegramAPIError


def normalized(text):
    text=unicodedata.normalize('NFKC',text).casefold()
    text=re.sub(r'https?://\S+|t\.me/\S+',' ',text)
    return ' '.join(re.findall(r'\w+',text,flags=re.UNICODE))


def similar(left,right):
    if not left or not right:return False
    if left==right:return True
    if min(len(left),len(right))<80:return False
    # Preserve differences in numbers, negation, and alarm/clear messages.
    if re.findall(r'\d+',left)!=re.findall(r'\d+',right):return False
    key={'не','немає','відбій','тривога','скасовано','оголошено','жовтий','червоний','дронова','ракетна'}
    if (set(left.split())&key)!=(set(right.split())&key):return False
    a,b=set(left.split()),set(right.split())
    return len(a&b)/max(1,len(a|b))>=0.85 and SequenceMatcher(None,left,right).ratio()>=0.90


def terms(text):
    if text.strip()=='-':return []
    values=[x.strip().casefold() for x in re.split(r'[,;\n]',text) if x.strip()]
    if not values or len(values)>20 or any(len(x)>60 for x in values):
        raise ValueError('Вкажіть до 20 слів або фраз через кому, кожна до 60 символів. «-» — без фільтра.')
    return list(dict.fromkeys(values))


class EditorialStore:
    def __init__(self,db):
        self.db=db
        for table,definitions in {
            'channel_sources':{'mode':"TEXT NOT NULL DEFAULT 'instant'",'topics':"TEXT NOT NULL DEFAULT '[]'",'places':"TEXT NOT NULL DEFAULT '[]'"},
            'channel_deliveries':{'post_id':'INTEGER','duplicate_of':'INTEGER'},
            'posts':{'origin':"TEXT NOT NULL DEFAULT '{}'",'auto_source':'INTEGER NOT NULL DEFAULT 0','edit_of':'INTEGER','base_revision':'INTEGER','origin_key':'TEXT'},
        }.items():
            existing={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            for name,definition in definitions.items():
                if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        db.executescript('''
        CREATE UNIQUE INDEX IF NOT EXISTS post_origin_key ON posts(origin_key);
        CREATE TABLE IF NOT EXISTS news_fingerprints (
            post_id INTEGER PRIMARY KEY,target_id INTEGER NOT NULL,normalized TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS news_fingerprint_time ON news_fingerprints(target_id,created);
        CREATE TABLE IF NOT EXISTS reader_news (
            id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
            body_text TEXT NOT NULL DEFAULT '',body_html TEXT NOT NULL DEFAULT '',media TEXT NOT NULL DEFAULT '[]',
            place TEXT NOT NULL DEFAULT '',event_time TEXT NOT NULL DEFAULT '',post_id INTEGER,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,submitted TEXT);
        CREATE TABLE IF NOT EXISTS news_states (user_id INTEGER PRIMARY KEY,news_id INTEGER NOT NULL,step TEXT NOT NULL);
        ''')
        db.commit()

    def matches(self,source,text):
        folded=text.casefold()
        return all(not json.loads(source[key]) or any(value in folded for value in json.loads(source[key])) for key in ('topics','places'))

    def duplicate(self,target_id,text):
        value=normalized(text)
        if not value:return None
        rows=self.db.execute("SELECT f.post_id,f.normalized FROM news_fingerprints f JOIN posts p ON p.id=f.post_id WHERE f.target_id=? AND f.created>=datetime('now','-20 minutes') AND p.status NOT IN ('archived','failed') ORDER BY f.created DESC LIMIT 100",(target_id,)).fetchall()
        return next((row['post_id'] for row in rows if similar(value,row['normalized'])),None)

    def remember(self,post_id,target_id,text):
        if not normalized(text):return
        with self.db:self.db.execute('INSERT INTO news_fingerprints(post_id,target_id,normalized) VALUES(?,?,?) ON CONFLICT(post_id) DO UPDATE SET normalized=excluded.normalized,created=CURRENT_TIMESTAMP',(post_id,target_id,normalized(text)))


class EditorialControls:
    def __init__(self,app):self.app=app

    async def action(self,callback):
        uid=callback.from_user.id
        if callback.message.chat.type!='private' or self.app.get_role(uid) not in ('owner','admin'):
            await callback.answer('⛔ Недостатньо прав.',show_alert=True);return
        parts=callback.data.split(':')
        if len(parts)!=3 or parts[1]!='pause' or parts[2] not in ('0','1'):
            await callback.answer('Некоректна дія.');return
        pause=parts[2]=='1';store=self.app.store
        self.app.store.cancel(uid)
        with store.db:
            old=store.setting('publishing_paused','0')=='1'
            store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('publishing_paused','1' if pause else '0'))
            if old and not pause:
                # Never release a burst of stale news after a global pause.
                store.db.execute("UPDATE posts SET status='review',approved_by=NULL,due=NULL,auto_source=0,revision=revision+1,error='Повернуто на погодження після загальної паузи.' WHERE status='queued' OR (status='scheduled' AND due<=CURRENT_TIMESTAMP)")
        self.app.publications.wake.set();self.app.bridge.wake.set()
        await callback.answer()
        text=('🛑 Усі публікації призупинено. Нові звернення, пропозиції новин і замовлення продовжують працювати. Уже розпочатий запит до Telegram може завершитися.' if pause else
              '▶️ Публікації відновлено. Дописи, що накопичилися або пропустили свій час під час паузи, повернуто на погодження. Майбутній розклад збережено.')
        await callback.message.answer(text,reply_markup=self.app.main_menu(self.app.get_role(uid),uid))

    async def source_card(self,message,source_id):
        store=self.app.store;source=store.one('SELECT * FROM channel_sources WHERE chat_id=?',(source_id,))
        if not source:raise ValueError('Джерело не знайдено.')
        health=store.one('SELECT * FROM source_health WHERE chat_id=?',(source_id,))
        health_note='\nСтан: ще не перевірено.'
        if health:
            names={'ok':'✅ Доступ є','error':'⚠️ Потрібна увага','paused':'⏸ Вимкнено','unknown':'Очікується перевірка'}
            health_note='\nСтан: '+names.get(health['state'],'Очікується перевірка')
            if health['detail']:health_note+='\n'+health['detail']
            if health['checked']:health_note+='\nПеревірено: '+self.app.publications.local_time(health['checked'])+' · Київ'
            if health['last_message']:health_note+='\nОстанній допис: '+self.app.publications.local_time(health['last_message'])+' · Київ'
        rows=[[(('🔎 Спершу погоджувати' if source['mode']=='instant' else '⚡ Публікувати одразу'),f'bridge:mode:{source_id}:{"review" if source["mode"]=="instant" else "instant"}')],
              [('🏷 Теми джерела',f'bridge:topics:{source_id}'),('📍 Населені пункти',f'bridge:places:{source_id}')],
              [('⬅️ Канали','menu:channels')]]
        rows.insert(1,[(('🚨 Довіра станам: так' if source['alarm_trusted'] else '🚨 Довіряти станам тривоги'),f'bridge:alarmtrust:{source_id}:{0 if source["alarm_trusted"] else 1}')])
        await message.answer(f"📥 <b>{escape(source['title'])}</b>\n\nРежим: {'одразу' if source['mode']=='instant' else 'погодження адміністратором'}\n"
            f"Теми: {escape(', '.join(json.loads(source['topics'])) or 'усі')}\n"
            f"Населені пункти: {escape(', '.join(json.loads(source['places'])) or 'усі')}{escape(health_note)}\n\n"
            'У кожному фільтрі достатньо збігу однієї фрази. Якщо задано обидва — потрібен збіг і теми, і місця. '
            'Перевіряється текст допису або підпис до медіа; текст на фото автоматично не розпізнається. '
            'Схожі новини за останні 20 хвилин передаються на погодження, а не відкидаються.',
            parse_mode='HTML',reply_markup=self.app.keyboard(rows,message.from_user.id))

    async def source_action(self,callback):
        uid=callback.from_user.id;store=self.app.store
        if callback.message.chat.type!='private' or self.app.get_role(uid) not in ('owner','admin'):
            await callback.answer('⛔ Недостатньо прав.',show_alert=True);return
        parts=callback.data.split(':')
        try:
            sid=int(parts[2]);source=store.one('SELECT * FROM channel_sources WHERE chat_id=?',(sid,))
            if not source:raise ValueError('Джерело не знайдено.')
            self.app.store.cancel(uid);await callback.answer()
            message=self.app.publications.actor_message(callback)
            if parts[1]=='sourcecard':await self.source_card(message,sid)
            elif parts[1]=='alarmtrust' and len(parts)==4 and parts[3] in ('0','1'):
                with store.db:
                    store.db.execute('UPDATE channel_sources SET alarm_trusted=? WHERE chat_id=?',(int(parts[3]),sid))
                    store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,due=NULL,revision=revision+1 WHERE auto_source=1 AND status IN ('queued','scheduled') AND id IN (SELECT post_id FROM channel_deliveries WHERE source_id=?)",(sid,))
                await self.source_card(message,sid)
            elif parts[1]=='mode' and len(parts)==4 and parts[3] in ('instant','review'):
                with store.db:
                    store.db.execute('UPDATE channel_sources SET mode=? WHERE chat_id=?',(parts[3],sid))
                    if parts[3]=='review':store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,revision=revision+1 WHERE auto_source=1 AND status IN ('queued','scheduled') AND id IN (SELECT post_id FROM channel_deliveries WHERE source_id=?)",(sid,))
                await self.source_card(message,sid)
            elif parts[1] in ('topics','places'):
                with store.db:store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)',(uid,f'filter_{parts[1]}:{sid}'))
                await message.answer('Вкажіть слова або фрази через кому. Наприклад: '+('тривога, відбій, електрика' if parts[1]=='topics' else 'Суми, Охтирка')+'\n«-» — прибрати фільтр.',reply_markup=self.app.keyboard([[('⬅️ Джерело',f'bridge:sourcecard:{sid}')]],uid))
            else:raise ValueError('Невідома дія.')
        except (ValueError,IndexError) as exc:await callback.message.answer(str(exc))

    async def filter_input(self,message,mode):
        uid=message.from_user.id
        if self.app.get_role(uid) not in ('owner','admin'):
            self.app.store.cancel(uid);await message.answer('⛔ Недостатньо прав.');return True
        try:
            field,sid=mode.removeprefix('filter_').split(':');sid=int(sid)
            if field not in ('topics','places'):raise ValueError('Невідомий фільтр.')
            values=terms(message.text or '')
            with self.app.store.db:self.app.store.db.execute(f'UPDATE channel_sources SET {field}=? WHERE chat_id=?',(json.dumps(values,ensure_ascii=False),sid))
            self.app.store.cancel(uid);await self.source_card(message,sid)
        except ValueError as exc:await message.answer(str(exc))
        return True
