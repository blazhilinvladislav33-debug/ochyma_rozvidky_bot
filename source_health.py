"""Source availability monitoring with durable transition notifications."""
import asyncio
from datetime import datetime, timezone
import logging
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError


class SourceMonitor:
    def __init__(self, app):
        self.app = app

    @property
    def store(self):
        return self.app.store

    async def record(self, source, state, detail=''):
        sid = source['chat_id']
        if not self.store.one('SELECT chat_id FROM channel_sources WHERE chat_id=?',(sid,)):
            return
        old = self.store.one('SELECT state FROM source_health WHERE chat_id=?', (sid,))
        with self.store.db:
            self.store.db.execute('''INSERT INTO source_health(chat_id,state,detail,checked)
                VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id) DO UPDATE SET
                state=excluded.state,detail=excluded.detail,checked=excluded.checked''', (sid, state, detail))
        changed = old is None or old['state'] != state
        alert = changed and (state == 'error' or (state == 'ok' and old is not None and old['state'] == 'error'))
        if not alert:
            return
        text = (f'⚠️ Джерело «{source["title"]}» недоступне.\n{detail}\nПеревірте розділ «Канали».' if state == 'error' else
                f'✅ Читання джерела «{source["title"]}» відновлено.')
        with self.store.db:
            self.store.db.execute('DELETE FROM source_alerts WHERE chat_id=?', (sid,))
            for uid in self.app.ADMIN_IDS:
                if self.app.get_role(uid) in ('owner','admin'):
                    self.store.db.execute('INSERT INTO source_alerts(chat_id,admin_id,text) VALUES(?,?,?)', (sid, uid, text))

    async def flush(self):
        for row in self.store.db.execute('SELECT * FROM source_alerts').fetchall():
            if self.app.get_role(row['admin_id']) not in ('owner','admin'):
                with self.store.db:self.store.db.execute('DELETE FROM source_alerts WHERE chat_id=? AND admin_id=?', (row['chat_id'],row['admin_id']))
                continue
            try:
                await self.app.bot.send_message(row['admin_id'],row['text'],reply_markup=self.app.keyboard([[('📡 Перевірити канали','menu:channels')]],row['admin_id']))
            except TelegramAPIError:
                continue
            with self.store.db:
                self.store.db.execute('DELETE FROM source_alerts WHERE chat_id=? AND admin_id=?', (row['chat_id'],row['admin_id']))

    async def check_one(self, source):
        if self.store.setting('autopublish','0') != '1' or not self.store.setting('publish_target'):
            await self.record(source, 'paused', 'Автопублікацію вимкнено адміністратором.')
            return
        if source['transport'] == 'reader':
            if self.store.setting('reader_enabled','1') != '1':
                await self.record(source,'paused','Читання акаунтом вимкнено адміністратором.')
                return
            reader = self.app.reader
            offset = self.store.one('SELECT * FROM reader_offsets WHERE chat_id=?',(source['chat_id'],))
            if reader.client is None or not reader.client.is_connected():
                await self.record(source,'error',reader.status)
            elif offset and offset['error']:
                await self.record(source,'error',offset['error'])
            elif offset and offset['checked']:
                checked=datetime.strptime(offset['checked'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc)-checked).total_seconds()>180:
                    await self.record(source,'error','Читання не підтверджувало роботу понад 3 хвилини.')
                else:await self.record(source,'ok','З’єднання та доступ перевірено.')
            return
        try:
            me=await self.app.bot.get_me()
            member=await self.app.bot.get_chat_member(source['chat_id'],me.id)
            if member.status not in ('administrator','creator','member'):
                await self.record(source,'error','Бот втратив доступ до каналу-джерела.')
            else:await self.record(source,'ok','Telegram підтвердив доступ бота.')
        except TelegramNetworkError:
            return
        except TelegramAPIError:
            await self.record(source,'error','Telegram не підтвердив доступ бота до каналу.')

    async def worker(self):
        await asyncio.sleep(30)
        while True:
            try:
                for source in self.store.db.execute('SELECT * FROM channel_sources').fetchall():
                    await self.check_one(source)
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Помилка перевірки стану джерел')
            await asyncio.sleep(60)
