import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def data_dir():
    return Path(os.environ.get('FLUENT_DATA_DIR', str(ROOT / 'data'))).resolve()


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def uid():
    return secrets.token_hex(16)


def dt(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise Problem('Укажите дату, время и часовой пояс.')


def iso(value):
    return dt(value).strftime('%Y-%m-%dT%H:%M:%SZ')


def integer(value, low=0, high=100000000):
    try:
        if isinstance(value, bool) or str(int(value)) != str(value):
            raise ValueError()
        result = int(value)
        if not low <= result <= high:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise Problem('Некорректное число.')


def text(value, limit=10000, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise Problem('Проверьте длину текстового поля.')
    value = value.strip()
    if required and not value:
        raise Problem('Заполните обязательные поля.')
    return value


def email(value):
    value = text(value, 254, True).lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise Problem('Проверьте email.')
    return value


def safe_url(value):
    from urllib.parse import urlparse
    value = text(value, 2000)
    if value and (urlparse(value).scheme != 'https' or not urlparse(value).hostname or urlparse(value).username):
        raise Problem('Нужна ссылка, начинающаяся с https://.')
    return value


def validate_zone(value):
    try:
        ZoneInfo(value)
        return value
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise Problem('Неизвестный часовой пояс.')


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 200:
        raise Problem('Пароль должен содержать от 12 до 200 символов.')
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
    return 'pbkdf2_sha256$600000$' + salt.hex() + '$' + key.hex()


def verify_password(password, encoded):
    if not encoded or not isinstance(password, str) or len(password) > 200:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 600000 <= int(rounds) <= 2000000:
            return False
        value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def connect():
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(str(folder / 'fluent.sqlite3'), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(db, sql, args=(), required=False):
    row = db.execute(sql, args).fetchone()
    if row is None and required:
        raise Problem('Запись не найдена.', 404)
    return dict(row) if row else None


def insert(db, table, **values):
    # All table and column names are constants supplied by application code.
    db.execute('INSERT INTO '+table+' ('+','.join(values)+') VALUES ('+','.join('?' for _ in values)+')', tuple(values.values()))
    return values


DEFAULTS = {'studio_name': 'fluent.', 'teacher_name': 'Преподаватель', 'cancel_hours': 24,
            'late_cancel_charge': True, 'referral_bonus': 1, 'referral_enabled': False,
            'payment_mode': 'manual_sbp', 'sbp_url': '', 'sbp_qr_file_id': '',
            'reminder_hours': 24, 'low_balance': 2, 'currency': 'RUB'}


def initialize():
    with connect() as db:
        db.executescript((ROOT / 'backend/schema.sql').read_text())
        for key, value in DEFAULTS.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))


def settings(db):
    return {r['key']: json.loads(r['value']) for r in rows(db, 'SELECT * FROM settings')}


def teacher(user):
    if user['role'] != 'teacher':
        raise Problem('Доступно только преподавателю.', 403)


def owns(user, student_id):
    if user['role'] != 'teacher' and user['id'] != student_id:
        raise Problem('Нет доступа к данным другого ученика.', 403)


def student_exists(db, student_id):
    return one(db, "SELECT id,name,email FROM users WHERE id=? AND role='student'", (student_id,), True)


def balance(db, student_id):
    return db.execute('SELECT COALESCE(SUM(delta),0) FROM ledger WHERE student_id=?', (student_id,)).fetchone()[0]


def audit(db, user, action, entity):
    insert(db, 'audit', id=uid(), actor_id=user['id'] if user else None, action=action, entity_id=entity, created_at=now())


def notify(db, user_id, title, body, route, event_key):
    key = user_id + ':' + event_key
    if one(db, 'SELECT id FROM notifications WHERE event_key=?', (key,)):
        return
    insert(db, 'notifications', id=uid(), user_id=user_id, title=title, body=body, route=route, event_key=key, created_at=now())
    user = one(db, 'SELECT * FROM users WHERE id=?', (user_id,), True)
    for channel, enabled in [('telegram', user['telegram_enabled'] and user['telegram_chat']), ('email', user['email_enabled'])]:
        if enabled:
            insert(db, 'outbox', id=uid(), user_id=user_id, channel=channel, subject=title, body=body, event_key=key+':'+channel)


def notify_teacher(db, title, body, route, key):
    for user in rows(db, "SELECT id FROM users WHERE role='teacher' AND active=1"):
        notify(db, user['id'], title, body, route, key)


def make_token(db, user_id, kind, seconds=86400, payload=None):
    raw = secrets.token_urlsafe(32)
    insert(db, 'tokens', token_hash=digest(raw), user_id=user_id, kind=kind,
           expires_at=time.time()+seconds, payload=json.dumps(payload or {}))
    return raw


def token_record(db, raw, kind):
    if not isinstance(raw, str):
        raise Problem('Недействительная ссылка.', 400)
    row = one(db, 'SELECT * FROM tokens WHERE token_hash=? AND kind=? AND expires_at>?', (digest(raw), kind, time.time()))
    if not row:
        raise Problem('Ссылка недействительна или устарела. Запросите новую.', 400)
    return row


def base_url():
    return os.environ.get('FLUENT_BASE_URL', 'http://127.0.0.1:4174').rstrip('/')


def public_user(user):
    return {key: user[key] for key in ['id', 'email', 'name', 'role', 'timezone', 'telegram_enabled', 'email_enabled']}
