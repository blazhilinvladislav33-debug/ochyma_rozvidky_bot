import json
from datetime import datetime, timedelta, timezone


POST_STATUS = {
    'draft':'Чернетка', 'review':'На погодженні', 'scheduled':'Заплановано',
    'queued':'У черзі', 'sending':'Публікується', 'published':'Опубліковано',
    'failed':'Помилка', 'uncertain':'Потрібна перевірка каналу', 'archived':'Архів',
}
EDITABLE = ('draft','review','failed')


def utc_string(value=None):
    return (value or datetime.now(timezone.utc)).strftime('%Y-%m-%d %H:%M:%S')


class PostRepository:
    def __init__(self, db):
        self.db=db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY, author_id INTEGER NOT NULL,
            body_html TEXT NOT NULL DEFAULT '', body_text TEXT NOT NULL DEFAULT '',
            media TEXT NOT NULL DEFAULT '[]', buttons TEXT NOT NULL DEFAULT '[]',
            header TEXT NOT NULL DEFAULT '', footer TEXT NOT NULL DEFAULT '',
            target_id INTEGER, target_title TEXT NOT NULL DEFAULT '',
            silent INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 0,
            approved_by INTEGER, due TEXT, published TEXT, sent_ids TEXT NOT NULL DEFAULT '[]',
            error TEXT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS post_events (
            id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, actor_id INTEGER,
            action TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS post_queue ON posts(status,due,id);
        CREATE INDEX IF NOT EXISTS post_author ON posts(author_id,id);
        CREATE TABLE IF NOT EXISTS saved_templates (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
            payload TEXT NOT NULL, media TEXT NOT NULL DEFAULT '[]',
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(posts)')}
        if 'revision' not in {row[1] for row in self.db.execute('PRAGMA table_info(saved_templates)')}:
            self.db.execute('ALTER TABLE saved_templates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        if 'related_to' not in columns:
            self.db.execute('ALTER TABLE posts ADD COLUMN related_to INTEGER')
        self.db.commit()

    def get(self, post_id):
        return self.db.execute('SELECT * FROM posts WHERE id=?',(post_id,)).fetchone()

    def event(self, post_id,actor,action):
        self.db.execute('INSERT INTO post_events(post_id,actor_id,action) VALUES(?,?,?)',(post_id,actor,action))

    def create(self,author_id,target_id=None,target_title='',header='',footer=''):
        with self.db:
            post_id=self.db.execute('INSERT INTO posts(author_id,target_id,target_title,header,footer) VALUES(?,?,?,?,?)',(author_id,target_id,target_title,header,footer)).lastrowid
            self.event(post_id,author_id,'created')
        return self.get(post_id)

    def edit(self,post_id,actor,**fields):
        allowed={'body_html','body_text','media','buttons','header','footer','target_id','target_title','silent'}
        if not fields or not set(fields)<=allowed:
            raise ValueError('Некоректні поля допису.')
        current=self.get(post_id)
        if current and current['related_to'] and 'target_id' in fields and fields['target_id'] != current['target_id']:
            raise ValueError('Уточнення публікується в тому самому каналі, що й оригінал.')
        if current and current['edit_of'] and set(fields)&{'media','target_id','target_title','silent'}:
            raise ValueError('У виправленні можна змінювати лише текст, підписи та кнопки.')
        with self.db:
            changed=self.db.execute('UPDATE posts SET '+','.join(f'{key}=?' for key in fields)+",status='draft',approved_by=NULL,due=NULL,error=NULL,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND status IN ('draft','review','failed')",(*fields.values(),post_id)).rowcount
            if not changed:
                raise ValueError('Цей допис уже не можна редагувати. Оновіть його картку.')
            self.event(post_id,actor,'edited')
        return self.get(post_id)

    def submit(self,post_id,actor):
        with self.db:
            changed=self.db.execute("UPDATE posts SET status='review',revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND status IN ('draft','failed')",(post_id,)).rowcount
            if not changed:
                raise ValueError('Оновіть картку допису.')
            self.event(post_id,actor,'submitted')
        return self.get(post_id)

    def queue(self,post_id,revision,actor,due=None):
        with self.db:
            changed=self.db.execute("UPDATE posts SET status=?,due=?,approved_by=?,auto_source=0,error=NULL,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND revision=? AND status IN ('draft','review','failed')",('scheduled' if due else 'queued',due,actor,post_id,revision)).rowcount
            if not changed:
                raise ValueError('Допис уже змінено або поставлено в чергу. Перегляньте його ще раз.')
            self.event(post_id,actor,'scheduled' if due else 'queued')
        return self.get(post_id)

    def draft(self,post_id,actor,from_uncertain=False):
        allowed=('uncertain',) if from_uncertain else ('scheduled','queued','review','failed')
        placeholders=','.join('?' for _ in allowed)
        with self.db:
            changed=self.db.execute(f"UPDATE posts SET status='draft',due=NULL,approved_by=NULL,error=NULL,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND status IN ({placeholders})",(post_id,*allowed)).rowcount
            if not changed:
                raise ValueError('Статус уже змінився. Оновіть допис.')
            self.event(post_id,actor,'checked_channel' if from_uncertain else 'returned_to_draft')
        return self.get(post_id)

    def archive(self,post_id,actor):
        with self.db:
            changed=self.db.execute("UPDATE posts SET status='archived',due=NULL,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND status IN ('draft','review','failed')",(post_id,)).rowcount
            if not changed:
                raise ValueError('Спершу поверніть допис у чернетки. Опубліковані дописи залишаються в історії.')
            self.event(post_id,actor,'archived')

    def correction(self,post_id,actor):
        original=self.get(post_id)
        if original and original['edit_of']:original=self.get(original['edit_of'])
        if not original or original['status']!='published' or not json.loads(original['sent_ids']):
            raise ValueError('Доступне лише редагування вже опублікованого ботом допису.')
        with self.db:
            fields=('target_id','target_title','header','footer','body_html','body_text','media','buttons','silent','origin','related_to')
            values=[original[field] for field in fields]
            cursor=self.db.execute('INSERT INTO posts(author_id,edit_of,base_revision,'+','.join(fields)+') VALUES('+','.join('?' for _ in range(3+len(fields)))+')',(actor,original['id'],original['revision'],*values))
            self.event(cursor.lastrowid,actor,'correction_created')
        return self.get(cursor.lastrowid)

    def apply_correction(self,post):
        fields=('header','footer','body_html','body_text','buttons')
        with self.db:
            changed=self.db.execute('UPDATE posts SET '+','.join(f'{field}=?' for field in fields)+",revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=? AND revision=? AND status='published'",(*[post[field] for field in fields],post['edit_of'],post['base_revision'])).rowcount
            if changed!=1:raise ValueError('Оригінал змінився. Перевірте опублікований допис.')
            self.event(post['edit_of'],post['approved_by'],'corrected')

    def clarification(self,post_id,actor,header='',footer=''):
        original=self.get(post_id)
        if not original or original['status']!='published' or not json.loads(original['sent_ids']):
            raise ValueError('Уточнення можна додати до опублікованого допису.')
        with self.db:
            cursor=self.db.execute('''INSERT INTO posts(author_id,target_id,target_title,header,footer,related_to,origin)
                VALUES(?,?,?,?,?,?,?)''', (actor,original['target_id'],original['target_title'],header,footer,post_id,json.dumps({'kind':'clarification','original_id':post_id})))
            self.event(cursor.lastrowid,actor,'clarification_created')
        return self.get(cursor.lastrowid)

    def release(self,post_id):
        with self.db:self.db.execute("UPDATE posts SET status=CASE WHEN due IS NULL THEN 'queued' ELSE 'scheduled' END WHERE id=? AND status='sending'",(post_id,))

    def recover(self):
        # A process could stop after Telegram accepted a post but before SQLite recorded it.
        with self.db:
            self.db.execute("UPDATE posts SET status='uncertain',error='Публікацію було перервано. Перевірте канал перед повторним надсиланням.' WHERE status='sending'")

    def claim(self,now=None):
        stamp=utc_string(now)
        with self.db:
            paused=self.db.execute("SELECT value FROM settings WHERE key='publishing_paused'").fetchone()
            if paused and paused[0]=='1':return None
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute("SELECT * FROM posts WHERE status='queued' OR (status='scheduled' AND due<=?) ORDER BY CASE WHEN json_extract(origin,'$.alert.urgent')=1 THEN 0 ELSE 1 END,coalesce(due,created),id LIMIT 1",(stamp,)).fetchone()
            if not row:
                return None
            changed=self.db.execute("UPDATE posts SET status='sending',updated=CURRENT_TIMESTAMP WHERE id=? AND status=?",(row['id'],row['status'])).rowcount
            if not changed:
                return None
            return self.get(row['id'])

    def finish(self,post_id,status,error=None,sent_ids=None,retry_after=None):
        if status not in ('published','failed','uncertain','scheduled'):
            raise ValueError('Некоректний статус публікації.')
        with self.db:
            due=utc_string(datetime.now(timezone.utc)+timedelta(seconds=retry_after)) if retry_after is not None else None
            self.db.execute("UPDATE posts SET status=?,error=?,sent_ids=?,published=CASE WHEN ?='published' THEN CURRENT_TIMESTAMP ELSE published END,due=?,updated=CURRENT_TIMESTAMP WHERE id=? AND status='sending'",(status,error,json.dumps(sent_ids or []),status,due,post_id))
            self.event(post_id,None,status)
