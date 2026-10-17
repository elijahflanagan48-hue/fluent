"""Same-origin WSGI API. Development: run.py; production: Gunicorn behind HTTPS."""
import base64
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from http import HTTPStatus
from urllib.parse import parse_qs, quote

from .core import (ROOT, Problem, connect, rows, one, insert, uid, now, digest, password_hash,
                   verify_password, make_token, token_record, public_user, teacher, owns,
                   base_url, data_dir, text, email, audit)
from . import service, integrations, storage


def rate_limit(db, key, limit=12, window=900):
    record = one(db,'SELECT * FROM rate_limits WHERE key=?',(key,))
    if not record or record['expires_at'] < time.time():
        db.execute('INSERT INTO rate_limits VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET count=excluded.count,expires_at=excluded.expires_at',(key,1,time.time()+window))
    elif record['count'] >= limit:
        raise Problem('Слишком много попыток. Повторите позже.',429)
    else:
        db.execute('UPDATE rate_limits SET count=count+1 WHERE key=?',(key,))


def session_user(db, env):
    cookie = SimpleCookie()
    try:
        cookie.load(env.get('HTTP_COOKIE',''))
        raw = cookie['fluent_session'].value if 'fluent_session' in cookie else ''
    except Exception:
        raw = ''
    row = one(db,'SELECT u.*,s.csrf,s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',(digest(raw),time.time()))
    return row


def make_session(db, user):
    raw = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(32)
    insert(db,'sessions',token_hash=digest(raw),user_id=user['id'],csrf=csrf,expires_at=time.time()+7*86400)
    cookie='fluent_session='+raw+'; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800'
    if base_url().startswith('https://'):
        cookie+='; Secure'
    return {'user':public_user(user),'csrf':csrf}, [('Set-Cookie',cookie)]


def require_user(user):
    if not user:
        raise Problem('Войдите в кабинет.',401)


def require_csrf(env, user):
    if not hmac.compare_digest(env.get('HTTP_X_CSRF_TOKEN',''),user['csrf']):
        raise Problem('Обновите страницу и повторите действие.',403)


def read_body(env):
    try:
        length=int(env.get('CONTENT_LENGTH') or 0)
    except ValueError:
        raise Problem('Некорректный запрос.')
    if length > 15*1024*1024 or length < 0:
        raise Problem('Слишком большой запрос.',413)
    if length and not env.get('CONTENT_TYPE','').startswith('application/json'):
        raise Problem('Требуется JSON.',415)
    try:
        data=json.loads(env['wsgi.input'].read(length)) if length else {}
    except (ValueError,UnicodeDecodeError):
        raise Problem('Некорректный JSON.')
    if not isinstance(data,dict):
        raise Problem('Требуется объект JSON.')
    return data


