import json
from datetime import datetime, timedelta, timezone


ORDER_STATUS = {
    'awaiting_payment': 'Очікує оплати', 'review': 'Оплата на перевірці',
    'paid': 'Оплату підтверджено', 'shipped': 'Відправлено',
    'cancelled': 'Скасовано', 'expired': 'Термін бронювання минув',
}


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class BookingRepository:
    def __init__(self, db):
        self.db = db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, photo_id TEXT NOT NULL,
            price INTEGER NOT NULL CHECK(price>0), stock INTEGER NOT NULL CHECK(stock>=0),
            active INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS product_events (
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, actor_id INTEGER,
            action TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS booking_states (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
            product_title TEXT NOT NULL, photo_id TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0), price INTEGER NOT NULL CHECK(price>0),
            recipient TEXT NOT NULL, phone TEXT NOT NULL, shipping TEXT NOT NULL,
            payment_details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'awaiting_payment',
            receipt_id TEXT, receipt_type TEXT, tracking TEXT, ticket_id INTEGER,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, expires TEXT NOT NULL,
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS booking_menu_updates (
            user_id INTEGER PRIMARY KEY, version INTEGER NOT NULL, enabled INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL DEFAULT 0, error TEXT,
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS booking_menu_queue ON booking_menu_updates(status,not_before);
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, actor_id INTEGER,
            action TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS order_user ON orders(user_id,id);
        CREATE INDEX IF NOT EXISTS order_status ON orders(status,expires);
        ''')
        columns={row[1] for row in self.db.execute('PRAGMA table_info(products)')}
        if 'deleted' not in columns:
            self.db.execute('ALTER TABLE products ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0')
            self.db.commit()

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def setting(self, key, default=''):
        row = self.one('SELECT value FROM settings WHERE key=?', (key,))
        return row[0] if row else default

    def readiness(self):
        missing=[]
        if not self.setting('booking_card').strip():
            missing.append('Вкажіть номер картки та отримувача через «Реквізити оплати».')
        if not self.one('SELECT id FROM products WHERE active=1 AND deleted=0 AND stock>0 LIMIT 1'):
            if self.one('SELECT id FROM products WHERE deleted=0 LIMIT 1'):
                missing.append('У каталозі адміністратора відкрийте шеврон, додайте залишок і перевірте, що його показано в каталозі.')
            else:
                missing.append('Натисніть «Додати шеврон»: завантажте фото, вкажіть назву, ціну та кількість більше нуля.')
        return missing

    def set_enabled(self, enabled, staff_ids=()):
        enabled=int(bool(enabled))
        staff_ids=tuple(sorted(set(staff_ids)))
        with self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            version=int(self.setting('booking_menu_version','0'))
            if self.setting('bookings_enabled','0')==str(enabled):
                return False,version,0
            if enabled:
                missing=self.readiness()
                if missing:
                    raise ValueError('Щоб увімкнути бронювання:\n\n'+'\n\n'.join(f'{i}. {text}' for i,text in enumerate(missing,1)))
            version+=1
            self.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)', [('bookings_enabled',str(enabled)),('booking_menu_version',str(version))])
            exclude=' AND user_id NOT IN ('+','.join('?' for _ in staff_ids)+')' if staff_ids else ''
            # Latest state replaces older unsent announcements on rapid toggles.
            self.db.execute("INSERT INTO booking_menu_updates(user_id,version,enabled) SELECT user_id,?,? FROM users WHERE blocked=0"+exclude+
                " ON CONFLICT(user_id) DO UPDATE SET version=excluded.version,enabled=excluded.enabled,status='pending',attempts=0,not_before=0,error=NULL,updated=CURRENT_TIMESTAMP",(version,enabled,*staff_ids))
            count=self.one("SELECT count(*) FROM booking_menu_updates WHERE version=? AND status='pending'",(version,))[0]
            return True,version,count

    def next_menu_update(self, now):
        return self.one("SELECT * FROM booking_menu_updates WHERE status='pending' AND not_before<=? ORDER BY updated,user_id LIMIT 1",(now,))

    def finish_menu_update(self,user_id,version,status,error=None,not_before=0):
        if status not in ('sent','pending','failed','skipped'):
            raise ValueError('Некоректний статус сповіщення.')
        with self.db:
            self.db.execute('UPDATE booking_menu_updates SET status=?,error=?,not_before=?,attempts=attempts+1,updated=CURRENT_TIMESTAMP WHERE user_id=? AND version=?', (status,error,not_before,user_id,version))

    def state(self, user_id):
        row = self.one('SELECT data FROM booking_states WHERE user_id=?', (user_id,))
        return json.loads(row[0]) if row else None

    def set_state(self, user_id, data):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO booking_states VALUES(?,?)', (user_id,json.dumps(data,ensure_ascii=False)))

    def clear(self, user_id):
        with self.db:
            self.db.execute('DELETE FROM booking_states WHERE user_id=?', (user_id,))

    def product(self, product_id):
        return self.one('SELECT * FROM products WHERE id=? AND deleted=0', (product_id,))

    def order(self, order_id):
        return self.one('SELECT * FROM orders WHERE id=?', (order_id,))

    def delete_product(self, product_id, actor_id):
        with self.db:
            changed=self.db.execute('UPDATE products SET deleted=1,active=0 WHERE id=? AND deleted=0',(product_id,)).rowcount
            if changed!=1:
                raise ValueError('Товар уже видалено або не знайдено.')
            self.db.execute("INSERT INTO product_events(product_id,actor_id,action) VALUES(?,?,'deleted')",(product_id,actor_id))

    def add_product(self, title, photo_id, price, stock):
        if not title or not photo_id or price<=0 or stock<0:
            raise ValueError('Некоректні дані шеврона.')
        with self.db:
            return self.db.execute('INSERT INTO products(title,photo_id,price,stock) VALUES(?,?,?,?)', (title,photo_id,price,stock)).lastrowid

    def reserve(self, user_id, data):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            # Conditional stock update serializes competing reservations in SQLite.
            prior = self.one('SELECT * FROM orders WHERE request_key=?', (data['key'],))
            if prior:
                if prior['user_id'] != user_id:
                    raise ValueError('Замовлення належить іншому користувачу.')
                return prior, False
            if self.setting('bookings_enabled','0') != '1':
                raise ValueError('Нові бронювання зараз вимкнені.')
            card = self.setting('booking_card')
            if not card:
                raise ValueError('Адміністратор ще не налаштував реквізити оплати.')
            blocked = self.one('SELECT blocked FROM users WHERE user_id=?',(user_id,))
            if blocked and blocked[0]:
                raise ValueError('Бронювання для вашого облікового запису обмежено.')
            quantity = data['quantity']
            if not isinstance(quantity,int) or quantity<1 or quantity>100000:
                raise ValueError('Некоректна кількість.')
            product = self.product(data['product_id'])
            if not product or not product['active']:
                raise ValueError('Цей шеврон уже недоступний.')
            if product['price'] != data['price']:
                raise ValueError('Ціна змінилася. Відкрийте каталог і почніть бронювання знову.')
            changed = self.db.execute('UPDATE products SET stock=stock-? WHERE id=? AND active=1 AND deleted=0 AND stock>=? AND price=?', (quantity,product['id'],quantity,data['price'])).rowcount
            if changed != 1:
                raise ValueError('Недостатньо шевронів у наявності. Оберіть меншу кількість.')
            expires = (datetime.now(timezone.utc)+timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            order_id = self.db.execute('''INSERT INTO orders(request_key,user_id,product_id,product_title,photo_id,quantity,price,recipient,phone,shipping,payment_details,expires)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (data['key'],user_id,product['id'],product['title'],product['photo_id'],quantity,product['price'],data['recipient'],data['phone'],data['shipping'],card,expires)).lastrowid
            self.db.execute('INSERT INTO order_events(order_id,actor_id,action) VALUES(?,?,?)', (order_id,user_id,'reserved'))
            return self.order(order_id), True

    def receipt(self, order_id, user_id, file_id, kind):
        with self.db:
            changed = self.db.execute("UPDATE orders SET status='review',receipt_id=?,receipt_type=?,updated=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND status='awaiting_payment' AND expires>=CURRENT_TIMESTAMP", (file_id,kind,order_id,user_id)).rowcount
            if not changed:
                raise ValueError('Оплату вже передано на перевірку або бронювання недоступне. Відкрийте замовлення знову.')
            self.db.execute('INSERT INTO order_events(order_id,actor_id,action) VALUES(?,?,?)', (order_id,user_id,'receipt'))

    def transition(self, order_id, action, actor_id, user_id=None, tracking=None):
        allowed = {'paid':('review',), 'reject':('review',), 'cancelled':('awaiting_payment','review'), 'shipped':('paid',), 'expired':('awaiting_payment',)}
        if action not in allowed:
            raise ValueError('Невідома дія.')
        with self.db:
            order = self.order(order_id)
            if not order or (user_id is not None and order['user_id'] != user_id):
                raise ValueError('Замовлення не знайдено.')
            if user_id is not None and (action!='cancelled' or order['status']!='awaiting_payment'):
                raise ValueError('Після надсилання оплати скасування погоджує адміністратор у чаті.')
            if order['status'] not in allowed[action]:
                raise ValueError('Статус уже змінився. Оновіть замовлення.')
            if action=='expired' and order['expires']>=utc_now():
                raise ValueError('Термін бронювання ще не минув.')
            if action=='shipped' and (not tracking or not tracking.isascii() or not tracking.isdigit() or len(tracking)!=14):
                raise ValueError('Номер ТТН має містити 14 цифр.')
            status = 'awaiting_payment' if action=='reject' else action
            expires = (datetime.now(timezone.utc)+timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S') if action=='reject' else order['expires']
            changed = self.db.execute('UPDATE orders SET status=?,tracking=coalesce(?,tracking),expires=?,updated=CURRENT_TIMESTAMP WHERE id=? AND status=?', (status,tracking,expires,order_id,order['status'])).rowcount
            if changed!=1:
                raise ValueError('Статус уже змінився. Оновіть замовлення.')
            if action in ('cancelled','expired'):
                self.db.execute('UPDATE products SET stock=stock+? WHERE id=?', (order['quantity'],order['product_id']))
            self.db.execute('INSERT INTO order_events(order_id,actor_id,action) VALUES(?,?,?)', (order_id,actor_id,action))
            return self.order(order_id)

    def expire(self):
        rows = self.db.execute("SELECT id FROM orders WHERE status='awaiting_payment' AND expires<CURRENT_TIMESTAMP").fetchall()
        expired = []
        for row in rows:
            try:
                expired.append(self.transition(row['id'],'expired',None))
            except ValueError:
                pass
        return expired
