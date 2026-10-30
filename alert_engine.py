"""Deterministic alert classification, durable territorial state and publication gates."""
from datetime import datetime, timezone, timedelta
from html import escape
import json
import hashlib
import re
import string
import unicodedata
from editorial import normalized, similar

DEFAULT_PLACES = [
    {'name':'Сумський район','aliases':['Сумського району','Сумському районі','Сумським районом'],'timer':'Сумський район'},
    {'name':'Суми','aliases':['Сумах','Сумам','Сумами','Сум'],'timer':'Суми'},
    {'name':'Юнаківка','aliases':['Юнаківки','Юнаківку','Юнаківці','Юнаківкою'],'timer':''},
    {'name':'Храпівщина','aliases':['Храпівщини','Храпівщину','Храпівщині','Храпівщиною'],'timer':''},
]
DEFAULT_KEYS = {
    'alarm':['повітряна тривога','повітряну тривогу','повітряної тривоги','оголошено тривогу','тривога'],
    'clear':['відбій','тривогу скасовано','тривога завершилась','тривога завершилася','тривога закінчилася','тривога скасована'],
    'kab':['каб','каби','кабів','кабами'],
    'shahed':['шахед','шахеди','шахедів','шахедами','шахедом','shahed'],
    'fpv':['фпв','fpv'],
    'fiber':['оптика','оптиці','оптоволокно','оптоволокні','оптоволоконний'],
    'reactive':['реактивний','реактивні','реактивного','реактивних'],
    'uncertain':['ударні','ударний','ударних','загроза'],
}
LABELS={'kab':'КАБ','shahed':'Шахед','fpv':'ФПВ','fiber':'ФПВ на оптоволокні','reactive':'Реактивний БпЛА'}
DEFAULT_TEMPLATES={
    'alarm':'🔴 {places} — повітряна тривога.',
    'clear':'🟢 {places} — відбій повітряної тривоги',
    'alarm_yellow':'🟡 {places} — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)',
    'alarm_red':'🔴 {places} — повітряна тривога, червоний рівень: Ракетна загроза (червоний рівень)',
    'direction':'⚠️ {places} — {threat} у вашому напрямку.',
    'threat':'⚠️ {places} — загроза {threat}.',
}
DOUBT=re.compile(r'\?|\b(?:не|немає|нема|без|вчора|учора|позавчора|завтра|можливо|ймовірно|нібито|архів|навчання|навчальна|тест|тестування|тестова|тестовий|тестове|спростовано|очікуємо|чекаємо|очікується|збили|знищено|знищили|ліквідовано|уламки|перехоплено|звіт|наслідки|наші|свої|продаємо|виробництво|виставка|погрожує)\b|\b(?:минул|непідтвердж|ймовірн|можлив)\w*|може\s+бути|була\s+тривога|були\s+каби',re.I)
NOVEL=re.compile(r'\b(?:знову|повторно|ще|нові|нова|новий|інший|інша)\b',re.I)
DIRECTION=re.compile(r'\b(?:[ву]\s+напрямку|напрямок|курс(?:ом)?\s+на|пряму\w*\s+(?:на|до)|руха\w*\s+до|лет\w*\s+на)\s+',re.I)


def now_utc():return datetime.now(timezone.utc)


def stamp(value):
    if isinstance(value,str):value=datetime.fromisoformat(value)
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='seconds')


def folded(text):
    return unicodedata.normalize('NFKC',text).casefold().replace('’',"'").replace('ʼ',"'")


def word_pattern(alias):
    return re.compile(r'(?<!\w)'+re.escape(folded(alias)).replace(r'\ ',r'\s+')+r'(?!\w)')


def place_matches(text, places):
    candidates=[]
    text=folded(text)
    for place in places:
        for alias in [place['name'],*place['aliases']]:
            candidates.extend((match.start(),match.end(),place) for match in word_pattern(alias).finditer(text))
    selected=[];occupied=[]
    for start,end,place in sorted(candidates,key=lambda item:-(item[1]-item[0])):
        if any(start<b and end>a for a,b in occupied):continue
        occupied.append((start,end));selected.append((start,place))
    names=set();result=[]
    for _,place in sorted(selected,key=lambda item:item[0]):
        if place['name'] not in names:result.append(place);names.add(place['name'])
    return result


