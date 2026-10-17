"""Private Supabase objects. Credentials never leave the server.

The application authorizes a file before calling get(); buckets must stay private.
No SDK dependency is required and redirects are rejected to protect API keys.
"""
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import Problem, data_dir


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def cloud():
    return os.environ.get('FLUENT_STORAGE') == 'supabase'


def config():
    origin = os.environ.get('SUPABASE_URL', '').rstrip('/')
    parsed = urlparse(origin)
    key = os.environ.get('SUPABASE_SECRET_KEY', '')
    bucket = os.environ.get('SUPABASE_BUCKET', 'fluent-private')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.port
            or parsed.path or parsed.query or parsed.fragment
            or not parsed.hostname.endswith('.supabase.co')
            or not key.startswith('sb_secret_') or not re.fullmatch(r'[a-z0-9-]{3,63}', bucket)):
        raise Problem('Облачное хранилище не настроено. Проверьте серверные параметры.', 503)
    return origin, key, bucket


def valid_name(name):
    if not re.fullmatch(r'(?:[a-f0-9]{32}|backups/fluent-[0-9T\-Z]+\.zip)', name):
        raise ValueError('Invalid object path')
    return name


def request(method, suffix, body=None, mime='application/json', upsert=False):
    origin, key, bucket = config()
    headers = {'apikey': key, 'Content-Type': mime, 'x-upsert': 'true' if upsert else 'false'}
    req = Request(origin + '/storage/v1/' + suffix.replace('{bucket}', quote(bucket)),
                  data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(req, timeout=45) as response:
            data = response.read(128 * 1024 * 1024 + 1)
            if len(data) > 128 * 1024 * 1024:
                raise Problem('Архив превышает лимит скачивания 128 МБ.', 413)
            return data
    except HTTPError as exc:
        # Provider error bodies can contain implementation details; never log them.
        raise Problem('Файл недоступен.' if exc.code == 404 else 'Не удалось обратиться к облачному хранилищу.',
                      404 if exc.code == 404 else 503) from None
    except (URLError, TimeoutError, OSError):
        raise Problem('Облачное хранилище временно недоступно. Повторите позже.', 503) from None


def put(name, raw, mime='application/octet-stream', upsert=False):
    valid_name(name)
    if cloud():
        request('POST', 'object/{bucket}/' + name, raw, mime, upsert)
    else:
        path = data_dir() / 'uploads' / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(raw)
        path.chmod(0o600)


def get(name):
    valid_name(name)
    if cloud():
        return request('GET', 'object/authenticated/{bucket}/' + name)
    path = data_dir() / 'uploads' / name
    if not path.is_file(): raise Problem('Файл недоступен.', 404)
    return path.read_bytes()


def retain_backups(count=7):
    if not cloud(): return
    raw = request('POST', 'object/list/{bucket}', json.dumps({
        'prefix': 'backups/', 'limit': 100, 'sortBy': {'column': 'name', 'order': 'desc'}}).encode())
    entries = json.loads(raw)
    names = ['backups/' + item['name'] for item in entries
             if re.fullmatch(r'fluent-[0-9T\-Z]+\.zip', item.get('name', ''))]
    if len(names) > count:
        request('DELETE', 'object/{bucket}', json.dumps({'prefixes': names[count:]}).encode())
