"""Run web and background jobs together on a host with one persistent disk."""
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse


def hosted_config():
    origin=os.environ.get('FLUENT_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL','')
    parsed=urlparse(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.path or parsed.username:
        raise SystemExit('Укажите HTTPS-адрес приложения в FLUENT_BASE_URL.')
    if os.environ.get('FLUENT_DEMO')=='1':
        raise SystemExit('Демонстрационный вход запрещён на хостинге.')
    try:
        port=int(os.environ.get('PORT','8000'))
        if not 1024<=port<=65535: raise ValueError()
    except ValueError:
        raise SystemExit('Некорректный порт сервера.')
    if not os.environ.get('FLUENT_DATA_DIR'):
        raise SystemExit('Укажите папку постоянного диска в FLUENT_DATA_DIR.')
    os.environ['FLUENT_BASE_URL']=origin
    # Render's runtime default enables access logs, which could log OAuth codes.
    os.environ['GUNICORN_CMD_ARGS']=''
    return port


def main():
    port=hosted_config()
    from .core import initialize
    initialize()
    children=[]
    stopping=False
    def stop(signum=None,frame=None):
        nonlocal stopping
        stopping=True
        for child in children:
            if child.poll() is None: child.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    code=0
    try:
        children.append(subprocess.Popen([sys.executable,'-m','gunicorn','backend.http:application',
            '--bind','0.0.0.0:'+str(port),'--workers','2','--threads','4','--timeout','90','--error-logfile','-']))
        children.append(subprocess.Popen([sys.executable,'worker.py']))
        while not stopping:
            failed=next((child for child in children if child.poll() is not None),None)
            if failed:
                code=failed.returncode or 1
                break
            time.sleep(1)
    finally:
        stop()
        for child in children:
            try: child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return code


if __name__=='__main__': sys.exit(main())