def parse_places(text):
    result=[];aliases={}
    for line in text.splitlines():
        if not line.strip():continue
        parts=[x.strip() for x in line.split('|')]
        if len(parts)!=3 or not 2<=len(parts[0])<=80:
            raise ValueError('Кожен рядок: Назва | варіанти через кому або - | територія таймера або -')
        name,raw,timer=parts
        values=[] if raw=='-' else [x.strip() for x in raw.split(',') if x.strip()]
        if len(values)>20 or any(len(x)>80 for x in values) or len(timer)>80:
            raise ValueError('До 20 варіантів назви; кожна назва й територія — до 80 символів.')
        for alias in [name,*values]:
            key=folded(alias)
            if key in aliases and aliases[key]!=name:raise ValueError('Однакова назва належить різним територіям: '+alias)
            aliases[key]=name
        if any(x['name']==name for x in result):raise ValueError('Назва повторюється: '+name)
        result.append({'name':name,'aliases':values,'timer':'' if timer=='-' else timer})
    if not 1<=len(result)<=100:raise ValueError('Додайте від 1 до 100 населених пунктів або територій.')
    names={x['name'] for x in result}
    if any(x['timer'] and x['timer'] not in names for x in result):raise ValueError('Територія таймера має бути окремим рядком у цьому списку.')
    return result


def validate_template(value):
    if not 1<=len(value)<=700:raise ValueError('Шаблон має містити від 1 до 700 символів.')
    try:
        fields=list(string.Formatter().parse(value))
    except ValueError:raise ValueError('Перевірте фігурні дужки шаблону.') from None
    if any(field is not None and (field not in ('places','threat') or spec or conversion) for _,field,spec,conversion in fields):
        raise ValueError('Доступні лише {places} та {threat}, без додаткового форматування.')
    if not any(field=='places' for _,field,_,_ in fields):raise ValueError('Додайте {places}, щоб територію було видно.')
    return value


