"""Local development runner. Production uses the supplied Docker/Gunicorn configuration."""
import argparse
import os
import threading
import time
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from backend.core import ROOT, initialize


def main():
    parser=argparse.ArgumentParser(description='fluent studio')
    parser.add_argument('--port',type=int,default=4174)
    parser.add_argument('--demo',action='store_true')
    args=parser.parse_args()
    os.environ.setdefault('FLUENT_BASE_URL','http://127.0.0.1:'+str(args.port))
    if args.demo:
        os.environ['FLUENT_DEMO']='1'
        os.environ['FLUENT_DATA_DIR']=str(ROOT/'data-demo')
    initialize()
    if args.demo:
        from manage import seed_demo
        seed_demo()
    from backend.http import application
    from backend.jobs import run_once
    def jobs():
        count=0
        while True:
            try:
                run_once(sync=count%5==0)
            except Exception:
                print('Фоновые задачи: повтор через минуту.',flush=True)
            count+=1
            time.sleep(60)
    threading.Thread(target=jobs,daemon=True).start()
    class ThreadedServer(ThreadingMixIn,WSGIServer):
        daemon_threads=True
    class QuietHandler(WSGIRequestHandler):
        def log_message(self,format,*args):
            # Do not log query strings that may contain OAuth codes.
            pass
    print('fluent: http://127.0.0.1:'+str(args.port),flush=True)
    with make_server('127.0.0.1',args.port,application,ThreadedServer,QuietHandler) as server:
        server.serve_forever()


if __name__=='__main__': main()