def public_route(db, env, path, method, data):
    if path=='/api/health' and method=='GET':
        return {'ok':True},200,[]
    if path=='/api/auth/info' and method=='GET':
        ready=bool(one(db,"SELECT id FROM users WHERE role='teacher'"))
        return {'demo':os.environ.get('FLUENT_DEMO')=='1','ready':ready,
                'setup_enabled':not ready and len(os.environ.get('FLUENT_SETUP_TOKEN',''))>=32},200,[]
    if path=='/api/auth/setup' and method=='POST':
        rate_limit(db,'setup:'+env.get('REMOTE_ADDR','local'),8,3600)
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        expected=os.environ.get('FLUENT_SETUP_TOKEN','')
        if len(expected)<32:
            raise Problem('Открытие студии через браузер не настроено.',404)
        provided=data.get('setup_token','')
        if not isinstance(provided,str) or not hmac.compare_digest(provided.encode(),expected.encode()):
            raise Problem('Код открытия студии не совпадает.',403)
        if one(db,"SELECT id FROM users WHERE role='teacher'"):
            raise Problem('Студия уже открыта. Войдите в свой кабинет.',409)
        person=uid()
        insert(db,'users',id=person,email=email(data.get('email')),name=text(data.get('name'),100,True),
               role='teacher',password_hash=password_hash(data.get('password')),created_at=now())
        audit(db,None,'studio.opened',person)
        payload,headers=make_session(db,one(db,'SELECT * FROM users WHERE id=?',(person,),True))
        return payload,200,headers
    if path=='/api/auth/demo' and method=='POST':
        if os.environ.get('FLUENT_DEMO')!='1':
            raise Problem('Демо отключено.',404)
        role='student' if data.get('role')=='student' else 'teacher'
        user=one(db,'SELECT * FROM users WHERE email=?',('student@demo.local' if role=='student' else 'teacher@demo.local',),True)
        payload,headers=make_session(db,user)
        return payload,200,headers
    if path in ('/api/auth/login','/api/auth/forgot','/api/auth/accept') and method=='POST':
        rate_limit(db,'auth:'+env.get('REMOTE_ADDR','local'),30,900)
        # Keep the abuse counter even if authentication or validation fails.
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        if path.endswith('/login'):
            addr=email(data.get('email'))
            rate_limit(db,'login:'+digest(addr),10,900)
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            user=one(db,'SELECT * FROM users WHERE email=? AND active=1',(addr,))
            valid=verify_password(data.get('password'),user['password_hash'] if user else None)
            if not user or not valid:
                raise Problem('Неверный email или пароль.',401)
            db.execute('DELETE FROM rate_limits WHERE key=?',('login:'+digest(addr),))
            payload,headers=make_session(db,user)
            return payload,200,headers
        if path.endswith('/forgot'):
            if not integrations.configured('SMTP_HOST','SMTP_FROM'):
                raise Problem('Восстановление по email пока не подключено. Попросите преподавателя выдать новую ссылку входа.',503)
            addr=email(data.get('email'))
            user=one(db,'SELECT * FROM users WHERE email=? AND active=1',(addr,))
            if user:
                raw=make_token(db,user['id'],'reset',3600)
                insert(db,'outbox',id=uid(),user_id=user['id'],channel='email',subject='Восстановление доступа — fluent.',body=base_url()+'/#reset='+raw,event_key='reset:'+uid())
            return {'ok':True},200,[]
        kind='reset' if data.get('kind')=='reset' else 'invite'
        token=token_record(db,data.get('token'),kind)
        encoded=password_hash(data.get('password'))
        db.execute('UPDATE users SET password_hash=? WHERE id=?',(encoded,token['user_id']))
        db.execute("DELETE FROM tokens WHERE user_id=? AND kind IN ('invite','reset')",(token['user_id'],))
        db.execute('DELETE FROM sessions WHERE user_id=?',(token['user_id'],))
        user=one(db,'SELECT * FROM users WHERE id=? AND active=1',(token['user_id'],),True)
        payload,headers=make_session(db,user)
        return payload,200,headers
    if path=='/api/webhooks/telegram' and method=='POST':
        expected=os.environ.get('TELEGRAM_WEBHOOK_SECRET','')
        if not expected or not hmac.compare_digest(env.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN',''),expected):
            raise Problem('Недействительная подпись.',403)
        message=data.get('message',{})
        content=message.get('text','')
        chat=message.get('chat',{})
        if chat.get('type')=='private' and content.startswith('/start '):
            raw=content.split(' ',1)[1]
            try:
                record=token_record(db,raw,'telegram')
            except Problem:
                return {'ok':True},200,[]
            db.execute('UPDATE users SET telegram_chat=?,telegram_enabled=1 WHERE id=?',(str(chat['id']),record['user_id']))
            db.execute('DELETE FROM tokens WHERE token_hash=?',(record['token_hash'],))
        return {'ok':True},200,[]
    return None


def api_route(db,env,path,method,data,user):
    if path=='/api/auth/me' and method=='GET':
        return {'user':public_user(user),'csrf':user['csrf']},200,[]
    if path=='/api/bootstrap' and method=='GET':
        return service.bootstrap(db,user),200,[]
    if path=='/api/auth/logout' and method=='POST':
        db.execute('DELETE FROM sessions WHERE token_hash=?',(user['token_hash'],))
        return {'ok':True},200,[('Set-Cookie','fluent_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')]
    if path=='/api/auth/password' and method=='POST':
        if not verify_password(data.get('current_password'),user['password_hash']):
            raise Problem('Текущий пароль не совпадает.',403)
        db.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(data.get('password')),user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?',(user['id'],user['token_hash']))
        return {'ok':True},200,[]
    if path=='/api/integrations/google/callback' and method=='GET':
        query=parse_qs(env.get('QUERY_STRING',''))
        record=token_record(db,query.get('state',[''])[0],'google')
        if record['user_id']!=user['id']:
            raise Problem('Подключение начато другим пользователем.',403)
        if query.get('error'):
            raise Problem('Подключение Google отменено.')
        token=integrations.google_exchange(query.get('code',[''])[0])
        if not token.get('refresh_token'):
            raise Problem('Google не выдал доступ для синхронизации. Повторите подключение.')
        token['expires_at']=time.time()+token['expires_in']
        db.execute("INSERT INTO integrations VALUES (?,'google',?) ON CONFLICT(user_id,provider) DO UPDATE SET data=excluded.data",(user['id'],json.dumps(token)))
        db.execute('DELETE FROM calendar_events WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM sync_state WHERE provider=?',('google:'+user['id'],))
        db.execute('DELETE FROM tokens WHERE token_hash=?',(record['token_hash'],))
        db.commit()
        return b'',303,[('Location','/#settings')]
    if path=='/api/integrations/google/connect' and method=='POST':
        raw=make_token(db,user['id'],'google',600)
        return {'url':integrations.google_auth_url(raw)},200,[]
    if path=='/api/integrations/google/sync' and method=='POST':
        clause,args=service.scoped(user)
        lessons=rows(db,'SELECT * FROM lessons WHERE '+clause+' AND (starts_at>? OR id IN (SELECT lesson_id FROM calendar_events WHERE user_id=?))',args+(now(),user['id']))
        integrations.google_sync(db,user,lessons)
        return {'ok':True,'count':len(lessons)},200,[]
    if path=='/api/integrations/google/disconnect' and method=='POST':
        db.execute("DELETE FROM integrations WHERE user_id=? AND provider='google'",(user['id'],))
        db.execute('DELETE FROM calendar_events WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM sync_state WHERE provider=?',('google:'+user['id'],))
        return {'ok':True},200,[]
    if path=='/api/integrations/telegram/connect' and method=='POST':
        if not integrations.integration_status(db,user)['telegram']:
            raise Problem('Telegram-бот ещё не подключён.',503)
        raw=make_token(db,user['id'],'telegram',600)
        return {'url':'https://t.me/'+os.environ['TELEGRAM_BOT_USERNAME']+'?start='+raw},200,[]
    if path=='/api/integrations/telegram/disconnect' and method=='POST':
        db.execute('UPDATE users SET telegram_chat=NULL,telegram_enabled=0 WHERE id=?',(user['id'],))
        return {'ok':True},200,[]
    if path.startswith('/api/files/') and method=='GET':
        file=one(db,'SELECT * FROM files WHERE id=?',(path.split('/')[-1],),True)
        if not service.can_read_file(db,user,file):
            raise Problem('Нет доступа к вложению.',403)
        inline=file['mime'].startswith(('audio/','image/'))
        return storage.get(file['id']),200,[('Content-Type',file['mime']),('Content-Disposition',('inline' if inline else 'attachment')+"; filename*=UTF-8''"+quote(file['filename'])),('Cache-Control','private, no-store')]
    if path.startswith('/api/export/') and method=='GET':
        kind=path.split('/')[-1]
        return service.csv_export(db,user,kind),200,[('Content-Type','text/csv; charset=utf-8'),('Content-Disposition','attachment; filename="fluent-'+kind+'.csv"')]
    if path=='/api/backups' and method=='POST':
        teacher(user)
        from manage import backup
        archive=backup(source_db=db)
        audit(db,user,'backup.created',archive.name)
        return {'url':'/api/backups/'+archive.name},200,[]
    if path.startswith('/api/backups/') and method=='GET':
        teacher(user)
        filename=path.split('/')[-1]
        if not re.fullmatch(r'fluent-[0-9T\-Z]+\.zip',filename):
            raise Problem('Архив не найден.',404)
        archive=data_dir()/'backups'/filename
        payload=storage.get('backups/'+filename) if storage.cloud() else archive.read_bytes() if archive.is_file() else None
        if payload is None: raise Problem('Архив не найден.',404)
        return payload,200,[('Content-Type','application/zip'),('Content-Disposition','attachment; filename="'+filename+'"')]
    if path=='/api/calendar.ics' and method=='GET':
        from datetime import timedelta
        from .core import dt
        clause,args=service.scoped(user)
        def esc(v): return str(v).replace('\\','\\\\').replace(';','\\;').replace(',','\\,').replace('\n','\\n').replace('\r','')
        lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//fluent//Calendar//RU','CALSCALE:GREGORIAN']
        for lesson in rows(db,"SELECT * FROM lessons WHERE "+clause+" AND status='scheduled'",args):
            start=dt(lesson['starts_at'])
            lines+=['BEGIN:VEVENT','UID:'+lesson['id']+'@fluent','DTSTAMP:'+dt(now()).strftime('%Y%m%dT%H%M%SZ'),'DTSTART:'+start.strftime('%Y%m%dT%H%M%SZ'),'DTEND:'+(start+timedelta(minutes=lesson['duration'])).strftime('%Y%m%dT%H%M%SZ'),'SUMMARY:'+esc(lesson['title']),'DESCRIPTION:'+esc(lesson['meeting_url']),'END:VEVENT']
        lines+=['END:VCALENDAR']
        return ('\r\n'.join(lines)+'\r\n').encode(),200,[('Content-Type','text/calendar; charset=utf-8'),('Content-Disposition','attachment; filename="fluent.ics"')]
    if method=='POST':
        routes={
            '/api/students':service.add_student,'/api/lessons':service.add_lesson,'/api/slots':service.add_slot,
            '/api/packages':service.save_package,'/api/payments':service.create_payment,'/api/homework':service.create_homework,
            '/api/templates':service.add_template,'/api/materials':service.add_material,'/api/goals':service.add_goal,
            '/api/progress':service.add_progress,'/api/messages':service.send_message,'/api/reports':service.create_report,
            '/api/files':service.upload,'/api/settings':service.save_settings,'/api/profile':service.save_profile}
        if path in routes:
            return routes[path](db,user,data),200,[]
        match=re.fullmatch(r'/api/(lessons|payments|homework)/([a-f0-9]{32})/(\w+)',path)
        if match:
            fn={'lessons':service.lesson_action,'payments':service.payment_action,'homework':service.homework_action}[match[1]]
            return fn(db,user,match[2],match[3],data),200,[]
        match=re.fullmatch(r'/api/(students|goals|packages|templates|reports|slots)/([a-f0-9]{32})(?:/(invite|publish|book|delete|balance))?',path)
        if match:
            kind,item_id,action=match.groups()
            if kind=='students':
                if action=='balance': return service.adjust_balance(db,user,item_id,data),200,[]
                return (service.new_invite(db,user,item_id) if action=='invite' else service.edit_student(db,user,item_id,data)),200,[]
            if kind=='goals': return service.update_goal(db,user,item_id,data),200,[]
            if kind=='packages': return service.save_package(db,user,data,item_id),200,[]
            if kind=='templates': return service.add_template(db,user,data,item_id),200,[]
            if kind=='reports' and action=='publish': return service.publish_report(db,user,item_id,data),200,[]
            if kind=='slots' and action=='book': return service.book_slot(db,user,item_id),200,[]
            if kind=='slots' and action=='delete':
                teacher(user)
                db.execute('DELETE FROM slots WHERE id=?',(item_id,))
                return {'ok':True},200,[]
        if path=='/api/notifications/read':
            db.execute('UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL',(now(),user['id']))
            return {'ok':True},200,[]
    raise Problem('Неизвестный адрес.',404)


STATIC={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/styles.css':'styles.css','/favicon.svg':'favicon.svg'}


def application(env,start_response):
    method,path=env.get('REQUEST_METHOD','GET'),env.get('PATH_INFO','/')
    headers=[('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),('X-Frame-Options','DENY'),
             ('Permissions-Policy','camera=(), microphone=(self), geolocation=()'),
             ('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"),
             ('Cache-Control','no-store')]
    if base_url().startswith('https://'):
        headers.append(('Strict-Transport-Security','max-age=31536000'))
    db=None
    try:
        if path in STATIC and method in ('GET','HEAD'):
            file=ROOT/STATIC[path]
            payload=file.read_bytes()
            status=200
            extra=[('Content-Type',mimetypes.guess_type(str(file))[0] or 'application/octet-stream')]
        elif path.startswith('/api/'):
            db=connect()
            if method=='POST':
                if not path.startswith('/api/webhooks/'):
                    origin=env.get('HTTP_ORIGIN')
                    if origin and origin!=base_url():
                        raise Problem('Недопустимый источник запроса.',403)
                data=read_body(env)
                db.execute('BEGIN IMMEDIATE')
            else:
                data={}
            result=public_route(db,env,path,method,data)
            if result is None:
                user=session_user(db,env)
                require_user(user)
                if method=='POST':
                    require_csrf(env,user)
                result=api_route(db,env,path,method,data,user)
            payload,status,extra=result
            db.commit()
            if not isinstance(payload,bytes):
                payload=json.dumps(payload,ensure_ascii=False).encode()
                extra.append(('Content-Type','application/json; charset=utf-8'))
        else:
            raise Problem('Страница не найдена.',404)
    except Problem as exc:
        if db: db.rollback()
        status,extra=exc.status,[('Content-Type','application/json; charset=utf-8')]
        payload=json.dumps({'error':exc.message},ensure_ascii=False).encode()
    except sqlite3.IntegrityError:
        if db: db.rollback()
        status,extra=409,[('Content-Type','application/json; charset=utf-8')]
        payload=json.dumps({'error':'Запись с такими данными уже существует. Обновите страницу.'},ensure_ascii=False).encode()
    except Exception as exc:
        if db: db.rollback()
        import traceback
        traceback.print_exc()
        status,extra=500,[('Content-Type','application/json; charset=utf-8')]
        payload=json.dumps({'error':'Не удалось выполнить действие. Повторите позже.'},ensure_ascii=False).encode()
    finally:
        if db: db.close()
    # Avoid duplicate response header fields when a route overrides a default.
    keys={k.lower() for k,v in extra}
    headers=[(k,v) for k,v in headers if k.lower() not in keys]+extra+[('Content-Length',str(len(payload)))]
    start_response(str(status)+' '+HTTPStatus(status).phrase,headers)
    return [b'' if method=='HEAD' else payload]
