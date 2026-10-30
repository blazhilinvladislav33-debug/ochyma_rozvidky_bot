import sqlite3
import json
from pathlib import Path


class TicketStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
            username TEXT, status TEXT NOT NULL DEFAULT 'new');
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, direction TEXT NOT NULL,
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS reads (
            admin_id INTEGER NOT NULL, ticket_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
            PRIMARY KEY(admin_id, ticket_id));
        CREATE TABLE IF NOT EXISTS read_messages (
            admin_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY(admin_id, message_id));
        CREATE TABLE IF NOT EXISTS keyboard_choices (
            user_id INTEGER NOT NULL, label TEXT NOT NULL, action TEXT NOT NULL,
            PRIMARY KEY(user_id, label));
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT,
            blocked INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_inputs (admin_id INTEGER PRIMARY KEY, mode TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_sources (
            chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
            added TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS channel_deliveries (
            id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_id,message_id,target_id));
        CREATE TABLE IF NOT EXISTS sessions (
            admin_id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS ticket_user ON tickets(user_id, status);
        CREATE INDEX IF NOT EXISTS message_ticket ON messages(ticket_id, id);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}
        for column in ('body', 'content_type', 'media'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE messages ADD COLUMN {column} TEXT')
        delivery_columns = {row[1] for row in self.db.execute('PRAGMA table_info(channel_deliveries)')}
        if 'group_id' not in delivery_columns:
            self.db.execute('ALTER TABLE channel_deliveries ADD COLUMN group_id TEXT')
        if 'queued' not in delivery_columns:
            self.db.execute('ALTER TABLE channel_deliveries ADD COLUMN queued REAL NOT NULL DEFAULT 0')
        source_columns = {row[1] for row in self.db.execute('PRAGMA table_info(channel_sources)')}
        if 'transport' not in source_columns:
            self.db.execute("ALTER TABLE channel_sources ADD COLUMN transport TEXT NOT NULL DEFAULT 'bot'")
        if 'username' not in source_columns:
            self.db.execute('ALTER TABLE channel_sources ADD COLUMN username TEXT')
        self.db.execute('''CREATE TABLE IF NOT EXISTS reader_offsets (
            chat_id INTEGER PRIMARY KEY,last_id INTEGER NOT NULL DEFAULT 0,
            error TEXT,checked TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS source_health (
            chat_id INTEGER PRIMARY KEY,state TEXT NOT NULL DEFAULT 'unknown',
            detail TEXT NOT NULL DEFAULT '',checked TEXT,last_message TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS source_alerts (
            chat_id INTEGER NOT NULL,admin_id INTEGER NOT NULL,text TEXT NOT NULL,
            PRIMARY KEY(chat_id,admin_id))''')
        self.db.execute("INSERT OR IGNORE INTO users(user_id,name,username) SELECT user_id,name,username FROM tickets WHERE id IN (SELECT max(id) FROM tickets GROUP BY user_id)")
        self.db.create_function('casefold', 1, lambda value: (value or '').casefold())
        self.db.commit()
        from bookings_store import BookingRepository
        self.bookings = BookingRepository(self.db)
        from posts_store import PostRepository
        self.posts = PostRepository(self.db)
        from editorial import EditorialStore
        self.editorial = EditorialStore(self.db)
        from alert_engine import AlertRepository
        self.alerts = AlertRepository(self.db)

    def touch_user(self, user_id, name, username):
        with self.db:
            self.db.execute("INSERT INTO users(user_id,name,username) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,last_seen=CURRENT_TIMESTAMP", (user_id,name,username))

    def setting(self, key, default=''):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row[0] if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def receive(self, user_id, name, username, chat_id, message_id, body=None, content_type=None, ticket_id=None, media=None):
        with self.db:
            old = self.one('SELECT ticket_id FROM messages WHERE chat_id=? AND message_id=? AND direction=\'in\'', (chat_id, message_id))
            if old:
                return old[0], False
            if ticket_id is not None:
                ticket = self.one('SELECT id FROM tickets WHERE id=? AND user_id=?', (ticket_id,user_id))
                if not ticket:
                    raise ValueError('Звернення не знайдено.')
                self.db.execute("UPDATE tickets SET status='new' WHERE id=? AND status='closed'", (ticket_id,))
            else:
                ticket = self.one("SELECT id FROM tickets WHERE user_id=? AND status!='closed' AND NOT EXISTS (SELECT 1 FROM orders WHERE orders.ticket_id=tickets.id) ORDER BY id DESC LIMIT 1", (user_id,))
            if ticket:
                ticket_id = ticket[0]
                self.db.execute('UPDATE tickets SET name=?, username=? WHERE id=?', (name, username, ticket_id))
            else:
                ticket_id = self.db.execute('INSERT INTO tickets(user_id,name,username) VALUES(?,?,?)', (user_id, name, username)).lastrowid
            self.db.execute("UPDATE tickets SET status='new' WHERE id=? AND status='waiting'", (ticket_id,))
            self.db.execute("INSERT INTO messages(ticket_id,direction,chat_id,message_id,body,content_type,media) VALUES(?,'in',?,?,?,?,?)", (ticket_id, chat_id, message_id, body, content_type,json.dumps(media or [])))
        return ticket_id, True

    def statistics(self, start, end, admin_id, staff_ids=()):
        # Every event is stored in UTC. Ticket creation is its first incoming message.
        args = (start, end)
        period = "created>=? AND created<?"
        totals = dict(self.one(
            f"SELECT count(*) AS messages, coalesce(sum(direction='in'),0) AS incoming, "
            f"coalesce(sum(direction='out'),0) AS outgoing FROM messages WHERE {period}", args))
        totals['active_users'] = self.one(
            f"SELECT count(DISTINCT t.user_id) FROM messages m JOIN tickets t ON t.id=m.ticket_id "
            f"WHERE m.direction='in' AND m.{period}", args)[0]
        statuses = {key: 0 for key in STATUS}
        rows = self.db.execute(
            "SELECT t.status,count(*) AS n FROM tickets t JOIN "
            "(SELECT ticket_id,min(created) AS first_message FROM messages WHERE direction='in' GROUP BY ticket_id) m "
            "ON m.ticket_id=t.id WHERE m.first_message>=? AND m.first_message<? GROUP BY t.status", args).fetchall()
        statuses.update({row['status']: row['n'] for row in rows})
        totals['statuses'] = statuses
        totals['tickets'] = sum(statuses.values())
        totals['unread'] = self.unread(admin_id)
        totals['open_now'] = self.one("SELECT count(*) FROM tickets WHERE status!='closed'")[0]
        staff_ids = tuple(sorted(set(staff_ids)))
        exclude = 'user_id NOT IN (' + ','.join('?' for _ in staff_ids) + ')' if staff_ids else '1=1'
        totals['users'] = self.one(f'SELECT count(*) FROM users WHERE {exclude}', staff_ids)[0]
        totals['blocked'] = self.one(f'SELECT count(*) FROM users WHERE blocked=1 AND {exclude}', staff_ids)[0]
        totals['new_users'] = self.one(f'SELECT count(*) FROM users WHERE first_seen>=? AND first_seen<? AND {exclude}', (*args,*staff_ids))[0]
        response = self.one(
            "SELECT count(*) AS replied, avg((julianday(first_out)-julianday(first_in))*86400) AS seconds "
            "FROM (SELECT ticket_id,min(CASE WHEN direction='in' THEN created END) AS first_in, "
            "min(CASE WHEN direction='out' THEN created END) AS first_out FROM messages GROUP BY ticket_id) "
            "WHERE first_in>=? AND first_in<? AND first_out>=first_in", args)
        totals['replied'] = response['replied']
        totals['response_seconds'] = response['seconds']
        return totals

    def ticket(self, ticket_id):
        return self.one('SELECT * FROM tickets WHERE id=?', (ticket_id,))

    def unread(self, admin_id, ticket_id=None):
        sql = "SELECT count(*) FROM messages m LEFT JOIN read_messages r ON r.message_id=m.id AND r.admin_id=? WHERE m.direction='in' AND r.message_id IS NULL"
        args = [admin_id]
        if ticket_id is not None:
            sql += ' AND m.ticket_id=?'
            args.append(ticket_id)
        return self.one(sql, args)[0]

    def mark_read(self, admin_id, message_ids):
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO read_messages VALUES(?,?)', [(admin_id, item) for item in message_ids])

    def session(self, admin_id):
        return self.one('SELECT ticket_id FROM sessions WHERE admin_id=?', (admin_id,))

    def cancel(self, admin_id, keep_booking=False):
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE admin_id=?', (admin_id,))
            self.db.execute('DELETE FROM admin_inputs WHERE admin_id=?', (admin_id,))
            self.db.execute('DELETE FROM news_states WHERE user_id=?', (admin_id,))
            if not keep_booking:
                self.db.execute('DELETE FROM booking_states WHERE user_id=?', (admin_id,))

    def reply(self, ticket_id, chat_id, message_id, body=None, content_type=None, media=None):
        with self.db:
            self.db.execute("INSERT INTO messages(ticket_id,direction,chat_id,message_id,body,content_type,media) VALUES(?,'out',?,?,?,?,?)", (ticket_id, chat_id, message_id, body, content_type,json.dumps(media or [])))
            self.db.execute("UPDATE tickets SET status='waiting' WHERE id=? AND status!='closed'", (ticket_id,))


def message_media(message):
    """Persist Bot API file IDs for new messages; legacy message references stay valid."""
    photos=getattr(message,'photo',None)
    if photos:return [{'kind':'photo','file_id':photos[-1].file_id}]
    for kind in ('video','document','voice','audio','animation','sticker','video_note'):
        value=getattr(message,kind,None)
        if value:return [{'kind':kind,'file_id':value.file_id}]
    return []


STATUS = {'new': 'Нове', 'working': 'У роботі', 'waiting': 'Очікує відповіді користувача', 'closed': 'Закрите'}
