"""Bot UI and target-channel observation for territorial alert rules."""
from datetime import datetime, timezone
from html import escape
import json
import logging
from alert_engine import DEFAULT_KEYS, DEFAULT_TEMPLATES, DEFAULT_PLACES, parse_places, validate_template, stamp, now_utc


class AlertControls:
    def __init__(self,app):self.app=app

    @property
    def repo(self):return self.app.store.alerts

    def admin(self,uid):return self.app.get_role(uid) in ('owner','admin')

    async def dashboard(self,message):
        store=self.app.store;target=store.setting('publish_target')
        rows=[[('⏸ Вимкнути правила' if self.repo.enabled else '▶️ Увімкнути правила',f'alert:enable:{0 if self.repo.enabled else 1}')],
              [('📍 Території та назви','alert:input:places'),('🔑 Ключові слова','alert:keys')],
              [('✏️ Стиль повідомлень','alert:styles'),('🧪 Перевірити текст','alert:input:test')],
              [('♻️ Інтервал повторів','alert:input:duplicates')],
              [('⬅️ Канали','menu:channels')]]
        states=store.db.execute('SELECT * FROM alert_states WHERE target_id=? ORDER BY area',(int(target) if target else 0,)).fetchall()
        lines=[]
        for state in states:
            value=state['started'] if state['phase']=='alarm' else state['cleared']
            time=self.app.publications.local_time(datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S')) if value else '—'
            lines.append((('🟡 ' if state['level']=='yellow' else '🔴 ') if state['phase']=='alarm' else '🟢 ')+escape(state['area'])+' · '+time+' Київ')
        trusted=store.one('SELECT count(*) FROM channel_sources WHERE alarm_trusted=1')[0]
        await message.answer('🚨 <b>Правила повідомлень про загрози</b>\n\n'+
            f'Стан: {"увімкнено" if self.repo.enabled else "вимкнено"}\nДовірених джерел стану: {trusted}\n\n'+
            ('\n'.join(lines) or 'Стан територій ще невідомий. Перший стан має надійти з довіреного джерела або ручного допису вашого каналу.')+
            '\n\nТермінові — одразу, якщо джерело працює в режимі «одразу». Звичайні — після 30 хв від початку тривоги; після відбою — через 5 хв. '
            'Без території новина чекає найпізніший відомий таймер. Новини старші 15 хв залишаються на погодженні. '
            'Ручні дописи вашого каналу враховуються після ввімкнення правил. Для перевірки недавньої історії підпишіть акаунт читання також на свій канал. '
            'Стани міста й району окремі. Неоднозначні повідомлення не перетворюються на підтверджену загрозу.',
            parse_mode='HTML',reply_markup=self.app.keyboard(rows,message.from_user.id))

    async def action(self,callback):
        uid=callback.from_user.id
        if callback.message.chat.type!='private' or not self.admin(uid):
            await callback.answer('⛔ Недостатньо прав.',show_alert=True);return
        store=self.app.store;parts=callback.data.split(':')
        message=self.app.publications.actor_message(callback)
        store.cancel(uid);await callback.answer()
        try:
            if len(parts)<2:raise ValueError('Некоректна дія.')
            if parts[1]=='enable' and len(parts)==3 and parts[2] in ('0','1'):
                if parts[2]=='1':
                    if not store.setting('publish_target'):raise ValueError('Спершу оберіть свій канал у розділі «Канали».')
                    if not store.one('SELECT chat_id FROM channel_sources WHERE alarm_trusted=1 LIMIT 1'):
                        raise ValueError('У картці потрібного джерела ввімкніть «Довіряти станам тривоги».')
                store.set_setting('alerts_enabled',parts[2])
                # Toggling policy never releases old formatted posts under new rules.
                with store.db:
                    store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,due=NULL,revision=revision+1,error='Правила сповіщень змінено. Перевірте допис.' WHERE auto_source=1 AND status IN ('queued','scheduled')")
                self.app.publications.wake.set()
                await self.dashboard(message)
            elif parts[1]=='home':await self.dashboard(message)
            elif parts[1]=='keys':
                rows=[[(f'🔑 {name}',f'alert:input:keys_{key}')] for key,name in
                      [('alarm','Тривога'),('clear','Відбій'),('kab','КАБ'),('shahed','Шахеди'),('fpv','ФПВ'),('fiber','Оптика'),('reactive','Реактивні'),('uncertain','Загальні слова')]]
                rows.append([('⬅️ Правила','alert:home')])
                await message.answer('Оберіть категорію, щоб змінити її варіанти написання. Регістр не враховується; збіги лише в межах слів.',reply_markup=self.app.keyboard(rows,uid))
            elif parts[1]=='styles':
                rows=[[(name,f'alert:input:style_{key}')] for key,name in [('alarm_yellow','🟡 Дронова тривога'),('alarm_red','🔴 Ракетна тривога'),('alarm','🚨 Тривога без типу'),('clear','🟢 Стиль відбою'),('direction','⚠️ Загроза в напрямку'),('threat','⚠️ Загальна загроза')]]
                rows.append([('⬅️ Правила','alert:home')])
                await message.answer('У шаблоні {places} означає території, {threat} — тип загрози. Території виділяються жирним.',reply_markup=self.app.keyboard(rows,uid))
            elif parts[1]=='input' and len(parts)==3:
                kind=parts[2];prompts={
                    'places':'Надішліть повний список, кожен рядок:\nНазва | варіанти через кому або - | територія таймера або -\n\n'+
                        '\n'.join(x['name']+' | '+(', '.join(x['aliases']) or '-')+' | '+(x['timer'] or '-') for x in self.repo.places())+
                        '\n\nТериторія таймера має бути окремим рядком у списку. Прив’язку села до району вкажіть самостійно після перевірки.',
                    'test':'Надішліть приклад повідомлення. Бот покаже результат без публікації й без зміни стану тривоги.',
                    'duplicates':'Уведіть інтервал перевірки повторів від 1 до 180 хвилин. Зараз: '+self.repo.setting('alerts_duplicate_minutes','20'),
                }
                if kind.startswith('keys_') and kind[5:] in DEFAULT_KEYS:
                    prompts[kind]='Надішліть варіанти через кому, до 30 фраз. «-» вимикає категорію.\nЗараз: '+', '.join(self.repo.config('keys',DEFAULT_KEYS)[kind[5:]])
                elif kind.startswith('style_') and kind[6:] in DEFAULT_TEMPLATES:
                    prompts[kind]='Надішліть шаблон до 700 символів. Доступні {places} та {threat}; {places} обов’язкове.\nЗараз:\n'+self.repo.config('templates',DEFAULT_TEMPLATES)[kind[6:]]
                if kind not in prompts:raise ValueError('Невідоме налаштування.')
                with store.db:store.db.execute('INSERT OR REPLACE INTO admin_inputs VALUES(?,?)',(uid,'alerts_'+kind))
                await message.answer(prompts[kind],reply_markup=self.app.keyboard([[('⬅️ Правила','alert:home')]],uid))
            else:raise ValueError('Некоректна дія.')
        except ValueError as exc:await message.answer(str(exc))

    async def input(self,message,mode):
        uid=message.from_user.id;store=self.app.store
        if not self.admin(uid):store.cancel(uid);return True
        kind=mode.removeprefix('alerts_');text=(message.text or '').strip()
        try:
            if not text or len(text)>18000:raise ValueError('Надішліть текст до 18000 символів.')
            if kind=='test':
                event=self.repo.classify(text)
                result=self.repo.render(event)[0] if event['urgent'] else text
                note=event['reason'] or ('Для стану тривоги додатково потрібне довірене джерело.' if event['kind'] in ('alarm','clear') else 'Розпізнано термінову загрозу.' if event['urgent'] else 'Звичайна новина: діятимуть таймери території.')
                await message.answer('🧪 Результат перевірки\n\n'+result+'\n\n'+note+'\n\nНічого не опубліковано, стани не змінені.',reply_markup=self.app.keyboard([[('⬅️ Правила','alert:home')]],uid))
                store.cancel(uid);return True
            if kind=='places':
                value=parse_places(text)
                old={x['name'] for x in self.repo.places()};new={x['name'] for x in value}
                if old-new and store.one("SELECT 1 FROM alert_states WHERE phase='alarm' LIMIT 1"):
                    raise ValueError('Під час відомої активної тривоги не можна прибирати території. Додавати й змінювати варіанти назв можна.')
                store.set_setting('alerts_places',json.dumps(value,ensure_ascii=False))
            elif kind.startswith('keys_') and kind[5:] in DEFAULT_KEYS:
                values=[] if text=='-' else list(dict.fromkeys(x.strip() for x in text.split(',') if x.strip()))
                if len(values)>30 or any(len(x)>80 for x in values):raise ValueError('До 30 фраз, кожна до 80 символів.')
                config=self.repo.config('keys',DEFAULT_KEYS);config[kind[5:]]=values
                store.set_setting('alerts_keys',json.dumps(config,ensure_ascii=False))
            elif kind.startswith('style_') and kind[6:] in DEFAULT_TEMPLATES:
                config=self.repo.config('templates',DEFAULT_TEMPLATES);config[kind[6:]]=validate_template(text)
                store.set_setting('alerts_templates',json.dumps(config,ensure_ascii=False))
            elif kind=='duplicates':
                if not text.isascii() or not text.isdigit() or not 1<=int(text)<=180:raise ValueError('Потрібне ціле число від 1 до 180.')
                store.set_setting('alerts_duplicate_minutes',str(int(text)))
            else:raise ValueError('Невідомий режим.')
            with store.db:store.db.execute("UPDATE posts SET status='review',auto_source=0,approved_by=NULL,due=NULL,revision=revision+1,error='Налаштування правил змінено.' WHERE auto_source=1 AND status IN ('queued','scheduled')")
            store.cancel(uid);await message.answer('✅ Збережено. Уже підготовлені автоматичні дописи повернуто на погодження.')
            await self.dashboard(message)
        except ValueError as exc:await message.answer(str(exc))
        return True

    async def observe(self,message):
        store=self.app.store
        if not self.repo.enabled or str(message.chat.id)!=store.setting('publish_target'):return
        text=message.text or message.caption or ''
        self.repo.record_target(message.chat.id,message.message_id,text,getattr(message,'edit_date',None) or message.date,
                                edited=bool(getattr(message,'edit_date',None)))
        self.app.publications.wake.set()

    async def refresh_target(self,target_id):
        if not self.repo.enabled or str(target_id)!=self.app.store.setting('publish_target'):return
        reader=getattr(self.app,'reader',None);client=reader.client if reader else None
        if not client or not client.is_connected():return
        try:
            messages=await client.get_messages(target_id,limit=30)
            for message in reversed(messages):
                self.repo.record_target(target_id,message.id,message.message or '',message.edit_date or message.date,edited=bool(message.edit_date))
        except Exception as exc:
            logging.warning('Target history check: %s',type(exc).__name__)
            raise ValueError('Не вдалося перевірити недавні ручні дописи каналу через акаунт. Перевірте доступ і погодьте допис вручну.') from None

    def apply_gate(self,post):
        decision=self.repo.before_send(post)
        if not decision:return False
        action,value=decision;store=self.app.store
        if action=='wait':
            origin=json.loads(post['origin']);origin['alert_wait']=True
            due=datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S')
            with store.db:store.db.execute("UPDATE posts SET status='scheduled',due=?,origin=?,error='Очікування таймера тривоги або відбою.' WHERE id=? AND status='sending'",(due,json.dumps(origin,ensure_ascii=False),post['id']))
        else:
            state='archived' if action=='duplicate' else 'review'
            with store.db:
                store.db.execute('UPDATE posts SET status=?,auto_source=0,approved_by=NULL,due=NULL,error=?,revision=revision+1 WHERE id=? AND status=\'sending\'',(state,value,post['id']))
                store.db.execute('UPDATE channel_deliveries SET status=?,error=? WHERE post_id=?',('duplicate' if action=='duplicate' else 'review',value,post['id']))
        return True
