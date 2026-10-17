"""Outbound adapters. Credentials are read only on the server, never returned to the UI."""
import base64
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .core import Problem, base_url, one, rows, now, dt


def request_json(url, data=None, headers=None, method=None, form=False):
    headers = dict(headers or {})
    payload = None
    if data is not None:
        payload = urlencode(data).encode() if form else json.dumps(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    req = Request(url, data=payload, headers=headers, method=method or ('POST' if payload is not None else 'GET'))
    try:
        with urlopen(req, timeout=15) as response:
            body = response.read(3 * 1024 * 1024)
            return json.loads(body) if body else {}
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        # Never send provider response bodies or URLs containing credentials to a client/log.
        raise Problem('Внешний сервис временно недоступен. Повторите позже.', 502) from exc


def configured(*keys):
    return all(os.environ.get(key) for key in keys)


def integration_status(db, user):
    google = one(db, "SELECT user_id FROM integrations WHERE user_id=? AND provider='google'", (user['id'],))
    return {'zoom': configured('ZOOM_ACCOUNT_ID', 'ZOOM_CLIENT_ID', 'ZOOM_CLIENT_SECRET'),
            'google': configured('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'), 'google_connected': bool(google),
            'telegram': configured('TELEGRAM_BOT_TOKEN', 'TELEGRAM_BOT_USERNAME', 'TELEGRAM_WEBHOOK_SECRET'),
            'telegram_connected': bool(user.get('telegram_chat')), 'email': configured('SMTP_HOST', 'SMTP_FROM')}


def zoom_meeting(lesson, cancel=False):
    if not configured('ZOOM_ACCOUNT_ID', 'ZOOM_CLIENT_ID', 'ZOOM_CLIENT_SECRET'):
        raise Problem('Zoom ещё не подключён. Можно вставить готовую ссылку на встречу.', 503)
    auth = base64.b64encode((os.environ['ZOOM_CLIENT_ID']+':'+os.environ['ZOOM_CLIENT_SECRET']).encode()).decode()
    token = request_json('https://zoom.us/oauth/token', {'grant_type': 'account_credentials', 'account_id': os.environ['ZOOM_ACCOUNT_ID']}, {'Authorization': 'Basic '+auth}, form=True)['access_token']
    headers = {'Authorization': 'Bearer '+token}
    if cancel:
        if lesson.get('zoom_id'):
            request_json('https://api.zoom.us/v2/meetings/'+quote(lesson['zoom_id'], safe=''), headers=headers, method='DELETE')
        return {}
    payload = {'topic': lesson['title'], 'type': 2, 'start_time': lesson['starts_at'], 'duration': lesson['duration'], 'timezone': 'UTC',
               'settings': {'waiting_room': True, 'join_before_host': False}}
    if lesson.get('zoom_id'):
        request_json('https://api.zoom.us/v2/meetings/'+quote(lesson['zoom_id'], safe=''), payload, headers, method='PATCH')
        return {'id': lesson['zoom_id'], 'join_url': lesson['meeting_url']}
    host = os.environ.get('ZOOM_USER_ID', 'me')
    return request_json('https://api.zoom.us/v2/users/'+quote(host, safe='')+'/meetings', payload, headers)


def zoom_fingerprint(lesson):
    return hashlib.sha256(json.dumps({k:lesson[k] for k in ['title','starts_at','duration','status','zoom_id']},sort_keys=True).encode()).hexdigest()


GOOGLE_SCOPE = 'https://www.googleapis.com/auth/calendar.events.owned'


def google_auth_url(state):
    if not configured('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'):
        raise Problem('Google Calendar ещё не настроен на сервере.', 503)
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
        'client_id': os.environ['GOOGLE_CLIENT_ID'], 'redirect_uri': base_url()+'/api/integrations/google/callback',
        'response_type': 'code', 'scope': GOOGLE_SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state})