class AlertRepository:
    def __init__(self,db):
        self.db=db
        columns={row[1] for row in db.execute('PRAGMA table_info(channel_sources)')}
        if 'alarm_trusted' not in columns:db.execute('ALTER TABLE channel_sources ADD COLUMN alarm_trusted INTEGER NOT NULL DEFAULT 0')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS alert_states (
            target_id INTEGER NOT NULL,area TEXT NOT NULL,phase TEXT NOT NULL,
            started TEXT,cleared TEXT,observed TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(target_id,area));
        CREATE TABLE IF NOT EXISTS alert_inputs (
            input_key TEXT NOT NULL,area TEXT NOT NULL,PRIMARY KEY(input_key,area));
        CREATE TABLE IF NOT EXISTS target_messages (
            target_id INTEGER NOT NULL,message_id INTEGER NOT NULL,body TEXT NOT NULL,
            event TEXT NOT NULL DEFAULT '{}',created TEXT NOT NULL,automatic INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(target_id,message_id));
        CREATE TABLE IF NOT EXISTS alert_outbox (
            post_id INTEGER PRIMARY KEY,target_id INTEGER NOT NULL,body TEXT NOT NULL,event TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS target_message_time ON target_messages(target_id,created);
        ''')
        if 'level' not in {row[1] for row in db.execute('PRAGMA table_info(alert_states)')}:
            db.execute("ALTER TABLE alert_states ADD COLUMN level TEXT NOT NULL DEFAULT ''")
        db.commit()

    def setting(self,key,default=''):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return row[0] if row else default

    @property
    def enabled(self):return self.setting('alerts_enabled','0')=='1'

    def config(self,name,default):
        value=json.loads(self.setting('alerts_'+name,json.dumps(default,ensure_ascii=False)))
        return {**default,**value} if name=='templates' else value

    def places(self):return self.config('places',DEFAULT_PLACES)

    def config_signature(self):
        data=[self.config('places',DEFAULT_PLACES),self.config('keys',DEFAULT_KEYS),self.config('templates',DEFAULT_TEMPLATES)]
        return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def classify(self,text):
        value=folded(text)
        keys=self.config('keys',DEFAULT_KEYS)
        matches={key:any(word_pattern(alias).search(value) for alias in words) for key,words in keys.items()}
        kind='clear' if matches.get('clear') else 'alarm' if matches.get('alarm') else ''
        yellow=bool(re.search(r'\b(?:жовт\w*\s+рів\w*|дронов\w*\s+загроз\w*)\b',value))
        red=bool(re.search(r'\b(?:червон\w*\s+рів\w*|ракетн\w*\s+загроз\w*)\b',value))
        level=('yellow' if yellow else 'red' if red else '') if kind=='alarm' else ''
        threats=[key for key in LABELS if matches.get(key)]
        if 'fiber' in threats and 'fpv' in threats:threats.remove('fpv')
        if 'reactive' in threats and 'shahed' in threats:threats.remove('shahed')
        if not kind and len(threats)==1:kind=threats[0]
        relevant=bool(kind or threats or matches.get('uncertain') or re.search(r'\b(?:тривог[аиу]|відбою)\b',value))
        selected=place_matches(value,self.places())
        direction=DIRECTION.search(value) if kind not in ('alarm','clear') else None
        if direction:
            destination=re.split(r'[.;\n]',value[direction.end():],maxsplit=1)[0]
            selected=place_matches(destination,self.places())
        directional=bool(direction or re.search(r'\b[ву]\s+вашому\s+напрямку\b',value))
        reason=''
        if relevant and DOUBT.search(value):reason='Заперечення, питання, минула подія або непідтверджені відомості.'
        elif relevant and not selected:reason='Не вдалося однозначно визначити налаштовану територію.'
        elif not kind and relevant:reason='Загальний або неоднозначний тип загрози.'
        elif kind not in ('alarm','clear') and len(threats)>1:reason='У повідомленні кілька різних типів загроз.'
        elif kind=='reactive' and not (matches.get('shahed') or re.search(r'\b(?:бпла|бплa|дрон\w*|безпілот\w*)\b',value)):reason='Не підтверджено, що йдеться про реактивний БпЛА.'
        elif kind=='fiber' and not (matches.get('fpv') or matches.get('uncertain') or directional or re.search(r'\b(?:дрон\w*|лет\w*|руха\w*)\b',value)):
            reason='Саме слово про оптику не підтверджує загрозу ФПВ.'
        elif relevant and NOVEL.search(value):reason='Можлива нова хвиля або зміна ситуації — перевірте повідомлення.'
        if kind in ('alarm','clear') and threats:reason='Змішано стан тривоги та окремий тип загрози.'
        if kind=='alarm' and yellow and red:reason='Одночасно вказані різні рівні тривоги. Потрібне погодження.'
        if kind=='clear':
            remaining=re.sub(r'\bвідбій\s+(?:повітряної\s+)?тривоги\b','',value)
            for alias in keys['clear']:remaining=word_pattern(alias).sub('',remaining)
            if any(word_pattern(alias).search(remaining) for alias in keys['alarm']):
                reason='У повідомленні одночасно різні стани тривоги. Потрібне погодження.'
        # A state belongs to an explicit timer territory, not an inferred district.
        if kind in ('alarm','clear') and any(x['timer']!=x['name'] for x in selected):
            reason='Для стану тривоги потрібна явно налаштована територія таймера.'
        if kind in ('alarm','clear'):
            remainder=value
            if kind=='alarm' and level:
                remainder=re.sub(r'\b(?:жовт\w*\s+рів\w*|червон\w*\s+рів\w*|дронов\w*\s+загроз\w*|ракетн\w*\s+загроз\w*)\b',' ',remainder)
            for place in selected:
                for alias in sorted([place['name'],*place['aliases']],key=len,reverse=True):remainder=word_pattern(alias).sub(' ',remainder)
            for alias in sorted(keys['alarm']+keys['clear'],key=len,reverse=True):remainder=word_pattern(alias).sub(' ',remainder)
            allowed={'у','в','на','для','та','і','й','по','увага','оголошено','оголошена','оголошення','офіційно',
                     'місто','район','повітряна','повітряну','повітряної','тривоги','пройдіть','перейдіть',
                     'до','укриття','залишайтеся','укритті','бережіть','себе','дотримуйтесь','правил','безпеки','безпеці'}
            if any(word not in allowed for word in re.findall(r'[^\W\d_]+',remainder,re.UNICODE)):
                reason=reason or 'Додатковий контекст стану тривоги неоднозначний. Перевірте територію й зміст.'
        names=[x['name'] for x in selected]
        scopes=list(dict.fromkeys(x['timer'] for x in selected if x['timer']))
        facts=re.findall(r'\b\d+\b',value)
        for name,pattern in [('north',r'\bпівн(?:іч|оч)\w*'),('south',r'\bпівд(?:ен|н)\w*'),
                             ('west',r'\bза(?:хід|ход)\w*'),('east',r'\bс(?:хід|ход)\w*')]:
            if re.search(pattern,value):facts.append('direction:'+name)
        if kind in ('alarm','clear'):facts=[]
        return {'kind':kind or ('uncertain' if relevant else 'ordinary'),'places':names,'scopes':scopes,
                'directional':directional,'facts':sorted(set(facts)),'level':level,'reason':reason,'urgent':relevant and not reason}

    def render(self,event):
        key=event['kind'] if event['kind'] in ('alarm','clear') else ('direction' if event['directional'] else 'threat')
        if key=='alarm' and event.get('level') in ('yellow','red'):key+='_'+event['level']
        template=self.config('templates',DEFAULT_TEMPLATES)[key]
        values={'places':' / '.join(event['places']),'threat':LABELS.get(event['kind'],'')}
        text=template.format(**values)
        html=escape(template).format(places='<b>'+escape(values['places'])+'</b>',threat=escape(values['threat']))
        return text,html

    def state_tokens(self,target_id,areas):
        result={}
        for area in areas:
            row=self.db.execute('SELECT * FROM alert_states WHERE target_id=? AND area=?',(target_id,area)).fetchone()
            result[area]=str(row['revision']) if row else '0'
        return result

    def update_state(self,target_id,event,date,input_key):
        date=stamp(date)
        if event['kind'] not in ('alarm','clear') or event['reason']:return
        for area in event['places']:
            row=self.db.execute('SELECT * FROM alert_states WHERE target_id=? AND area=?',(target_id,area)).fetchone()
            if row and (date<row['observed'] or (date==row['observed'] and (row['phase']!=event['kind'] or row['level']!=event.get('level','')))):
                event['reason']='Старий або суперечливий стан території.';event['urgent']=False;return
        for area in event['places']:
            if self.db.execute('SELECT 1 FROM alert_inputs WHERE input_key=? AND area=?',(input_key,area)).fetchone():continue
            row=self.db.execute('SELECT * FROM alert_states WHERE target_id=? AND area=?',(target_id,area)).fetchone()
            if row and date<row['observed']:
                event['reason']='Старіше за відомий стан території.';event['urgent']=False;continue
            if row and date==row['observed'] and row['phase']!=event['kind']:
                event['reason']='Суперечливі стани території з однаковим часом.';event['urgent']=False;continue
            if row and row['phase']==event['kind']:
                changed=row['level']!=event.get('level','')
                self.db.execute('UPDATE alert_states SET observed=?,level=?,revision=revision+? WHERE target_id=? AND area=?',(date,event.get('level',''),int(changed),target_id,area))
            else:
                started=date if event['kind']=='alarm' else (row['started'] if row else None)
                cleared=date if event['kind']=='clear' else None
                self.db.execute('''INSERT INTO alert_states(target_id,area,phase,started,cleared,observed)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(target_id,area) DO UPDATE SET phase=excluded.phase,
                    started=excluded.started,cleared=excluded.cleared,observed=excluded.observed,revision=revision+1''',
                    (target_id,area,event['kind'],started,cleared,date))
                self.db.execute('UPDATE alert_states SET level=? WHERE target_id=? AND area=?',(event.get('level',''),target_id,area))
            self.db.execute('INSERT OR IGNORE INTO alert_inputs VALUES(?,?)',(input_key,area))
            self.db.execute("UPDATE posts SET status='queued',due=NULL WHERE target_id=? AND auto_source=1 AND status='scheduled' AND json_extract(origin,'$.alert_wait')=1",(target_id,))

    def analyse(self,text,source,target_id,date,input_key):
        event=self.classify(text)
        if event['kind'] in ('alarm','clear') and not source['alarm_trusted']:
            event['reason']='Джерело не позначене як довірене для станів тривоги.';event['urgent']=False
        if datetime.fromisoformat(stamp(date))>now_utc()+timedelta(minutes=2):
            event['reason']='Час повідомлення в майбутньому.';event['urgent']=False
        if event['kind'] in ('alarm','clear') and datetime.fromisoformat(stamp(date))<now_utc()-timedelta(minutes=15):
            event['reason']='Старе оголошення не підтверджує поточний стан території.';event['urgent']=False
        with self.db:
            self.update_state(target_id,event,date,input_key)
        event['tokens']=self.state_tokens(target_id,event['scopes'])
        event['config']=self.config_signature()
        return event

    def gate_until(self,target_id,event):
        if event['kind']!='ordinary':return None
        scopes=event['scopes']
        rows=self.db.execute('SELECT * FROM alert_states WHERE target_id=?',(target_id,)).fetchall()
        deadlines=[]
        for row in rows:
            if scopes and row['area'] not in scopes:continue
            base=row['started'] if row['phase']=='alarm' else row['cleared']
            if base:deadlines.append(datetime.fromisoformat(base)+timedelta(minutes=30 if row['phase']=='alarm' else 5))
        return max(deadlines) if deadlines else None

    def duplicate(self,target_id,event,text,date=None):
        since=stamp((date or now_utc())-timedelta(minutes=int(self.setting('alerts_duplicate_minutes','20'))))
        rows=self.db.execute('SELECT * FROM target_messages WHERE target_id=? AND created>=? ORDER BY created DESC',(target_id,since)).fetchall()
        for row in rows:
            old=json.loads(row['event'])
            if event['kind']=='ordinary':
                if similar(normalized(text),normalized(row['body'])):return row['message_id']
            elif not event['reason'] and not old.get('reason') and old.get('kind')==event['kind']:
                if (event.get('level','')==old.get('level','') and set(event['places'])==set(old.get('places',[])) and event['facts']==old.get('facts',[]) and
                        event.get('tokens',{})==old.get('tokens',{})):
                    return row['message_id']
        return None

    def record_target(self,target_id,message_id,text,date,automatic=False,event=None,edited=False,trusted_manual=False):
        if not self.enabled:return
        existing=self.db.execute('SELECT * FROM target_messages WHERE target_id=? AND message_id=?',(target_id,message_id)).fetchone()
        # Replaying the same message must not restart timers or change its original provenance.
        if existing and existing['body']==text and not trusted_manual:return
        own=self.db.execute("SELECT event FROM alert_outbox WHERE target_id=? AND body=? AND created>=datetime('now','-2 minutes')",(target_id,text)).fetchone()
        known=self.db.execute("SELECT origin,auto_source FROM posts WHERE target_id=? AND status='published' AND EXISTS(SELECT 1 FROM json_each(posts.sent_ids) WHERE value=?) ORDER BY id DESC LIMIT 1",(target_id,message_id)).fetchone()
        automatic=False if trusted_manual else (automatic or bool(own) or bool(existing and existing['automatic']) or bool(known and known['auto_source']))
        saved_event=json.loads(known['origin']).get('alert') if known and known['auto_source'] else None
        event=event or saved_event or (json.loads(own['event']) if own else self.classify(text))
        with self.db:
            if not automatic:
                if edited and event['kind'] in ('alarm','clear'):
                    event['reason']='Стан у ручному дописі відредаговано; перевірте поточні стани.'
                    event['urgent']=False
                if event['kind'] in ('alarm','clear') and datetime.fromisoformat(stamp(date))<now_utc()-timedelta(minutes=15):
                    event['reason']='Старий ручний допис не підтверджує поточний стан території.'
                    event['urgent']=False
                initial_reason=event['reason']
                self.update_state(target_id,event,date,f'target:{target_id}:{message_id}:{stamp(date)}')
                # A freshly discovered manual post may precede the first source observation
                # of the same phase. It is proof of publication, not a reason to rewind state.
                if not initial_reason and event['reason'] and event['kind'] in ('alarm','clear'):
                    states=[self.db.execute('SELECT * FROM alert_states WHERE target_id=? AND area=?',(target_id,area)).fetchone() for area in event['places']]
                    if states and all(row and row['phase']==event['kind'] and row['revision']==1 for row in states):
                        event['reason']='';event['urgent']=True
                event['tokens']=self.state_tokens(target_id,event['scopes'])
            self.db.execute('''INSERT INTO target_messages(target_id,message_id,body,event,created,automatic)
                VALUES(?,?,?,?,?,?) ON CONFLICT(target_id,message_id) DO UPDATE SET body=excluded.body,
                event=excluded.event,automatic=excluded.automatic''',(target_id,message_id,text,json.dumps(event,ensure_ascii=False),stamp(date),int(automatic)))

    def before_send(self,post,date=None):
        if not post['auto_source']:return None
        origin=json.loads(post['origin']);event=origin.get('alert')
        if not self.enabled:return ('review','Правила сповіщень вимкнено. Перевірте підготовлений допис.') if event else None
        if not event:return ('review','Правила сповіщень увімкнено після підготовки цього допису. Перевірте його.')
        if event.get('config')!=self.config_signature():return ('review','Налаштування сповіщень змінилися після підготовки допису.')
        if event['reason']:return ('review',event['reason'])
        if event['kind'] in ('alarm','clear'):
            source=self.db.execute('SELECT alarm_trusted FROM channel_sources WHERE chat_id=?',(origin.get('source_id'),)).fetchone()
            if not source or not source['alarm_trusted']:return ('review','Довіру до джерела стану тривоги вимкнено.')
        if event['kind'] in ('alarm','clear') and event.get('tokens')!=self.state_tokens(post['target_id'],event['scopes']):
            return ('review','Стан території змінився після підготовки повідомлення.')
        duplicate=self.duplicate(post['target_id'],event,origin.get('filter_text',post['body_text']),date)
        if duplicate:return ('duplicate',f'Те саме вже є у вашому каналі: повідомлення №{duplicate}.')
        if event['kind']=='ordinary':
            states=self.db.execute('SELECT area FROM alert_states WHERE target_id=?',(post['target_id'],)).fetchall()
            known={row['area'] for row in states}
            if not known or any(area not in known for area in event['scopes']):
                return ('review','Стан потрібної території ще невідомий. Перевірте актуальність і погодьте новину вручну.')
        until=self.gate_until(post['target_id'],event)
        if until and until>(date or now_utc()):return ('wait',stamp(until))
        return None

    def sending(self,post,body):
        if not self.enabled:return
        event=json.loads(post['origin']).get('alert') or self.classify(post['body_text'])
        if 'tokens' not in event:event['tokens']=self.state_tokens(post['target_id'],event['scopes'])
        with self.db:self.db.execute('INSERT OR REPLACE INTO alert_outbox(post_id,target_id,body,event) VALUES(?,?,?,?)',(post['id'],post['target_id'],body,json.dumps(event,ensure_ascii=False)))

    def sent(self,post,body,ids):
        if not self.enabled:return
        outbox=self.db.execute('SELECT * FROM alert_outbox WHERE post_id=?',(post['id'],)).fetchone()
        event=json.loads(outbox['event']) if outbox else None
        if ids:
            manual=not post['auto_source']
            if manual:event=self.classify(post['body_text'])
            self.record_target(post['target_id'],ids[0],body,now_utc(),not manual,event,trusted_manual=manual)
