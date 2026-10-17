import json
import os
import sqlite3
import time
from datetime import timedelta
from .core import connect, rows, one, settings, now, dt, balance, notify, public_user
from . import integrations, service


def reminders(db):
    cfg=settings(db)
    current=dt(now())
    for lesson in rows(db,"SELECT * FROM lessons WHERE status='scheduled' AND starts_at>?",(now(),)):
        hours=(dt(lesson['starts_at'])-current).total_seconds()/3600
        if 0<hours<=cfg['reminder_hours']:
            notify(db,lesson['student_id'],'Скоро урок',lesson['title']+' · '+lesson['starts_at'],'calendar','reminder:'+lesson['id']+':'+lesson['starts_at'])
    for task in rows(db,"SELECT * FROM homework WHERE status='assigned'"):
        hours=(dt(task['due_at'])-current).total_seconds()/3600
        if 0<hours<=24:
            notify(db,task['student_id'],'Скоро срок домашнего задания',task['title'],'homework','deadline:'+task['id']+':'+task['due_at'])
    for student in rows(db,"SELECT id FROM users WHERE role='student' AND active=1"):
        value=balance(db,student['id'])
        if value<=cfg['low_balance']:
            last=one(db,'SELECT id FROM ledger WHERE student_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(student['id'],))
            notify(db,student['id'],'Пора пополнить баланс','Осталось занятий: '+str(value),'payments','balance:'+(last['id'] if last else 'initial'))
    db.execute('DELETE FROM sessions WHERE expires_at<?',(time.time(),))
    db.execute('DELETE FROM tokens WHERE expires_at<?',(time.time(),))
    db.execute('DELETE FROM rate_limits WHERE expires_at<?',(time.time(),))


def deliver_one():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        job=one(db,"SELECT * FROM outbox WHERE (state='pending' OR (state='sending' AND next_attempt<?)) AND next_attempt<=? ORDER BY rowid LIMIT 1",(time.time(),time.time()))
        if not job: return False
        user=one(db,'SELECT * FROM users WHERE id=?',(job['user_id'],),True)
        db.execute("UPDATE outbox SET state='sending',next_attempt=? WHERE id=?",(time.time()+120,job['id']))
    try:
        if not user['active']:
            state='skipped'
        elif job['channel']=='telegram':
            if not user['telegram_enabled'] or not user['telegram_chat']:
                state='skipped'
            else:
                integrations.send_telegram(user['telegram_chat'],job['subject']+'\n'+job['body'])
                state='sent'
        elif user['email_enabled'] or job['event_key'].startswith('reset:'):
            integrations.send_email(user['email'],job['subject'],job['body'])
            state='sent'
        else:
            state='skipped'
        with connect() as db:
            db.execute('UPDATE outbox SET state=?,error=? WHERE id=?',(state,'',job['id']))
    except Exception:
        attempts=job['attempts']+1
        with connect() as db:
            db.execute('UPDATE outbox SET state=?,attempts=?,next_attempt=?,error=? WHERE id=?',('failed' if attempts>=6 else 'pending',attempts,time.time()+min(3600,60*2**attempts),'Не удалось доставить уведомление.',job['id']))
    return True


def sync_calendars():
    with connect() as db:
        users=rows(db,"SELECT u.* FROM users u JOIN integrations i ON i.user_id=u.id WHERE i.provider='google' AND u.active=1")
    for user in users:
        try:
            with connect() as db:
                clause,args=service.scoped(user)
                # Mapped events stay eligible so cancellations propagate even after a long outage.
                lessons=rows(db,'SELECT * FROM lessons WHERE '+clause+' AND (starts_at>? OR id IN (SELECT lesson_id FROM calendar_events WHERE user_id=?))',args+(now(),user['id']))
                integrations.google_sync(db,user,lessons)
        except Exception:
            with connect() as db:
                notify(db,user['id'],'Не удалось обновить Google Calendar','Проверьте подключение в настройках.','settings','google-error:'+now()[:10])


def sync_zoom():
    if not integrations.configured('ZOOM_ACCOUNT_ID','ZOOM_CLIENT_ID','ZOOM_CLIENT_SECRET'): return
    with connect() as db:
        lessons=rows(db,'SELECT * FROM lessons WHERE zoom_id IS NOT NULL AND starts_at>?',(now(),))
    for lesson in lessons:
        try:
            fingerprint=integrations.zoom_fingerprint(lesson)
            with connect() as db:
                state=one(db,"SELECT fingerprint FROM sync_state WHERE provider='zoom' AND entity_id=?",(lesson['id'],))
            if state and state['fingerprint']==fingerprint:
                continue
            integrations.zoom_meeting(lesson,cancel=lesson['status'] in ('cancelled','late_cancelled'))
            with connect() as db:
                if lesson['status'] in ('cancelled','late_cancelled'):
                    db.execute('UPDATE lessons SET zoom_id=NULL,meeting_url=? WHERE id=?',('',lesson['id']))
                db.execute("INSERT INTO sync_state VALUES ('zoom',?,?) ON CONFLICT(provider,entity_id) DO UPDATE SET fingerprint=excluded.fingerprint",(lesson['id'],fingerprint))
        except Exception:
            from .core import notify_teacher
            with connect() as db:
                notify_teacher(db,'Не удалось обновить Zoom','Проверьте встречу и подключение Zoom в настройках.','settings','zoom-error:'+lesson['id']+':'+now()[:10])


def run_once(sync=False):
    with connect() as db:
        reminders(db)
    for _ in range(20):
        if not deliver_one(): break
    if sync:
        sync_calendars()
        sync_zoom()
