"""Read selected joined channels through a separate Telegram user session.

No messages, reactions, joins or read acknowledgements are sent by this module.
Publication is performed exclusively by the existing bot pipeline.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import hashlib
import logging
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parent
MEDIA_PREFIX = 'reader:'


def media_path(value, root=ROOT):
    """Only resolve files belonging to the managed reader media directory."""
    if not value.startswith(MEDIA_PREFIX):
        raise ValueError('Некоректне вкладення джерела.')
    name = value[len(MEDIA_PREFIX):]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
        raise ValueError('Некоректне ім’я вкладення джерела.')
    folder = (Path(root) / 'reader_media').resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise ValueError('Вкладення джерела відсутнє на хості. Перевірте папку reader_media.')
    return path


def input_file(value):
    if value.startswith(MEDIA_PREFIX):
        from aiogram.types import FSInputFile
        return FSInputFile(media_path(value))
    return value


class AccountReader:
    def __init__(self, app, root=ROOT):
        self.app = app
        self.root = Path(root)
        self.client = None
        self.status = 'Не підключено. Запустіть Connect-Reader.cmd на хості.'
        self.wake = asyncio.Event()
        self.access_checked = {}

    @property
    def store(self):
        return self.app.store

    async def resolve(self, reference):
        from telethon import functions, types, utils
        client = self.client
        if not client or not client.is_connected():
            raise ValueError('Акаунт ще не підключений. Запустіть Connect-Reader.cmd на хості й перевірте стан у розділі каналів.')
        entity = await client.get_entity(reference)
        if not isinstance(entity, types.Channel) or not entity.broadcast:
            raise ValueError('Потрібен саме канал Telegram, а не група чи особистий чат.')
        if getattr(entity, 'noforwards', False):
            raise ValueError('Канал забороняє копіювання дописів.')
        # Require an existing subscription. Never auto-join on the user's behalf.
        await client(functions.channels.GetParticipantRequest(entity, 'me'))
        latest = await client.get_messages(entity, limit=1)
        return utils.get_peer_id(entity), entity.title, latest[0].id if latest else 0

    def add_source(self, chat_id, title, cursor, username=None):
        if str(chat_id) == self.store.setting('publish_target'):
            raise ValueError('Джерело та канал публікації мають бути різними.')
        existing = self.store.one('SELECT transport FROM channel_sources WHERE chat_id=?', (chat_id,))
        if existing:
            raise ValueError('Цей канал уже доданий. Спершу видаліть його зі списку, якщо потрібно змінити спосіб читання.')
        with self.store.db:
            self.store.db.execute("INSERT INTO channel_sources(chat_id,title,mode,transport,username) VALUES(?,?,'review','reader',?)", (chat_id, title, username))
            self.store.db.execute('INSERT OR REPLACE INTO reader_offsets(chat_id,last_id) VALUES(?,?)', (chat_id, cursor))
        self.wake.set()

    def active_source(self, chat_id):
        if self.store.setting('reader_enabled', '1') != '1' or self.store.setting('autopublish', '0') != '1':
            return None
        target = self.store.setting('publish_target')
        if not target or str(chat_id) == target:
            return None
        return self.store.one("SELECT * FROM channel_sources WHERE chat_id=? AND transport='reader'", (chat_id,))

    async def ingest(self, message, source, entity):
        """Persist before advancing the cursor. Failed ingestion remains visible."""
        source_id = source['chat_id']
        target = self.store.setting('publish_target')
        if not self.active_source(source_id) or not target:
            return False
        if self.store.one('SELECT id FROM channel_deliveries WHERE source_id=? AND message_id=? AND target_id=?', (source_id, message.id, int(target))):
            return True
        body = message.message or ''
        payload = {'reader': True, 'text': body, 'date': message.date.isoformat(),
                   'id': message.id, 'source': source_id, 'media': None}
        error = None
        if getattr(entity, 'noforwards', False) or getattr(message, 'noforwards', False):
            error = 'Джерело забороняє копіювання.'
        elif getattr(message, 'action', None):
            error = 'Службовий допис не публікується.'
        elif message.media and not getattr(message, 'web_preview', None):
            if message.photo:
                kind, suffix = 'photo', '.jpg'
            elif message.gif:
                kind, suffix = 'animation', '.mp4'
            elif message.video:
                kind, suffix = 'video', '.mp4'
            elif message.voice:
                kind, suffix = 'voice', '.ogg'
            elif message.audio:
                kind, suffix = 'audio', '.mp3'
            elif message.document and not message.sticker:
                kind, suffix = 'document', '.bin'
            else:
                kind, suffix = None, ''
            extension = getattr(getattr(message, 'file', None), 'ext', None)
            if kind != 'photo' and extension and re.fullmatch(r'\.[A-Za-z0-9]{1,10}',extension):
                suffix = extension
            size = getattr(getattr(message, 'file', None), 'size', None)
            limit = (10 if kind == 'photo' else 49) * 1024 * 1024
            folder = self.root / 'reader_media'
            folder.mkdir(exist_ok=True)
            if not kind:
                error = 'Цей тип вкладення поки не підтримується модулем читання.'
            elif size is None or size > limit:
                error = 'Вкладення завелике або його розмір невідомий. Потрібна ручна публікація.'
            elif sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) + size > 1024 * 1024 * 1024:
                error = 'Сховище вкладень джерел перевищує 1 ГБ. Потрібне обслуговування хоста.'
            else:
                name = f'{abs(source_id)}_{message.id}{suffix}'
                path = folder / name
                temporary = folder / (name + '.part')
                downloaded = await message.download_media(file=str(temporary))
                if not downloaded or not temporary.exists():
                    raise RuntimeError('Не вдалося завантажити вкладення джерела')
                if temporary.stat().st_size > limit:
                    temporary.unlink()
                    error = 'Вкладення перевищує ліміт публікації ботом.'
                else:
                    temporary.replace(path)
                    with path.open('rb') as stream:
                        fingerprint = hashlib.file_digest(stream,'sha256').hexdigest()
                    payload['media'] = {'kind': kind, 'file_id': MEDIA_PREFIX + name, 'fingerprint': fingerprint}
        if not error and not body and not payload['media']:
            error = 'Порожній або непідтримуваний допис.'
        # Recheck after a download, since an administrator may disable/remove a source.
        if not self.active_source(source_id) or target != self.store.setting('publish_target'):
            return False
        with self.store.db:
            payload['blocked'] = error
            self.store.db.execute('''INSERT OR IGNORE INTO channel_deliveries
                (source_id,message_id,target_id,payload,group_id,queued,status,error)
                VALUES(?,?,?,?,?,?,?,?)''', (source_id, message.id, int(target), json.dumps(payload, ensure_ascii=False),
                str(message.grouped_id) if message.grouped_id else None, time.time(), 'failed' if error else 'pending', error))
            self.store.db.execute('''INSERT INTO source_health(chat_id,last_message) VALUES(?,?)
                ON CONFLICT(chat_id) DO UPDATE SET last_message=excluded.last_message''', (source_id,message.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')))
        self.app.bridge.wake.set()
        return True

    async def scan_source(self, source):
        self.app.bridge.reader_busy.add(source['chat_id'])
        try:
            await self._scan_source(source)
        finally:
            self.app.bridge.reader_busy.discard(source['chat_id'])

    async def _scan_source(self, source):
        from telethon import types, functions, utils
        source_id = source['chat_id']
        if not self.active_source(source_id):
            return
        entity = await self.client.get_entity(types.PeerChannel(utils.resolve_id(source_id)[0]))
        if time.monotonic()-self.access_checked.get(source_id, float('-inf')) > 60:
            await self.client(functions.channels.GetParticipantRequest(entity, 'me'))
            self.access_checked[source_id] = time.monotonic()
        offset = self.store.one('SELECT last_id FROM reader_offsets WHERE chat_id=?', (source_id,))
        if not offset:
            latest = await self.client.get_messages(entity, limit=1)
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO reader_offsets(chat_id,last_id) VALUES(?,?)', (source_id, latest[0].id if latest else 0))
            return
        # Reverse fetches oldest unseen first, so an offline backlog cannot skip messages.
        async for message in self.client.iter_messages(entity, min_id=offset[0], reverse=True, limit=100):
            if not await self.ingest(message, source, entity):
                break
            with self.store.db:
                self.store.db.execute('UPDATE reader_offsets SET last_id=max(last_id,?) WHERE chat_id=?', (message.id, source_id))

    async def connected(self, client):
        from telethon import events, errors
        await client.connect()
        if not await client.is_user_authorized():
            self.status = 'Потрібен вхід. Запустіть Connect-Reader.cmd на хості.'
            return
        me = await client.get_me()
        if me.bot:
            raise ValueError('Для читання потрібен користувацький Telegram-акаунт.')
        # Cache joined channel IDs, allowing private channels to be added by numeric ID.
        await client.get_dialogs()
        self.client = client
        self.access_checked.clear()
        self.status = 'Акаунт підключено. Читаються лише додані джерела.'
        async def updated(event):
            if str(event.chat_id)==self.store.setting('publish_target') and self.store.alerts.enabled:
                self.store.alerts.record_target(event.chat_id,event.message.id,event.message.message or '',event.message.date)
                self.app.publications.wake.set()
            if self.active_source(event.chat_id):
                self.wake.set()
        client.add_event_handler(updated, events.NewMessage())
        async def edited(event):
            if str(event.chat_id)==self.store.setting('publish_target') and self.store.alerts.enabled:
                self.store.alerts.record_target(event.chat_id,event.message.id,event.message.message or '',event.message.edit_date or event.message.date,edited=True)
                self.app.publications.wake.set()
        client.add_event_handler(edited,events.MessageEdited())
        try:
            while client.is_connected():
                self.wake.clear()
                if self.store.setting('reader_enabled', '1') == '1':
                    sources = self.store.db.execute("SELECT * FROM channel_sources WHERE transport='reader'").fetchall()
                    for source in sources:
                        try:
                            await self.scan_source(source)
                            with self.store.db:
                                self.store.db.execute("UPDATE reader_offsets SET error=NULL,checked=CURRENT_TIMESTAMP WHERE chat_id=?", (source['chat_id'],))
                        except errors.FloodWaitError as exc:
                            self.status = f'Telegram просить зачекати {exc.seconds} с.'
                            await asyncio.sleep(max(1, exc.seconds))
                        except Exception as exc:
                            # Do not log private session credentials or source contents.
                            with self.store.db:
                                self.store.db.execute('UPDATE reader_offsets SET error=?,checked=CURRENT_TIMESTAMP WHERE chat_id=?', ('Помилка доступу або завантаження: '+type(exc).__name__, source['chat_id']))
                            logging.warning('Reader source %s: %s', source['chat_id'], type(exc).__name__)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        finally:
            client.remove_event_handler(updated)
            client.remove_event_handler(edited)
            self.client = None

    async def worker(self):
        from host_runtime import single_instance, AlreadyRunning
        while True:
            client = None
            try:
                folder = self.root / '.reader'
                config = folder / 'credentials.json'
                if config.exists():
                    from telethon import TelegramClient
                    credentials = json.loads(config.read_text(encoding='utf-8'))
                    with single_instance(self.root / '.runtime' / 'reader.lock'):
                        client = TelegramClient(str(folder / 'account'), int(credentials['api_id']), credentials['api_hash'],
                                                auto_reconnect=True, connection_retries=3, flood_sleep_threshold=0)
                        await self.connected(client)
                        await client.disconnect()
                        client = None
                else:
                    self.status = 'Не підключено. Запустіть Connect-Reader.cmd на хості.'
            except asyncio.CancelledError:
                raise
            except AlreadyRunning:
                self.status = 'Триває локальне підключення акаунта. Зачекайте.'
            except Exception as exc:
                self.status = 'Немає з’єднання з акаунтом: '+type(exc).__name__+'. Повторна спроба автоматично.'
                logging.warning('Reader connection: %s', type(exc).__name__)
            finally:
                self.client = None
                if client:
                    with suppress(Exception):
                        await client.disconnect()
            await asyncio.sleep(15)