def google_exchange(code):
    return request_json('https://oauth2.googleapis.com/token', {
        'code': code, 'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
        'redirect_uri': base_url()+'/api/integrations/google/callback', 'grant_type': 'authorization_code'}, form=True)


def google_access(db, user_id):
    row = one(db, "SELECT data FROM integrations WHERE user_id=? AND provider='google'", (user_id,), True)
    data = json.loads(row['data'])
    if data.get('expires_at', 0) < time.time()+90:
        fresh = request_json('https://oauth2.googleapis.com/token', {
            'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
            'refresh_token': data['refresh_token'], 'grant_type': 'refresh_token'}, form=True)
        data.update(fresh)
        data['expires_at'] = time.time()+fresh['expires_in']
        db.execute("UPDATE integrations SET data=? WHERE user_id=? AND provider='google'", (json.dumps(data), user_id))
    return data['access_token']


def google_sync(db, user, lessons):
    from datetime import timedelta
    headers = {'Authorization': 'Bearer '+google_access(db, user['id'])}
    for lesson in lessons:
        event = one(db, 'SELECT * FROM calendar_events WHERE user_id=? AND lesson_id=?', (user['id'], lesson['id']))
        # Deterministic IDs avoid duplicate calendar events on timeout/retry.
        event_id = 'f'+hashlib.sha256((user['id']+lesson['id']).encode()).hexdigest()
        endpoint = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
        if lesson['status'] in ('cancelled', 'late_cancelled') and not event:
            continue
        body = {'summary': lesson['title'], 'description': 'fluent. '+lesson.get('meeting_url', ''),
                'start': {'dateTime': lesson['starts_at']},
                'end': {'dateTime': (dt(lesson['starts_at'])+timedelta(minutes=lesson['duration'])).isoformat()},
                'status': 'cancelled' if lesson['status'] in ('cancelled', 'late_cancelled') else 'confirmed'}
        provider='google:'+user['id']
        fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        state=one(db,'SELECT fingerprint FROM sync_state WHERE provider=? AND entity_id=?',(provider,lesson['id']))
        if state and state['fingerprint']==fingerprint:
            continue
        if event:
            request_json(endpoint+'/'+event_id, body, headers, method='PATCH')
        else:
            body['id'] = event_id
            try:
                request_json(endpoint, body, headers)
            except Problem:
                # Recover a previous successful insert whose response was lost.
                request_json(endpoint+'/'+event_id, body, headers, method='PUT')
            db.execute('INSERT INTO calendar_events VALUES (?,?,?) ON CONFLICT(user_id,lesson_id) DO NOTHING', (user['id'], lesson['id'], event_id))
        db.execute('INSERT INTO sync_state VALUES (?,?,?) ON CONFLICT(provider,entity_id) DO UPDATE SET fingerprint=excluded.fingerprint',(provider,lesson['id'],fingerprint))


def send_telegram(chat_id, message):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise Problem('Telegram не настроен.', 503)
    result = request_json('https://api.telegram.org/bot'+token+'/sendMessage', {'chat_id': chat_id, 'text': message, 'disable_web_page_preview': True})
    if not result.get('ok'):
        raise Problem('Telegram не принял сообщение.', 502)


def send_email(address, subject, body):
    if not configured('SMTP_HOST', 'SMTP_FROM'):
        raise Problem('Почта ещё не подключена.', 503)
    mail = EmailMessage()
    mail['From'] = os.environ['SMTP_FROM']
    mail['To'] = address
    mail['Subject'] = subject
    mail.set_content(body)
    host, port = os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '587'))
    client = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) if port == 465 else smtplib.SMTP(host, port, timeout=15)
    with client:
        if port != 465:
            client.starttls(context=ssl.create_default_context())
        if os.environ.get('SMTP_USER'):
            client.login(os.environ['SMTP_USER'], os.environ.get('SMTP_PASSWORD', ''))
        client.send_message(mail)
