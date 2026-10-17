"""Production entry point. TLS is terminated by the reverse proxy."""
import os
from urllib.parse import urlparse
from .core import base_url, initialize


def main():
    parsed=urlparse(base_url())
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.username:
        raise SystemExit('Задайте FLUENT_BASE_URL=https://ваш-домен без пути.')
    if os.environ.get('FLUENT_DEMO')=='1':
        raise SystemExit('Демонстрационный вход запрещён в production.')
    initialize()
    os.execvp('gunicorn',['gunicorn','backend.http:application','--bind','0.0.0.0:8000','--workers','2','--threads','4','--timeout','90','--error-logfile','-'])


if __name__=='__main__': main()
