import argparse
import getpass
import json
import os
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from backend.core import initialize, connect, uid, now, rows, one, insert, password_hash, data_dir, email, make_token, base_url


def create_owner(address,name):
    password=getpass.getpass('Пароль (минимум 12 символов): ')
    if password!=getpass.getpass('Повторите пароль: '):
        raise SystemExit('Пароли не совпали.')
    with connect() as db:
        if one(db,"SELECT id FROM users WHERE role='teacher'"):
            raise SystemExit('Преподаватель уже создан. Используйте reset-owner.')
        insert(db,'users',id=uid(),email=email(address),name=name,role='teacher',password_hash=password_hash(password),created_at=now())
    print('Кабинет преподавателя создан.')


def backup(destination=None):
    folder=data_dir()
    target=Path(destination) if destination else folder/'backups'/('fluent-'+now().replace(':','-')+'.zip')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.exists():
        raise ValueError('Архив уже существует.')
    with tempfile.TemporaryDirectory(prefix='fluent-backup-') as scratch:
        snapshot=Path(scratch)/'fluent.sqlite3'
        source=connect()
        copy=sqlite3.connect(str(snapshot))
        try:
            source.backup(copy)
            # Sessions and provider tokens must be reissued after a restore.
            copy.execute('DELETE FROM sessions')
            copy.execute('DELETE FROM tokens')
            copy.execute('DELETE FROM integrations')
            copy.execute('DELETE FROM calendar_events')
            copy.execute('DELETE FROM sync_state')
            copy.commit()
            copy.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            source.close()
            copy.close()
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot,'fluent.sqlite3')
            uploads=folder/'uploads'
            if uploads.exists():
                for file in uploads.iterdir():
                    if file.is_file(): archive.write(file,'uploads/'+file.name)
    target.chmod(0o600)
    return target


def restore(archive,destination):
    target=Path(destination).resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit('Восстановление возможно только в пустую новую папку.')
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            path=Path(item.filename)
            allowed=item.filename=='fluent.sqlite3' or (len(path.parts)==2 and path.parts[0]=='uploads' and len(path.name)==32 and all(c in '0123456789abcdef' for c in path.name))
            if not allowed or item.file_size>2*1024*1024*1024:
                raise SystemExit('В архиве есть неподдерживаемые пути или файлы.')
        if 'fluent.sqlite3' not in source.namelist():
            raise SystemExit('База в архиве не найдена.')
        target.mkdir(parents=True,exist_ok=True,mode=0o700)
        source.extractall(target)
    print('Восстановлено в '+str(target)+'. Задайте FLUENT_DATA_DIR и заново подключите Google Calendar.')


