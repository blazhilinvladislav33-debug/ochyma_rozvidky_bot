"""Private web control plane, served alongside the bot on the same event loop.

GitHub Pages may host web_ui; this module remains on the bot's Windows host.
Telegram OIDC, PKCE, single-use state, signed ID tokens, server-side role checks.
"""
import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
import logging
import os
from pathlib import Path
import secrets
import io
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
from aiohttp import web
from dotenv import dotenv_values
import jwt

from posts_store import EDITABLE
from publications import link_buttons, scheduled_time
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

ROOT=Path(__file__).resolve().parent


def digest(value):return sha256(value.encode()).hexdigest()


def origin(value):
    parts=urlsplit(value)
    return f'{parts.scheme}://{parts.netloc}'


@dataclass
class PanelConfig:
    base_url: str='http://127.0.0.1:8787'
    frontend_url: str='http://127.0.0.1:8787/'
    client_id: str=''
    client_secret: str=''
    host: str='127.0.0.1'
    port: int=8787
    enabled: bool=True

    @classmethod
    def load(cls,root=ROOT):
        values={**dotenv_values(Path(root)/'.panel.env'),**{k:v for k,v in os.environ.items() if k.startswith('WEB_')}}
        base=(values.get('WEB_BASE_URL') or 'http://127.0.0.1:8787').rstrip('/')
        front=values.get('WEB_FRONTEND_URL') or base+'/'
        for value in (base,front):
            parsed=urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
                raise ValueError('Некоректна адреса панелі.')
            if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost')):
                raise ValueError('Для зовнішньої панелі потрібна HTTPS-адреса.')
        if urlsplit(base).path not in ('','/'):
            raise ValueError('WEB_BASE_URL має містити адресу без додаткового шляху.')
        return cls(base,front,values.get('WEB_CLIENT_ID') or '',values.get('WEB_CLIENT_SECRET') or '',
                   values.get('WEB_HOST') or '127.0.0.1',int(values.get('WEB_PORT') or 8787),values.get('WEB_ENABLED','1')=='1')

    @property
    def ready(self):return bool(self.client_id and self.client_secret)


