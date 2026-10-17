"""Run exactly one worker instance for deliveries, calendar synchronization and backups."""
import fcntl
import time
from backend.core import initialize,data_dir,now
from backend.jobs import run_once
from manage import backup
from backend import storage


def main():
    initialize()
    data_dir().mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(data_dir()/'worker.lock').open('w')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Фоновый процесс уже запущен.')
    count=0
    while True:
        try:
            run_once(sync=count%5==0)
            folder=data_dir()/'backups'
            today=now()[:10]
            existing=list(folder.glob('fluent-'+today+'*.zip')) if folder.exists() else []
            if not existing:
                # Free storage: keep seven database-only snapshots. The originals
                # stay in the private bucket; manual archives also include files.
                backup(include_files=not storage.cloud())
            # Seven recent automatic archives. User-created archives outside this folder are unaffected.
            if folder.exists():
                for old in sorted(folder.glob('fluent-*.zip'),reverse=True)[7:]:
                    old.unlink()
        except Exception:
            print('Фоновая задача не завершена, повтор через минуту.',flush=True)
        count+=1
        time.sleep(60)


if __name__=='__main__': main()