def seed_demo():
    # This function is never called during production initialization.
    with connect() as db:
        if one(db,'SELECT id FROM users LIMIT 1'): return
        owner=uid()
        insert(db,'users',id=owner,email='teacher@demo.local',name='Анна Морозова',role='teacher',password_hash=password_hash('Fluent-demo-only-2026'),created_at=now())
        names=[('Екатерина Волкова','B2','Business English','student@demo.local'),('Михаил Иванов','B1','Путешествия','mikhail@demo.local'),('Алиса Смирнова','C1','IELTS 7.5','alice@demo.local'),('Даниил Петров','A2','Разговорная практика','daniil@demo.local')]
        people=[]
        for idx,(name,level,focus,addr) in enumerate(names):
            person=uid();people.append(person)
            insert(db,'users',id=person,email=addr,name=name,role='student',password_hash=password_hash('Fluent-demo-only-2026'),created_at=now())
            insert(db,'students',user_id=person,level=level,focus=focus,referral_code=secrets.token_hex(4).upper())
            insert(db,'ledger',id=uid(),student_id=person,delta=[6,2,5,1][idx],reason='Стартовый демобаланс',source='demo:'+person,created_at=now())
        for count,amount,title in [(4,600000,'В своём ритме'),(8,1120000,'Уверенный прогресс'),(12,1560000,'Полное погружение')]:
            insert(db,'packages',id=uid(),name=title,lessons=count,amount=amount,description='Индивидуальные занятия по 50 минут')
        current=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
        for idx in range(8):
            at=current+timedelta(hours=2+idx*3)
            insert(db,'lessons',id=uid(),student_id=people[idx%4],title=['Business English · Negotiations','Travel stories','IELTS · Writing task 2','Food & culture'][idx%4],starts_at=at.strftime('%Y-%m-%dT%H:%M:%SZ'),duration=50,created_at=now(),updated_at=now())
        for idx in range(3):
            at=current+timedelta(days=3,hours=idx*2)
            insert(db,'slots',id=uid(),starts_at=at.strftime('%Y-%m-%dT%H:%M:%SZ'),duration=50)
        for idx in range(3):
            task_id=uid()
            insert(db,'homework',id=task_id,student_id=people[idx],title=['Opinion essay','My perfect trip','An interview with your future self'][idx],kind=['Writing','Speaking','Speaking'][idx],instructions='Расскажите о своём опыте и приведите 2–3 примера. Используйте выражения с последнего урока. Можно отправить текст или голосовую запись.',due_at=(current+timedelta(days=2+idx)).strftime('%Y-%m-%dT%H:%M:%SZ'),status='submitted' if idx==1 else 'assigned',answer='I would love to visit a new city and explore its culture.' if idx==1 else '',created_at=now())
        for kind,title,content in [('Speaking','60-second story','Запишите историю длиной 60–90 секунд. Используйте три выражения с урока.'),('Writing','Opinion essay','Напишите 150–180 слов. Сформулируйте мнение, приведите аргументы и сделайте вывод.'),('Listening','Listen & reflect','Послушайте материал дважды. Кратко перескажите основную мысль и выпишите три вопроса.')]:
            insert(db,'templates',id=uid(),title=title,kind=kind,instructions=content,created_at=now())
        insert(db,'goals',id=uid(),student_id=people[0],title='Уверенно выступить на встрече',target=8,progress=3,unit='практик',deadline=(current+timedelta(days=30)).strftime('%Y-%m-%d'),created_at=now())
        for idx,skill in enumerate(['Speaking','Listening','Reading','Writing']):
            insert(db,'progress_notes',id=uid(),student_id=people[0],skill=skill,score=55+idx*8,note='Стартовая оценка навыка.',created_at=now())
        insert(db,'materials',id=uid(),title='Как готовиться к speaking',body='Выберите одну тему. Две минуты говорите без остановки, затем прослушайте запись и отметьте одно улучшение. Не стремитесь исправить всё сразу.',created_at=now())
        insert(db,'messages',id=uid(),student_id=people[0],author_id=owner,body='Добро пожаловать! Здесь будут уроки, задания и наши заметки. Начнём с небольшой цели на месяц.',created_at=now())
    print('Созданы демонстрационные данные. Они отделены от рабочей базы.')


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    own=sub.add_parser('owner');own.add_argument('--email',required=True);own.add_argument('--name',required=True)
    reset=sub.add_parser('reset-owner');reset.add_argument('--email',required=True)
    backup_parser=sub.add_parser('backup');backup_parser.add_argument('--output')
    restore_parser=sub.add_parser('restore');restore_parser.add_argument('archive');restore_parser.add_argument('--destination',required=True)
    sub.add_parser('telegram-webhook')
    args=parser.parse_args()
    initialize()
    if args.command=='owner': create_owner(args.email,args.name)
    elif args.command=='reset-owner':
        with connect() as db:
            user=one(db,"SELECT * FROM users WHERE email=? AND role='teacher'",(email(args.email),),True)
            value=password_hash(getpass.getpass('Новый пароль: '))
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(value,user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
    elif args.command=='backup': print(backup(args.output))
    elif args.command=='restore': restore(args.archive,args.destination)
    elif args.command=='telegram-webhook':
        from backend.integrations import request_json,configured
        if not configured('TELEGRAM_BOT_TOKEN','TELEGRAM_WEBHOOK_SECRET') or not base_url().startswith('https://'):
            raise SystemExit('Настройте токен бота, секрет webhook и HTTPS-адрес приложения.')
        result=request_json('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/setWebhook',{'url':base_url()+'/api/webhooks/telegram','secret_token':os.environ['TELEGRAM_WEBHOOK_SECRET'],'allowed_updates':['message']})
        print('Webhook установлен.' if result.get('ok') else 'Не удалось настроить webhook.')


if __name__=='__main__': main()