class WebPanel:
    def __init__(self,app,config=None,root=ROOT):
        self.app=app;self.root=Path(root);self.config=config or PanelConfig.load(root)
        self.runner=None;self.http=None;self.keys=None;self.keys_until=0
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS panel_sessions(
                token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,
                expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS panel_uploads(
                file_id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,kind TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS panel_replies(
                operation TEXT NOT NULL,user_id INTEGER NOT NULL,ticket_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,state TEXT NOT NULL,message_id INTEGER,
                created REAL NOT NULL,PRIMARY KEY(operation,user_id));
        ''')
        # An interrupted send may already have reached Telegram. Do not resend it.
        with self.db:self.db.execute("UPDATE panel_replies SET state='uncertain' WHERE state='sending'")
        # Browser login state is intentionally ephemeral, consumed once, and expires.
        self.logins={}
        self.web=web.Application(middlewares=[self.middleware],client_max_size=50*1024*1024)
        self.web.router.add_get('/health',self.health)
        self.web.router.add_get('/auth/start',self.auth_start)
        self.web.router.add_get('/auth/callback',self.auth_callback)
        self.web.router.add_get('/api/session',self.session)
        self.web.router.add_post('/api/logout',self.logout)
        self.web.router.add_route('*','/api/{tail:.*}',self.api)
        self.web.router.add_get('/',self.index)
        self.web.router.add_get('/{asset:index.html|app.js|style.css|config.js}',self.asset)

    @property
    def db(self):return self.app.store.db

    @web.middleware
    async def middleware(self,request,handler):
        allowed={origin(self.config.frontend_url),origin(self.config.base_url)}
        supplied=request.headers.get('Origin')
        if supplied and supplied not in allowed:
            return web.json_response({'error':'Доступ з цієї адреси заборонений.'},status=403)
        try:
            if request.method=='OPTIONS':
                response=web.Response(status=204)
            else:
                if request.method not in ('GET','HEAD') and not supplied:
                    raise web.HTTPForbidden(reason='Потрібна адреса джерела запиту.')
                response=await handler(request)
        except web.HTTPException as exc:
            if exc.status>=400:
                response=web.json_response({'error':exc.reason},status=exc.status)
            else:response=exc
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            response=web.json_response({'error':str(exc)[:300]},status=400)
        except Exception:
            # Never record OAuth query strings, authorization codes or session tokens.
            logging.error('Вебпанель: помилка обробки %s',request.path)
            response=web.json_response({'error':'Не вдалося виконати дію. Оновіть сторінку або перевірте з’єднання.'},status=503)
        if supplied in allowed:
            response.headers['Access-Control-Allow-Origin']=supplied
            response.headers['Vary']='Origin'
            response.headers['Access-Control-Allow-Headers']='Authorization, Content-Type'
            response.headers['Access-Control-Allow-Methods']='GET, POST, PUT, DELETE, OPTIONS'
            response.headers['Access-Control-Expose-Headers']='Content-Disposition'
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self' https:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def user(self,request,admin=False):
        auth=request.headers.get('Authorization','')
        if not auth.startswith('Bearer ') or len(auth)>300:raise web.HTTPUnauthorized(reason='Увійдіть через Telegram.')
        row=self.db.execute('SELECT * FROM panel_sessions WHERE token_hash=? AND expires>?',(digest(auth[7:]),time.time())).fetchone()
        if not row:raise web.HTTPUnauthorized(reason='Сеанс завершився. Увійдіть знову.')
        role=self.app.get_role(row['user_id'])
        if role not in ('owner','admin','moderator') or (admin and role not in ('owner','admin')):
            raise web.HTTPForbidden(reason='Недостатньо прав.')
        return row['user_id'],role,row['name']

    def issue_session(self,uid,name):
        token=secrets.token_urlsafe(40)
        with self.db:
            self.db.execute('DELETE FROM panel_sessions WHERE expires<?',(time.time(),))
            self.db.execute('INSERT INTO panel_sessions VALUES(?,?,?,?)',(digest(token),uid,name[:100],time.time()+8*3600))
        return token

    async def health(self,request):
        return web.json_response({'ok':True,'service':'bot-panel','login_configured':self.config.ready})

    async def auth_start(self,request):
        if not self.config.ready:raise web.HTTPServiceUnavailable(reason='Спершу запустіть Configure-Panel.cmd на серверному ПК.')
        now=time.time();self.logins={k:v for k,v in self.logins.items() if v['expires']>now}
        if len(self.logins)>=200:raise web.HTTPTooManyRequests(reason='Зачекайте хвилину й повторіть вхід.')
        state=secrets.token_urlsafe(32);binding=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48);nonce=secrets.token_urlsafe(32)
        self.logins[state]={'binding':digest(binding),'verifier':verifier,'nonce':nonce,'expires':now+300}
        query=urlencode({'client_id':self.config.client_id,'redirect_uri':self.config.base_url+'/auth/callback',
            'response_type':'code','scope':'openid profile','state':state,'nonce':nonce,
            'code_challenge':base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip('='),'code_challenge_method':'S256'})
        response=web.HTTPFound('https://oauth.telegram.org/auth?'+query)
        response.set_cookie('panel_login',binding,httponly=True,secure=self.config.base_url.startswith('https:'),samesite='Lax',max_age=300,path='/auth')
        return response

    async def validate_id_token(self,token,nonce):
        header=jwt.get_unverified_header(token)
        if header.get('alg')!='RS256' or not isinstance(header.get('kid'),str):raise ValueError('Непідтримуваний підпис Telegram.')
        if not self.keys or time.time()>self.keys_until or not any(k.get('kid')==header['kid'] for k in self.keys['keys']):
            async with self.http.get('https://oauth.telegram.org/.well-known/jwks.json',allow_redirects=False) as response:
                if response.status!=200:raise ValueError('Не вдалося перевірити підпис Telegram.')
                self.keys=await response.json();self.keys_until=time.time()+3600
        key=next((k for k in self.keys['keys'] if k.get('kid')==header['kid']),None)
        if not key:raise ValueError('Невідомий підпис Telegram.')
        claims=jwt.decode(token,jwt.PyJWK.from_dict(key).key,algorithms=['RS256'],audience=self.config.client_id,
            issuer='https://oauth.telegram.org',options={'require':['exp','iat','iss','aud','sub','nonce','id']},leeway=15)
        if not secrets.compare_digest(str(claims['nonce']),nonce):raise ValueError('Невірне підтвердження входу.')
        uid=claims['id']
        if type(uid) is not int or uid<=0:raise ValueError('Telegram не надав ID користувача.')
        return uid,str(claims.get('name') or 'Редактор')

    async def auth_callback(self,request):
        state=request.query.get('state','');attempt=self.logins.pop(state,None)
        binding=request.cookies.get('panel_login','')
        if not attempt or attempt['expires']<time.time() or not secrets.compare_digest(attempt['binding'],digest(binding)):
            raise web.HTTPForbidden(reason='Вхід прострочений. Почніть заново з панелі.')
        code=request.query.get('code','')
        if not code or len(code)>3000:raise web.HTTPForbidden(reason='Вхід не підтверджено.')
        try:
            async with self.http.post('https://oauth.telegram.org/token',auth=aiohttp.BasicAuth(self.config.client_id,self.config.client_secret),
                data={'grant_type':'authorization_code','code':code,'redirect_uri':self.config.base_url+'/auth/callback','code_verifier':attempt['verifier']},allow_redirects=False) as result:
                if result.status!=200:raise ValueError('Telegram не підтвердив вхід.')
                data=await result.json()
            uid,name=await self.validate_id_token(data['id_token'],attempt['nonce'])
        except Exception:
            raise web.HTTPForbidden(reason='Не вдалося перевірити вхід через Telegram. Спробуйте знову.') from None
        if self.app.get_role(uid) not in ('owner','admin','moderator'):
            raise web.HTTPForbidden(reason='Цей Telegram-акаунт не має доступу до керування ботом.')
        token=self.issue_session(uid,name)
        response=web.HTTPFound(self.config.frontend_url+'#session='+token)
        response.del_cookie('panel_login',path='/auth')
        return response

    async def session(self,request):
        uid,role,name=self.user(request)
        return web.json_response({'user_id':uid,'role':role,'name':name})

    async def logout(self,request):
        self.user(request)
        with self.db:self.db.execute('DELETE FROM panel_sessions WHERE token_hash=?',(digest(request.headers['Authorization'][7:]),))
        return web.json_response({'ok':True})

    def get_post(self,pid,uid,role):
        row=self.app.store.posts.get(pid)
        if not row:raise web.HTTPNotFound(reason='Допис не знайдено.')
        if role=='moderator' and row['author_id']!=uid:raise web.HTTPForbidden(reason='Доступні лише ваші дописи.')
        return row

    def post_data(self,row):
        result={k:row[k] for k in ('id','author_id','body_text','header','footer','target_title','silent','status','revision','due','error','created')}
        result['media']=json.loads(row['media']);result['buttons']=json.loads(row['buttons'])
        return result

    def edit_fields(self,data,current,uid):
        text=data.get('body_text',current['body_text'] if current else '')
        if not isinstance(text,str) or len(text)>12000:raise ValueError('Текст завеликий.')
        fields={'body_text':text,'body_html':current['body_html'] if current and text==current['body_text'] else escape(text)}
        for field in ('header','footer'):
            value=data.get(field,current[field] if current else self.app.store.setting('publish_'+field))
            if not isinstance(value,str) or len(value)>300:raise ValueError('Підпис має бути до 300 символів.')
            fields[field]=value
        fields['silent']=int(bool(data.get('silent',current['silent'] if current else False)))
        if 'buttons_text' in data:fields['buttons']=json.dumps(link_buttons(data['buttons_text']),ensure_ascii=False)
        media=data.get('media',json.loads(current['media']) if current else [])
        if not isinstance(media,list) or len(media)>10:raise ValueError('Можна додати до 10 вкладень.')
        existing=json.loads(current['media']) if current else []
        for item in media:
            if item in existing:continue
            if not isinstance(item,dict) or set(item)!={'kind','file_id'} or not self.db.execute('SELECT 1 FROM panel_uploads WHERE file_id=? AND user_id=? AND kind=?',(item.get('file_id'),uid,item.get('kind'))).fetchone():
                raise ValueError('Завантажте вкладення через панель.')
        fields['media']=json.dumps(media)
        return fields

    async def api(self,request):
        uid,role,name=self.user(request);admin=role in ('owner','admin');store=self.app.store;repo=store.posts
        tail=request.match_info['tail'].strip('/');method=request.method
        data=await request.json() if method in ('POST','PUT','DELETE') and request.content_type=='application/json' else {}
        if tail=='tickets' and method=='GET':
            status=request.query.get('status','all');search=request.query.get('q','')[:100];page=max(0,int(request.query.get('page','0')))
            if status not in ('all','new','working','waiting','closed'):raise ValueError('Невідомий стан звернення.')
            filters=[];args=[]
            if status!='all':filters.append('t.status=?');args.append(status)
            if search:
                filters.append('(t.name LIKE ? OR t.username LIKE ? OR CAST(t.user_id AS TEXT)=? OR CAST(t.id AS TEXT)=?)')
                args.extend(('%'+search+'%','%'+search+'%',search,search))
            where=' WHERE '+' AND '.join(filters) if filters else ''
            count=self.db.execute('SELECT count(*) FROM tickets t'+where,args).fetchone()[0]
            rows=self.db.execute('''SELECT t.*,
                (SELECT body FROM messages m WHERE m.ticket_id=t.id ORDER BY id DESC LIMIT 1) preview,
                (SELECT created FROM messages m WHERE m.ticket_id=t.id ORDER BY id DESC LIMIT 1) updated,
                (SELECT count(*) FROM messages m LEFT JOIN read_messages r ON r.message_id=m.id AND r.admin_id=?
                 WHERE m.ticket_id=t.id AND m.direction='in' AND r.message_id IS NULL) unread
                FROM tickets t'''+where+" ORDER BY (t.status='closed'),coalesce(updated,'') DESC,t.id DESC LIMIT 30 OFFSET ?",(uid,*args,page*30)).fetchall()
            return web.json_response({'items':[dict(r) for r in rows],'total':count,'page':page,'unread':store.unread(uid)})
        if tail.startswith('tickets/'):
            return await self.ticket_api(request,uid,tail,method,data)
        if tail=='overview' and method=='GET':
            where='' if admin else ' WHERE author_id=?';args=() if admin else (uid,)
            counts={r['status']:r['n'] for r in self.db.execute('SELECT status,count(*) n FROM posts'+where+' GROUP BY status',args)}
            sources=[dict(r) for r in self.db.execute('SELECT chat_id,title,transport,mode FROM channel_sources')] if admin else []
            return web.json_response({'counts':counts,'paused':store.setting('publishing_paused','0')=='1',
                'target':store.setting('publish_target_title') or 'Канал ще не підключено','sources':sources,
                'reader_status':getattr(self.app.reader,'status','Не підключено'),'login_configured':self.config.ready})
        if tail=='pause' and method=='POST':
            self.user(request,admin=True)
            if type(data.get('paused')) is not bool:raise ValueError('Вкажіть стан паузи.')
            paused=data['paused']
            with self.db:
                was=store.setting('publishing_paused','0')=='1'
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('publishing_paused','1' if paused else '0'))
                if was and not paused:self.db.execute("UPDATE posts SET status='review',approved_by=NULL,due=NULL,auto_source=0,revision=revision+1,error='Повернуто на погодження після загальної паузи.' WHERE status='queued' OR (status='scheduled' AND due<=CURRENT_TIMESTAMP)")
            self.app.publications.wake.set();self.app.bridge.wake.set()
            return web.json_response({'paused':paused})
        if tail=='posts' and method=='GET':
            state=request.query.get('status','all');page=max(0,int(request.query.get('page','0')))
            if state not in ('all','draft','review','scheduled','queued','published','failed','uncertain','archived'):raise ValueError('Невідомий список.')
            filters=[];args=[]
            if not admin:filters.append('author_id=?');args.append(uid)
            if state!='all':filters.append('status=?');args.append(state)
            where=' WHERE '+' AND '.join(filters) if filters else ''
            total=self.db.execute('SELECT count(*) FROM posts'+where,args).fetchone()[0]
            rows=self.db.execute('SELECT * FROM posts'+where+' ORDER BY id DESC LIMIT 20 OFFSET ?',(*args,page*20)).fetchall()
            return web.json_response({'items':[self.post_data(r) for r in rows],'total':total,'page':page})
        if tail=='posts' and method=='POST':
            fields=self.edit_fields(data,None,uid);target=store.setting('publish_target')
            post=repo.create(uid,int(target) if target else None,store.setting('publish_target_title'))
            post=repo.edit(post['id'],uid,**fields)
            return web.json_response(self.post_data(post),status=201)
        bits=tail.split('/')
        if len(bits)>=2 and bits[0]=='posts' and bits[1].isdigit():
            pid=int(bits[1]);post=self.get_post(pid,uid,role)
            if len(bits)==2 and method=='GET':return web.json_response(self.post_data(post))
            if method in ('PUT','POST'):
                if type(data.get('revision')) is not int or post['revision']!=data['revision']:
                    raise web.HTTPConflict(reason='Допис змінився в боті або іншому вікні. Відкрийте його заново.')
            if len(bits)==2 and method=='PUT':
                post=repo.edit(pid,uid,**self.edit_fields(data,post,uid))
                return web.json_response(self.post_data(post))
            if len(bits)==3 and method=='POST':
                action=bits[2]
                if action in ('publish','schedule','submit'):
                    if post['status'] not in EDITABLE:raise ValueError('Допис уже не можна надіслати повторно.')
                    self.app.publications.validate(post)
                    if action=='submit':post=repo.submit(pid,uid)
                    else:
                        self.user(request,admin=True)
                        due=scheduled_time(data.get('when','')) if action=='schedule' else None
                        await self.app.publications.verify_target(post['target_id'])
                        post=repo.queue(pid,data['revision'],uid,due);self.app.publications.wake.set()
                elif action=='archive':repo.archive(pid,uid);post=repo.get(pid)
                elif action=='draft':
                    self.user(request,admin=True)
                    if post['status']=='uncertain':raise ValueError('Спершу перевірте результат публікації в Telegram.')
                    post=repo.draft(pid,uid)
                elif action=='template':
                    title=str(data.get('title','')).strip()
                    if not 1<=len(title)<=60:raise ValueError('Назва має містити від 1 до 60 символів.')
                    self.app.publications.validate(post,target=False)
                    if any(m['kind']=='copy' for m in json.loads(post['media'])):raise ValueError('Для шаблону потрібні завантажені медіа.')
                    payload={k:post[k] for k in ('body_text','body_html','header','footer','buttons','silent')}
                    with self.db:
                        tid=self.db.execute('INSERT INTO saved_templates(user_id,title,payload,media) VALUES(?,?,?,?)',(uid,title,json.dumps(payload,ensure_ascii=False),post['media'])).lastrowid
                    return web.json_response({'id':tid})
                else:raise web.HTTPNotFound()
                return web.json_response(self.post_data(post))
        if tail=='templates' and method=='GET':
            rows=self.db.execute('SELECT * FROM saved_templates WHERE user_id=? ORDER BY id DESC',(uid,)).fetchall()
            return web.json_response({'items':[{'id':r['id'],'title':r['title'],'revision':r['revision'],'media':json.loads(r['media']),**json.loads(r['payload'])} for r in rows]})
        if len(bits)>=2 and bits[0]=='templates' and bits[1].isdigit():
            tid=int(bits[1]);row=self.db.execute('SELECT * FROM saved_templates WHERE id=? AND user_id=?',(tid,uid)).fetchone()
            if not row:raise web.HTTPNotFound(reason='Шаблон не знайдено.')
            if type(data.get('revision')) is not int or data['revision']!=row['revision']:raise web.HTTPConflict(reason='Шаблон змінився. Оновіть список.')
            if len(bits)==3 and bits[2]=='use' and method=='POST':
                target=store.setting('publish_target');post=repo.create(uid,int(target) if target else None,store.setting('publish_target_title'))
                post=repo.edit(post['id'],uid,**json.loads(row['payload']),media=row['media'])
                return web.json_response(self.post_data(post))
            if len(bits)==2 and method=='DELETE':
                with self.db:self.db.execute('DELETE FROM saved_templates WHERE id=? AND user_id=? AND revision=?',(tid,uid,data['revision']))
                return web.json_response({'ok':True})
            if len(bits)==2 and method=='PUT':
                title=str(data.get('title','')).strip()
                if not 1<=len(title)<=60:raise ValueError('Назва має містити від 1 до 60 символів.')
                current={**json.loads(row['payload']),'media':row['media']}
                fields=self.edit_fields(data,current,uid)
                media=fields.pop('media');payload=json.dumps(fields,ensure_ascii=False)
                with self.db:self.db.execute('UPDATE saved_templates SET title=?,payload=?,media=?,revision=revision+1 WHERE id=? AND user_id=? AND revision=?',(title,payload,media,tid,uid,data['revision']))
                return web.json_response({'ok':True})
        if tail=='upload' and method=='POST':return await self.upload(request,uid)
        raise web.HTTPNotFound(reason='Розділ не знайдено.')

    async def ticket_api(self,request,uid,tail,method,data):
        store=self.app.store;bits=tail.split('/')
        if len(bits)<2 or not bits[1].isdigit():raise web.HTTPNotFound()
        tid=int(bits[1]);ticket=store.ticket(tid)
        if not ticket:raise web.HTTPNotFound(reason='Звернення не знайдено.')
        if len(bits)==2 and method=='GET':
            after=max(0,int(request.query.get('after','0')));before=max(0,int(request.query.get('before','0')))
            if after:rows=self.db.execute('SELECT * FROM messages WHERE ticket_id=? AND id>? ORDER BY id LIMIT 100',(tid,after)).fetchall()
            else:rows=list(reversed(self.db.execute('SELECT * FROM messages WHERE ticket_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 50',(tid,before,before)).fetchall()))
            messages=[{'id':r['id'],'direction':r['direction'],'body':r['body'],'content_type':r['content_type'],
                       'created':r['created'],'has_media':bool(json.loads(r['media'] or '[]')),
                       'attachment':r['content_type'] not in ('text',None)} for r in rows]
            first=rows[0]['id'] if rows else 0
            older=bool(first and self.db.execute('SELECT 1 FROM messages WHERE ticket_id=? AND id<?',(tid,first)).fetchone())
            blocked=store.one('SELECT blocked FROM users WHERE user_id=?',(ticket['user_id'],))
            return web.json_response({'ticket':dict(ticket),'messages':messages,'older':older,'blocked':bool(blocked and blocked[0])})
        if len(bits)==3 and bits[2]=='read' and method=='POST':
            values=data.get('ids',[])
            if not isinstance(values,list) or len(values)>100 or any(type(v) is not int for v in values):raise ValueError('Некоректний список повідомлень.')
            valid=[v for v in values if self.db.execute('SELECT 1 FROM messages WHERE id=? AND ticket_id=?',(v,tid)).fetchone()]
            store.mark_read(uid,valid)
            return web.json_response({'ok':True})
        if len(bits)==3 and bits[2]=='status' and method=='POST':
            if data.get('status') not in ('new','working','waiting','closed'):raise ValueError('Некоректний стан звернення.')
            with self.db:
                changed=self.db.execute('UPDATE tickets SET status=? WHERE id=? AND status=?',(data['status'],tid,data.get('expected'))).rowcount
                if not changed:raise web.HTTPConflict(reason='Стан звернення вже змінився. Оновіть діалог.')
                if data['status']=='closed':self.db.execute('DELETE FROM sessions WHERE ticket_id=?',(tid,))
            return web.json_response({'ok':True})
        if len(bits)==3 and bits[2]=='reply' and method=='POST':
            operation=data.get('operation','');body=data.get('body','');media=data.get('media',[])
            if not isinstance(operation,str) or not 20<=len(operation)<=100:raise ValueError('Некоректний номер відповіді.')
            if not isinstance(body,str) or len(body)>4000 or not isinstance(media,list) or len(media)>1 or not (body.strip() or media):raise ValueError('Введіть текст до 4000 символів або додайте одне вкладення.')
            if media:
                if body and len(body)>1000:raise ValueError('Підпис до вкладення має бути до 1000 символів.')
                self.edit_fields({'media':media},None,uid)
            signature=digest(json.dumps({'body':body,'media':media},sort_keys=True))
            previous=self.db.execute('SELECT * FROM panel_replies WHERE operation=? AND user_id=?',(operation,uid)).fetchone()
            if previous:
                if previous['ticket_id']!=tid or previous['content_hash']!=signature:raise web.HTTPConflict(reason='Номер відповіді вже використаний для іншого повідомлення.')
                if previous['state']=='sent':return web.json_response({'ok':True,'message_id':previous['message_id']})
                raise web.HTTPConflict(reason='Ця відповідь уже оброблялась. Перевірте історію в Telegram перед повтором.')
            if ticket['status']=='closed':raise ValueError('Спершу відкрийте звернення знову.')
            blocked=store.one('SELECT blocked FROM users WHERE user_id=?',(ticket['user_id'],))
            if blocked and blocked[0]:raise ValueError('Користувача заблоковано. Відповідь не надіслано.')
            with self.db:self.db.execute("INSERT INTO panel_replies VALUES(?,?,?,?,'sending',NULL,?)",(operation,uid,tid,signature,time.time()))
            try:
                if media:
                    from account_reader import input_file
                    item=media[0];function=getattr(self.app.bot,'send_'+item['kind'])
                    sent=await function(ticket['user_id'],input_file(item['file_id']),caption=body or None,parse_mode=None)
                else:sent=await self.app.bot.send_message(ticket['user_id'],body,parse_mode=None)
            except (TelegramBadRequest,TelegramForbiddenError):
                with self.db:self.db.execute("UPDATE panel_replies SET state='failed' WHERE operation=? AND user_id=?",(operation,uid))
                raise ValueError('Telegram не доставив відповідь. Користувач міг заблокувати бота.') from None
            except Exception:
                with self.db:self.db.execute("UPDATE panel_replies SET state='uncertain' WHERE operation=? AND user_id=?",(operation,uid))
                raise ValueError('Результат надсилання невідомий. Перевірте діалог у Telegram перед повтором.') from None
            with self.db:
                store.reply(tid,ticket['user_id'],sent.message_id,body,media[0]['kind'] if media else 'text',media)
                self.db.execute("UPDATE panel_replies SET state='sent',message_id=? WHERE operation=? AND user_id=?",(sent.message_id,operation,uid))
            return web.json_response({'ok':True,'message_id':sent.message_id})
        if len(bits)==5 and bits[2]=='messages' and bits[3].isdigit() and bits[4] in ('attachment','forward'):
            item=self.db.execute('SELECT * FROM messages WHERE id=? AND ticket_id=?',(int(bits[3]),tid)).fetchone()
            if not item:raise web.HTTPNotFound()
            if bits[4]=='forward' and method=='POST':
                await self.app.bot.copy_message(uid,item['chat_id'],item['message_id'])
                return web.json_response({'ok':True})
            if bits[4]=='attachment' and method=='GET':
                media=json.loads(item['media'] or '[]')
                if not media:raise ValueError('Для старого вкладення натисніть «Отримати в Telegram».')
                value=media[0]['file_id'];kind=media[0]['kind']
                if value.startswith('reader:'):
                    from account_reader import media_path
                    path=media_path(value,self.root)
                    return web.FileResponse(path,headers={'Content-Disposition':'attachment; filename="attachment'+path.suffix+'"'})
                file=await self.app.bot.get_file(value)
                if not file.file_path or (file.file_size or 0)>49*1024*1024:raise ValueError('Великий файл відкрийте в Telegram.')
                buffer=io.BytesIO();await self.app.bot.download_file(file.file_path,buffer)
                ext=Path(file.file_path).suffix
                if len(ext)>8 or not ext[1:].isalnum():ext='.bin'
                return web.Response(body=buffer.getvalue(),content_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="attachment'+ext+'"'})
        raise web.HTTPNotFound()

    async def upload(self,request,uid):
        reader=await request.multipart();field=await reader.next()
        if field is None or field.name!='file':raise ValueError('Оберіть файл.')
        folder=self.root/'reader_media';folder.mkdir(exist_ok=True)
        if sum(p.stat().st_size for p in folder.iterdir() if p.is_file())>=1024**3:raise ValueError('Сховище медіа заповнене.')
        path=folder/('web-'+secrets.token_hex(20)+'.bin');size=0;head=b''
        try:
            with path.open('wb') as stream:
                while True:
                    chunk=await field.read_chunk()
                    if not chunk:break
                    size+=len(chunk)
                    if size>49*1024*1024:raise ValueError('Файл має бути до 49 МБ.')
                    if len(head)<32:head=(head+chunk)[:32]
                    stream.write(chunk)
            if head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n'):
                kind='photo'
                if size>10*1024*1024:raise ValueError('Фото має бути до 10 МБ.')
            elif head[4:8]==b'ftyp':kind='video'
            else:raise ValueError('Підтримуються фото JPG/PNG та відео MP4.')
            extension='.mp4' if kind=='video' else '.png' if head.startswith(b'\x89PNG') else '.jpg'
            final=path.with_suffix(extension);path.rename(final);path=final
            file_id='reader:'+path.name
            with self.db:self.db.execute('INSERT INTO panel_uploads VALUES(?,?,?,?)',(file_id,uid,kind,time.time()))
            return web.json_response({'kind':kind,'file_id':file_id})
        except BaseException:
            path.unlink(missing_ok=True);raise

    async def index(self,request):return web.FileResponse(self.root/'web_ui'/'index.html')

    async def asset(self,request):return web.FileResponse(self.root/'web_ui'/request.match_info['asset'])

    async def start(self):
        if not self.config.enabled:return
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.runner=web.AppRunner(self.web,access_log=None)
        try:
            await self.runner.setup()
            await web.TCPSite(self.runner,self.config.host,self.config.port).start()
        except BaseException:
            await self.stop();raise
        logging.info('Вебпанель: http://127.0.0.1:%s',self.config.port)

    async def stop(self):
        if self.runner:await self.runner.cleanup()
        if self.http:await self.http.close()


async def run_services(application):
    """One host process; the panel stays available while bot networking retries."""
    from host_runner import supervise
    panel=WebPanel(application)
    await panel.start()
    try:await supervise(application.main,application.bot.session.close)
    finally:await panel.stop()


if __name__=='__main__':
    # UI preview only: no database, Telegram connection or authentication bypass.
    import http.server
    from functools import partial
    print('Демонстрація: http://127.0.0.1:8788/?demo=1',flush=True)
    http.server.ThreadingHTTPServer(('127.0.0.1',8788),partial(http.server.SimpleHTTPRequestHandler,directory=str(ROOT/'web_ui'))).serve_forever()
