import base64
import binascii
import csv
import io
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (Problem, text, integer, email, safe_url, validate_zone, uid, now, dt, iso,
                   rows, one, insert, settings, teacher, owns, student_exists, balance,
                   audit, notify, notify_teacher, make_token, base_url, public_user, data_dir)
from . import integrations


def scoped(user, column='student_id'):
    return ('1=1', ()) if user['role'] == 'teacher' else (column+'=?', (user['id'],))


def bootstrap(db, user):
    clause, args = scoped(user)
    cfg = settings(db)
    cfg['referral_enabled'], cfg['referral_bonus'] = False, 0
    people = rows(db, "SELECT u.id,u.name,u.email,u.timezone,u.active,s.level,s.focus,s.private_notes,s.referral_code,s.referrer_id FROM users u JOIN students s ON s.user_id=u.id WHERE "+('1=1' if user['role']=='teacher' else 'u.id=?')+' ORDER BY u.name', args)
    for person in people:
        person['balance'] = balance(db, person['id'])
        if user['role'] != 'teacher':
            person.pop('private_notes', None)
            person.pop('referrer_id', None)
    lessons = rows(db, 'SELECT * FROM lessons WHERE '+clause+' ORDER BY starts_at', args)
    payments = rows(db, 'SELECT * FROM payments WHERE '+clause+' ORDER BY created_at DESC', args)
    for payment in payments:
        for key in ['request_key', 'provider_id', 'qr_svg', 'error']:
            payment.pop(key, None)
    import os
    result = {'hosting': {'free': os.environ.get('FLUENT_HOSTING')=='free'},
              'user': public_user(user), 'settings': cfg, 'students': people,
              'teachers': rows(db, "SELECT id,name FROM users WHERE role='teacher'"),
              'lessons': lessons, 'slots': rows(db, 'SELECT * FROM slots WHERE starts_at>? ORDER BY starts_at', (now(),)),
              'payments': payments, 'packages': rows(db, 'SELECT * FROM packages '+('' if user['role']=='teacher' else 'WHERE active=1')+' ORDER BY lessons'),
              'ledger': rows(db, 'SELECT * FROM ledger WHERE '+clause+' ORDER BY created_at DESC', args),
              'homework': rows(db, 'SELECT * FROM homework WHERE '+clause+' ORDER BY due_at', args),
              'goals': rows(db, 'SELECT * FROM goals WHERE '+clause+' ORDER BY created_at DESC', args),
              'progress': rows(db, 'SELECT * FROM progress_notes WHERE '+clause+' ORDER BY created_at', args),
              'materials': rows(db, 'SELECT * FROM materials WHERE '+clause+' OR student_id IS NULL ORDER BY created_at DESC', args),
              'messages': rows(db, 'SELECT * FROM messages WHERE '+clause+' ORDER BY created_at', args),
              'reports': rows(db, 'SELECT * FROM reports WHERE '+clause+('' if user['role']=='teacher' else ' AND published_at IS NOT NULL')+' ORDER BY month DESC', args),
              'notifications': rows(db, 'SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100', (user['id'],)),
              'integrations': integrations.integration_status(db, user),
              'templates': rows(db, 'SELECT * FROM templates ORDER BY title') if user['role']=='teacher' else [],
              'referrals': [], 'files': []}
    for file in rows(db, 'SELECT id,owner_id,student_id,entity_type,entity_id,filename,mime,size,created_at FROM files'):
        if can_read_file(db, user, file):
            result['files'].append(file)
    if user['role'] == 'teacher':
        result['deliveries'] = rows(db, 'SELECT channel,state,COUNT(*) AS count FROM outbox GROUP BY channel,state')
    result['referrals'] = []
    result['notification_counts'] = notification_counts(db, user)
    result['notifications'] = [n for n in result['notifications'] if n['route'] != 'referrals']
    return result


def add_student(db, user, data):
    teacher(user)
    addr, name = email(data.get('email')), text(data.get('name'), 100, True)
    if one(db, 'SELECT id FROM users WHERE email=?', (addr,)):
        raise Problem('Ученик с таким email уже существует.', 409)
    referrer = None
    student_id = uid()
    insert(db, 'users', id=student_id, email=addr, name=name, role='student', timezone=validate_zone(data.get('timezone', 'Europe/Moscow')), created_at=now())
    insert(db, 'students', user_id=student_id, level=text(data.get('level', 'B1'), 20), focus=text(data.get('focus', ''), 1000),
           private_notes=text(data.get('private_notes', ''), 5000), referral_code=secrets.token_hex(4).upper(), referrer_id=referrer)
    token = make_token(db, student_id, 'invite', 7*86400)
    audit(db, user, 'student.created', student_id)
    return {'id': student_id, 'invite_url': base_url()+'/#invite='+token}


def new_invite(db, user, student_id):
    teacher(user)
    student_exists(db, student_id)
    db.execute("DELETE FROM tokens WHERE user_id=? AND kind='invite'", (student_id,))
    return {'invite_url': base_url()+'/#invite='+make_token(db, student_id, 'invite', 7*86400)}


def edit_student(db, user, student_id, data):
    teacher(user)
    student_exists(db, student_id)
    db.execute('UPDATE users SET name=?,timezone=?,active=? WHERE id=?', (text(data.get('name'), 100, True), validate_zone(data.get('timezone', 'Europe/Moscow')), int(bool(data.get('active', True))), student_id))
    db.execute('UPDATE students SET level=?,focus=?,private_notes=? WHERE user_id=?', (text(data.get('level', ''), 20), text(data.get('focus', ''), 1000), text(data.get('private_notes', ''), 5000), student_id))
    if not data.get('active', True):
        db.execute('DELETE FROM sessions WHERE user_id=?', (student_id,))
    audit(db, user, 'student.updated', student_id)
    return {'ok': True}


def no_conflict(db, starts_at, duration, exclude=None):
    start, end = dt(starts_at), dt(starts_at)+timedelta(minutes=duration)
    for lesson in rows(db, "SELECT id,starts_at,duration FROM lessons WHERE status='scheduled' AND id<>?", (exclude or '',)):
        other = dt(lesson['starts_at'])
        if start < other+timedelta(minutes=lesson['duration']) and end > other:
            raise Problem('Это время уже занято другим уроком.', 409)


def consume_slots(db, starts_at, duration):
    start,end=dt(starts_at),dt(starts_at)+timedelta(minutes=duration)
    for slot in rows(db,'SELECT * FROM slots'):
        other=dt(slot['starts_at'])
        if start<other+timedelta(minutes=slot['duration']) and end>other:
            db.execute('DELETE FROM slots WHERE id=?',(slot['id'],))


def add_lesson(db, user, data):
    teacher(user)
    student_id = data.get('student_id')
    student_exists(db, student_id)
    start, duration = iso(data.get('starts_at')), integer(data.get('duration', 50), 15, 240)
    if dt(start) < dt(now()):
        raise Problem('Новый урок должен быть в будущем.')
    title, link = text(data.get('title'), 150, True), safe_url(data.get('meeting_url', ''))
    repeat = integer(data.get('repeat', 1), 1, 26)
    ids = []
    from zoneinfo import ZoneInfo
    local_start=dt(start).astimezone(ZoneInfo(user['timezone']))
    for week in range(repeat):
        # Preserve local wall-clock time across daylight-saving transitions.
        at = (local_start+timedelta(weeks=week)).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        no_conflict(db, at, duration)
        lesson_id = uid()
        insert(db, 'lessons', id=lesson_id, student_id=student_id, title=title, starts_at=at, duration=duration, meeting_url=link, created_at=now(), updated_at=now())
        consume_slots(db,at,duration)
        ids.append(lesson_id)
    notify(db, student_id, 'В расписании новый урок', title, 'calendar', 'lesson.created:'+ids[0])
    audit(db, user, 'lesson.created', ids[0])
    return {'ids': ids}


def add_slot(db, user, data):
    teacher(user)
    start, duration = iso(data.get('starts_at')), integer(data.get('duration', 50), 15, 240)
    if dt(start) <= dt(now()):
        raise Problem('Свободное окно должно быть в будущем.')
    no_conflict(db, start, duration)
    slot = insert(db, 'slots', id=uid(), starts_at=start, duration=duration)
    return slot


def book_slot(db, user, slot_id):
    if user['role'] != 'student':
        raise Problem('Записаться может ученик.')
    slot = one(db, 'SELECT * FROM slots WHERE id=?', (slot_id,), True)
    if dt(slot['starts_at']) <= dt(now()):
        raise Problem('Окно уже прошло.')
    if balance(db, user['id']) <= 0:
        raise Problem('Сначала пополните баланс занятий.')
    no_conflict(db, slot['starts_at'], slot['duration'])
    lesson_id = uid()
    insert(db, 'lessons', id=lesson_id, student_id=user['id'], title='Индивидуальный урок', starts_at=slot['starts_at'], duration=slot['duration'], created_at=now(), updated_at=now())
    consume_slots(db,slot['starts_at'],slot['duration'])
    notify_teacher(db, 'Запись на урок', user['name']+' записался на занятие.', 'calendar', 'book:'+lesson_id)
    notify(db, user['id'], 'Вы записаны на урок', 'Занятие появилось в расписании.', 'calendar', 'book:'+lesson_id)
    return {'id': lesson_id}


def lesson_action(db, user, lesson_id, action, data):
    if action == 'reschedule':
        teacher(user)
    lesson = one(db, 'SELECT * FROM lessons WHERE id=?', (lesson_id,), True)
    owns(user, lesson['student_id'])
    if action == 'link':
        teacher(user)
        db.execute('UPDATE lessons SET meeting_url=?,updated_at=? WHERE id=?', (safe_url(data.get('meeting_url', '')), now(), lesson_id))
        return {'ok': True}
    if action == 'note':
        teacher(user)
        db.execute('UPDATE lessons SET note=?,updated_at=? WHERE id=?', (text(data.get('note', ''), 10000), now(), lesson_id))
        return {'ok': True}
    if action == 'zoom':
        teacher(user)
        if lesson['status'] != 'scheduled':
            raise Problem('Урок уже завершён или отменён.')
        meeting = integrations.zoom_meeting(lesson)
        db.execute('UPDATE lessons SET zoom_id=?,meeting_url=? WHERE id=?', (str(meeting['id']), safe_url(meeting['join_url']), lesson_id))
        updated=one(db,'SELECT * FROM lessons WHERE id=?',(lesson_id,),True)
        db.execute("INSERT INTO sync_state VALUES ('zoom',?,?) ON CONFLICT(provider,entity_id) DO UPDATE SET fingerprint=excluded.fingerprint",(lesson_id,integrations.zoom_fingerprint(updated)))
        return {'ok': True}
    if action == 'complete':
        teacher(user)
        if lesson['status'] == 'completed':
            return {'ok': True}
        if lesson['status'] != 'scheduled':
            raise Problem('Отменённый урок нельзя завершить.', 409)
        if dt(lesson['starts_at']) > dt(now()):
            raise Problem('Урок ещё не начался.')
        db.execute("UPDATE lessons SET status='completed',updated_at=?,note=? WHERE id=?", (now(), text(data.get('note', lesson['note'])), lesson_id))
        insert(db, 'ledger', id=uid(), student_id=lesson['student_id'], delta=-1, reason='Проведён урок: '+lesson['title'], source='lesson:'+lesson_id, created_at=now())
    elif action in ('cancel', 'reschedule'):
        if lesson['status'] != 'scheduled':
            raise Problem('Урок уже завершён или отменён.', 409)
        config = settings(db)
        late = dt(lesson['starts_at'])-dt(now()) < timedelta(hours=config['cancel_hours'])
        if action == 'cancel':
            charged = late and config['late_cancel_charge'] and user['role']=='student'
            if charged and not data.get('accept_charge'):
                raise Problem('Поздняя отмена: будет списано одно занятие. Подтвердите правило.', 409)
            status = 'late_cancelled' if charged else 'cancelled'
            db.execute('UPDATE lessons SET status=?,updated_at=? WHERE id=?', (status, now(), lesson_id))
            if charged:
                insert(db, 'ledger', id=uid(), student_id=lesson['student_id'], delta=-1, reason='Поздняя отмена урока', source='lesson:'+lesson_id, created_at=now())
        else:
            if data.get('expected_starts_at') and dt(data['expected_starts_at']) != dt(lesson['starts_at']):
                raise Problem('Урок уже изменён. Обновите календарь.', 409)
            if 'expected_duration' in data and integer(data['expected_duration'], 15, 240) != lesson['duration']:
                raise Problem('Длительность урока уже изменена. Обновите календарь.', 409)
            starts_at = iso(data.get('starts_at'))
            duration = integer(data.get('duration', lesson['duration']), 15, 240)
            if dt(starts_at) <= dt(now()):
                raise Problem('Выберите время в будущем.')
            no_conflict(db, starts_at, duration, lesson_id)
            db.execute('UPDATE lessons SET starts_at=?,duration=?,updated_at=? WHERE id=?', (starts_at, duration, now(), lesson_id))
            db.execute("UPDATE homework SET due_at=? WHERE student_id=? AND status='assigned' AND due_at=?", (starts_at, lesson['student_id'], lesson['starts_at']))
            consume_slots(db,starts_at,duration)
    else:
        raise Problem('Неизвестное действие.', 404)
    label = {'complete': 'Урок завершён', 'cancel': 'Урок отменён', 'reschedule': 'Урок перенесён'}[action]
    key = 'lesson:'+lesson_id+':'+action+':'+uid()
    notify(db, lesson['student_id'], label, lesson['title'], 'calendar', key)
    if user['role']=='student':
        notify_teacher(db, label, user['name']+': '+lesson['title'], 'calendar', key)
    audit(db, user, 'lesson.'+action, lesson_id)
    return {'ok': True}


def save_package(db, user, data, package_id=None):
    teacher(user)
    values = (text(data.get('name'), 100, True), integer(data.get('lessons'), 1, 100), integer(data.get('amount'), 1000, 100000000), int(bool(data.get('active', True))), safe_url(data.get('sbp_url', '')), text(data.get('description', ''), 1000))
    if package_id:
        one(db, 'SELECT id FROM packages WHERE id=?', (package_id,), True)
        db.execute('UPDATE packages SET name=?,lessons=?,amount=?,active=?,sbp_url=?,description=? WHERE id=?', values+(package_id,))
    else:
        package_id = uid()
        db.execute('INSERT INTO packages(id,name,lessons,amount,active,sbp_url,description) VALUES (?,?,?,?,?,?,?)', (package_id,)+values)
    return {'id': package_id}


def create_payment(db, user, data):
    student_id = user['id'] if user['role']=='student' else data.get('student_id')
    student_exists(db, student_id)
    package = one(db, 'SELECT * FROM packages WHERE id=? AND active=1', (data.get('package_id'),), True)
    # One unresolved order per package/person; refreshes and double-clicks cannot create duplicates.
    old = one(db, "SELECT * FROM payments WHERE student_id=? AND package_id=? AND status IN ('pending','claimed') ORDER BY created_at DESC LIMIT 1", (student_id, package['id']))
    if old:
        return {'id': old['id']}
    payment_id = uid()
    insert(db, 'payments', id=payment_id, student_id=student_id, package_id=package['id'], lessons=package['lessons'], amount=package['amount'], payment_url=package['sbp_url'] or settings(db)['sbp_url'], request_key=uid(), created_at=now())
    return {'id': payment_id}


def grant_payment(db, payment, user, reference):
    if payment['status']=='paid':
        return {'ok': True}
    if payment['status'] not in ('pending','claimed'):
        raise Problem('Этот платёж нельзя подтвердить.', 409)
    db.execute("UPDATE payments SET status='paid',paid_at=?,reference=? WHERE id=?", (now(), reference, payment['id']))
    insert(db, 'ledger', id=uid(), student_id=payment['student_id'], delta=payment['lessons'], reason='Оплата пакета занятий', source='payment:'+payment['id'], created_at=now())
    notify(db, payment['student_id'], 'Оплата подтверждена', 'Зачислено занятий: '+str(payment['lessons']), 'payments', 'paid:'+payment['id'])
    audit(db, user, 'payment.confirmed', payment['id'])
    return {'ok': True}


def payment_action(db, user, payment_id, action, data):
    payment = one(db, 'SELECT * FROM payments WHERE id=?', (payment_id,), True)
    owns(user, payment['student_id'])
    if action == 'claim':
        if payment['status'] == 'claimed':
            return {'ok': True}
        if payment['status'] != 'pending':
            raise Problem('Оплата уже обработана.', 409)
        db.execute("UPDATE payments SET status='claimed' WHERE id=?", (payment_id,))
        notify_teacher(db, 'Ученик отметил оплату', 'Проверьте поступление '+str(payment['amount']//100)+' ₽ в банке.', 'payments', 'claimed:'+payment_id)
        return {'ok': True}
    teacher(user)
    if action == 'confirm':
        return grant_payment(db, payment, user, text(data.get('reference', ''), 100))
    if action == 'reject':
        if payment['status'] not in ('pending','claimed'):
            raise Problem('Подтверждённый платёж нельзя отклонить.', 409)
        reason = text(data.get('reason'), 500, True)
        db.execute("UPDATE payments SET status='rejected',reference=? WHERE id=?", (reason, payment_id))
        notify(db, payment['student_id'], 'Поступление пока не найдено', reason, 'payments', 'rejected:'+payment_id)
        audit(db, user, 'payment.rejected', payment_id)
        return {'ok': True}
    if action == 'void':
        if payment['status']=='voided': return {'ok':True}
        if payment['status']!='paid': raise Problem('Можно исправить только подтверждённый платёж.',409)
        reason=text(data.get('reason'),500,True)
        db.execute("UPDATE payments SET status='voided',reference=? WHERE id=?",(reason,payment_id))
        insert(db,'ledger',id=uid(),student_id=payment['student_id'],delta=-payment['lessons'],reason='Исправление оплаты: '+reason,source='payment-void:'+payment_id,created_at=now())
        reward=one(db,'SELECT * FROM ledger WHERE source=?',('referral:'+payment_id,))
        if reward:
            insert(db,'ledger',id=uid(),student_id=reward['student_id'],delta=-reward['delta'],reason='Исправление реферального бонуса',source='referral-void:'+payment_id,created_at=now())
            db.execute('UPDATE students SET referral_rewarded=0 WHERE user_id=?',(payment['student_id'],))
        notify(db,payment['student_id'],'Уточнение оплаты',reason,'payments','void:'+payment_id)
        audit(db,user,'payment.voided',payment_id)
        return {'ok':True}
    raise Problem('Неизвестное действие.', 404)


def create_homework(db, user, data):
    teacher(user)
    student_exists(db, data.get('student_id'))
    assignment_id = uid()
    lesson = one(db, "SELECT starts_at FROM lessons WHERE student_id=? AND status='scheduled' AND starts_at>? ORDER BY starts_at LIMIT 1", (data['student_id'], now()))
    if not lesson:
        raise Problem('Сначала запланируйте следующее занятие с этим учеником.')
    title = text(data.get('title') or 'Hometask', 150, True)
    insert(db, 'homework', id=assignment_id, student_id=data['student_id'], title=title, kind='Practice', instructions=text(data.get('instructions'), 15000, True), due_at=lesson['starts_at'], created_at=now())
    notify(db, data['student_id'], 'Новое домашнее задание', title, 'homework', 'homework:'+assignment_id)
    return {'id': assignment_id, 'due_at': lesson['starts_at']}


def homework_action(db, user, assignment_id, action, data):
    task = one(db, 'SELECT * FROM homework WHERE id=?', (assignment_id,), True)
    owns(user, task['student_id'])
    if action=='submit':
        if user['role']!='student':
            raise Problem('Работу отправляет ученик.', 403)
        if task['status']=='reviewed':
            raise Problem('Работа уже проверена.', 409)
        answer = text(data.get('answer', ''), 20000)
        attachments = one(db, "SELECT id FROM files WHERE entity_type='homework' AND entity_id=? AND owner_id=?", (assignment_id, user['id']))
        if not answer and not attachments:
            raise Problem('Добавьте ответ, файл или голосовую запись.')
        db.execute("UPDATE homework SET answer=?,status='submitted',submitted_at=? WHERE id=?", (answer, now(), assignment_id))
        notify_teacher(db, 'Работа ждёт проверки', user['name']+': '+task['title'], 'homework', 'submission:'+assignment_id+':'+uid())
    elif action=='review':
        teacher(user)
        if task['status']!='submitted':
            raise Problem('Ученик ещё не отправил работу.', 409)
        db.execute("UPDATE homework SET feedback=?,score=?,status='reviewed',reviewed_at=? WHERE id=?", (text(data.get('feedback'), 15000, True), integer(data.get('score'), 0, 100), now(), assignment_id))
        notify(db, task['student_id'], 'Домашнее задание проверено', task['title'], 'homework', 'review:'+assignment_id+':'+uid())
    elif action=='reopen':
        teacher(user)
        db.execute("UPDATE homework SET status='assigned' WHERE id=?", (assignment_id,))
        notify(db, task['student_id'], 'Задание возвращено на доработку', task['title'], 'homework', 'reopen:'+assignment_id+':'+uid())
    else:
        raise Problem('Неизвестное действие.', 404)
    return {'ok': True}


def add_template(db, user, data, template_id=None):
    teacher(user)
    values = (text(data.get('title'),150,True), text(data.get('kind','Practice'),40), text(data.get('instructions'),15000,True))
    if template_id:
        one(db, 'SELECT id FROM templates WHERE id=?', (template_id,), True)
        db.execute('UPDATE templates SET title=?,kind=?,instructions=? WHERE id=?', values+(template_id,))
    else:
        template_id=uid()
        db.execute('INSERT INTO templates(id,title,kind,instructions,created_at) VALUES (?,?,?,?,?)', (template_id,)+values+(now(),))
    return {'id': template_id}


def add_material(db, user, data):
    teacher(user)
    student_id = data.get('student_id') or None
    if student_id:
        student_exists(db, student_id)
    material_id=uid()
    insert(db, 'materials', id=material_id, student_id=student_id, title=text(data.get('title'),150,True), body=text(data.get('body',''),15000), url=safe_url(data.get('url','')), created_at=now())
    recipients=[{'id':student_id}] if student_id else rows(db,"SELECT id FROM users WHERE role='student' AND active=1")
    for person in recipients:notify(db,person['id'],'Новый материал',data['title'],'materials','material:'+material_id)
    return {'id': material_id}


def add_goal(db, user, data):
    student_id = user['id'] if user['role']=='student' else data.get('student_id')
    student_exists(db, student_id)
    goal_id=uid()
    deadline = text(data.get('deadline',''),10)
    if deadline:
        try:
            datetime.strptime(deadline, '%Y-%m-%d')
        except ValueError:
            raise Problem('Некорректный срок цели.')
    insert(db, 'goals', id=goal_id, student_id=student_id, title=text(data.get('title'),150,True), target=integer(data.get('target'),1,10000), unit=text(data.get('unit','шагов'),40), deadline=deadline, created_at=now())
    if user['role']=='teacher':notify(db,student_id,'Новая цель',data['title'],'progress','goal:'+goal_id)
    else:notify_teacher(db,'Ученик добавил цель',data['title'],'progress','goal:'+goal_id)
    return {'id':goal_id}


def update_goal(db, user, goal_id, data):
    goal=one(db,'SELECT * FROM goals WHERE id=?',(goal_id,),True)
    owns(user,goal['student_id'])
    db.execute('UPDATE goals SET progress=? WHERE id=?',(integer(data.get('progress'),0,goal['target']),goal_id))
    if user['role']=='teacher':notify(db,goal['student_id'],'Обновлён прогресс цели',goal['title'],'progress','goal-updated:'+goal_id+':'+uid())
    else:notify_teacher(db,'Ученик обновил цель',goal['title'],'progress','goal-updated:'+goal_id+':'+uid())
    return {'ok':True}


def add_progress(db, user, data):
    teacher(user)
    student_exists(db,data.get('student_id'))
    skill=text(data.get('skill'),40,True)
    if skill not in ['Speaking','Listening','Reading','Writing','Grammar','Pronunciation']:
        raise Problem('Неизвестный навык.')
    insert(db,'progress_notes',id=uid(),student_id=data['student_id'],skill=skill,score=integer(data.get('score'),0,100),note=text(data.get('note',''),5000),created_at=now())
    notify(db,data['student_id'],'Новая оценка навыка',skill,'progress','progress:'+uid())
    return {'ok':True}


def send_message(db,user,data):
    student_id=user['id'] if user['role']=='student' else data.get('student_id')
    student_exists(db,student_id)
    message_id=data.get('client_id') or uid()
    if not isinstance(message_id,str) or not re.fullmatch(r'[a-f0-9]{32}',message_id):
        raise Problem('Некорректный идентификатор сообщения.')
    body=text(data.get('body'),10000,True)
    existing=one(db,'SELECT * FROM messages WHERE id=?',(message_id,))
    if existing:
        if existing['author_id']!=user['id'] or existing['student_id']!=student_id or existing['body']!=body:
            raise Problem('Это сообщение уже существует.',409)
        return {'id':message_id,'message':existing}
    message={'id':message_id,'student_id':student_id,'author_id':user['id'],'body':body,'created_at':now()}
    insert(db,'messages',**message)
    if user['role']=='teacher':
        notify(db,student_id,'Сообщение преподавателя',body[:200],'messages','message:'+message_id)
    else:
        notify_teacher(db,'Сообщение ученика',user['name']+': '+body[:180],'messages','message:'+message_id)
    return {'id':message_id,'message':message}


def notification_counts(db,user):
    return {r['route']:r['count'] for r in rows(db,"SELECT route,COUNT(*) AS count FROM notifications WHERE user_id=? AND read_at IS NULL AND route<>'referrals' GROUP BY route",(user['id'],))}


def activity(db,user,conversation=''):
    result={'notification_counts':notification_counts(db,user),'notifications':rows(db,"SELECT * FROM notifications WHERE user_id=? AND route<>'referrals' ORDER BY created_at DESC LIMIT 100",(user['id'],))}
    if conversation:
        owns(user,conversation)
        student_exists(db,conversation)
        result['messages']=rows(db,'SELECT * FROM messages WHERE student_id=? ORDER BY created_at,id',(conversation,))
    return result


def read_notifications(db,user,data):
    where,args='user_id=? AND read_at IS NULL',[user['id']]
    if data.get('route'):
        if data['route'] not in ('overview','calendar','students','payments','homework','materials','progress','reports','analytics','templates','messages','settings'):
            raise Problem('Неизвестный раздел.')
        where+=' AND route=?'
        args.append(data['route'])
    if data.get('route')=='messages' and data.get('student_id'):
        owns(user,data['student_id'])
        student_exists(db,data['student_id'])
        where+=" AND event_key IN (SELECT ? || ':message:' || id FROM messages WHERE student_id=?)"
        args.extend([user['id'],data['student_id']])
    db.execute('UPDATE notifications SET read_at=? WHERE '+where,(now(),*args))
    return {'ok':True,'notification_counts':notification_counts(db,user)}


def report_data(db,student_id,month):
    from zoneinfo import ZoneInfo
    student=one(db,'SELECT * FROM users WHERE id=?',(student_id,),True)
    try:
        start=datetime.strptime(month,'%Y-%m').replace(tzinfo=ZoneInfo(student['timezone']))
    except (TypeError, ValueError):
        raise Problem('Выберите месяц отчёта.')
    next_month=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    a,b=iso(start.isoformat()),iso(next_month.isoformat())
    lessons=rows(db,"SELECT * FROM lessons WHERE student_id=? AND starts_at>=? AND starts_at<? AND status='completed'",(student_id,a,b))
    work=rows(db,'SELECT * FROM homework WHERE student_id=? AND reviewed_at>=? AND reviewed_at<?',(student_id,a,b))
    scores=[w['score'] for w in work if w['score'] is not None]
    goals=rows(db,'SELECT * FROM goals WHERE student_id=?',(student_id,))
    progress=rows(db,'SELECT * FROM progress_notes WHERE student_id=? AND created_at>=? AND created_at<? ORDER BY created_at',(student_id,a,b))
    return {'student':student['name'],'month':month,'lessons':len(lessons),'minutes':sum(l['duration'] for l in lessons),'homework':len(work),
            'average':round(sum(scores)/len(scores)) if scores else None,'balance':balance(db,student_id),'goals':goals,'progress':progress,
            'topics':[l['title'] for l in lessons]}


def create_report(db,user,data):
    teacher(user)
    student_exists(db,data.get('student_id'))
    content=report_data(db,data['student_id'],text(data.get('month'),7,True))
    content['comment']=text(data.get('comment',''),10000)
    existing=one(db,'SELECT * FROM reports WHERE student_id=? AND month=?',(data['student_id'],data['month']))
    if existing and existing['published_at']:
        raise Problem('Этот отчёт уже опубликован. Для изменения отредактируйте комментарий.',409)
    report_id=existing['id'] if existing else uid()
    if existing:
        db.execute('UPDATE reports SET body=? WHERE id=?',(json.dumps(content,ensure_ascii=False),report_id))
    else:
        insert(db,'reports',id=report_id,student_id=data['student_id'],month=data['month'],body=json.dumps(content,ensure_ascii=False),created_at=now())
    return {'id':report_id}


def publish_report(db,user,report_id,data):
    teacher(user)
    report=one(db,'SELECT * FROM reports WHERE id=?',(report_id,),True)
    content=json.loads(report['body'])
    content['comment']=text(data.get('comment',content.get('comment','')),10000)
    db.execute('UPDATE reports SET body=?,published_at=? WHERE id=?',(json.dumps(content,ensure_ascii=False),now(),report_id))
    notify(db,report['student_id'],'Ваш отчёт за месяц готов',report['month'],'reports','report:'+report_id)
    return {'ok':True}


def can_read_file(db,user,file):
    if user['role']=='teacher' or file['owner_id']==user['id']:
        return True
    if file['entity_type']=='sbp_qr':
        return settings(db)['sbp_qr_file_id']==file['id']
    if file['entity_type']=='material' and file['student_id'] is None:
        return True
    return file['student_id']==user['id']


def upload(db,user,data):
    entity_type,entity_id=data.get('entity_type'),data.get('entity_id')
    student_id=None
    if entity_type in ('homework','payment','material'):
        table={'homework':'homework','payment':'payments','material':'materials'}[entity_type]
        target=one(db,'SELECT * FROM '+table+' WHERE id=?',(entity_id,),True)
        student_id=target['student_id']
        if entity_type=='material':
            teacher(user)
        else:
            owns(user,student_id)
        if entity_type=='payment' and target['status'] in ('paid','rejected','voided'):
            raise Problem('Вложения обработанного платежа нельзя изменять.',409)
        if entity_type=='homework' and user['role']=='student' and target['status']=='reviewed':
            raise Problem('Задание уже проверено.',409)
    elif entity_type=='sbp_qr':
        teacher(user)
        entity_id='studio'
    else:
        raise Problem('Неизвестный тип вложения.')
    filename=Path(text(data.get('filename'),180,True)).name
    try:
        raw=base64.b64decode(data.get('data',''),validate=True)
    except (ValueError,binascii.Error):
        raise Problem('Файл повреждён.')
    if not raw or len(raw)>10*1024*1024:
        raise Problem('Размер файла должен быть от 1 байта до 10 МБ.')
    if db.execute('SELECT COALESCE(SUM(size),0) FROM files WHERE owner_id=?',(user['id'],)).fetchone()[0]+len(raw)>1024*1024*1024:
        raise Problem('Достигнут лимит вложений 1 ГБ. Обратитесь к преподавателю.',413)
    from . import storage
    if storage.cloud() and db.execute('SELECT COALESCE(SUM(size),0) FROM files').fetchone()[0]+len(raw)>800*1024*1024:
        raise Problem('Достигнут общий лимит файлов бесплатной студии 800 МБ. Данные не удалены; новые загрузки приостановлены.',413)
    suffix=Path(filename).suffix.lower()
    mime=None
    if raw.startswith(b'\x89PNG\r\n\x1a\n') and suffix=='.png': mime='image/png'
    elif raw.startswith(b'\xff\xd8\xff') and suffix in ('.jpg','.jpeg'): mime='image/jpeg'
    elif raw[:4]==b'RIFF' and raw[8:12]==b'WEBP' and suffix=='.webp': mime='image/webp'
    elif raw.startswith(b'%PDF-') and suffix=='.pdf': mime='application/pdf'
    elif raw.startswith(b'\x1aE\xdf\xa3') and suffix=='.webm': mime='audio/webm'
    elif raw.startswith(b'OggS') and suffix=='.ogg': mime='audio/ogg'
    elif raw[:4]==b'RIFF' and raw[8:12]==b'WAVE' and suffix=='.wav': mime='audio/wav'
    elif raw[4:8]==b'ftyp' and suffix in ('.m4a','.mp4'): mime='audio/mp4'
    elif (raw.startswith(b'ID3') or raw[:2] in (b'\xff\xfb',b'\xff\xf3',b'\xff\xf2')) and suffix=='.mp3': mime='audio/mpeg'
    elif raw.startswith(b'PK\x03\x04') and suffix in ('.docx','.pptx','.xlsx'): mime='application/octet-stream'
    elif suffix=='.txt':
        try:
            raw.decode('utf-8')
            mime='text/plain'
        except UnicodeDecodeError:
            pass
    if not mime or (entity_type=='sbp_qr' and not mime.startswith('image/')):
        raise Problem('Поддерживаются PDF, PNG, JPEG, WebP, TXT, Office и аудио. Для QR нужен PNG или JPEG.')
    file_id=uid()
    insert(db,'files',id=file_id,owner_id=user['id'],student_id=student_id,entity_type=entity_type,entity_id=entity_id,filename=filename,mime=mime,size=len(raw),created_at=now())
    storage.put(file_id,raw,mime)
    if entity_type=='sbp_qr':
        db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(file_id),'sbp_qr_file_id'))
    return {'id':file_id,'filename':filename}


def save_settings(db,user,data):
    teacher(user)
    allowed={'studio_name':lambda x:text(x,80,True),'cancel_hours':lambda x:integer(x,0,168),
             'late_cancel_charge':bool,
             'sbp_url':safe_url,'reminder_hours':lambda x:integer(x,1,72),'low_balance':lambda x:integer(x,0,10)}
    for key,validator in allowed.items():
        if key in data:
            db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(validator(data[key]),ensure_ascii=False),key))
    audit(db,user,'settings.updated','studio')
    return {'ok':True}


def save_profile(db,user,data):
    db.execute('UPDATE users SET name=?,timezone=?,email_enabled=?,telegram_enabled=? WHERE id=?',(
        text(data.get('name',user['name']),100,True),validate_zone(data.get('timezone',user['timezone'])),
        int(bool(data.get('email_enabled',user['email_enabled']))),int(bool(data.get('telegram_enabled',user['telegram_enabled']))),user['id']))
    return {'ok':True}


def adjust_balance(db,user,student_id,data):
    teacher(user)
    student_exists(db,student_id)
    key=text(data.get('request_key'),100,True)
    source='adjustment:'+key
    if one(db,'SELECT id FROM ledger WHERE source=?',(source,)): return {'ok':True}
    delta=integer(data.get('delta'),-1000,1000)
    if not delta: raise Problem('Укажите ненулевую корректировку.')
    reason=text(data.get('reason'),500,True)
    insert(db,'ledger',id=uid(),student_id=student_id,delta=delta,reason=reason,source=source,created_at=now())
    audit(db,user,'balance.adjusted',student_id)
    return {'ok':True}


def csv_export(db,user,kind):
    teacher(user)
    if kind=='payments':
        data=rows(db,'SELECT u.name,u.email,p.created_at,p.paid_at,p.amount,p.lessons,p.status,p.reference FROM payments p JOIN users u ON u.id=p.student_id ORDER BY p.created_at DESC')
        headers=['Ученик','Email','Создан','Подтверждён','Копейки','Занятия','Статус','Номер операции']
    elif kind=='students':
        data=rows(db,'SELECT u.name,u.email,s.level,s.focus,COALESCE((SELECT SUM(delta) FROM ledger WHERE student_id=u.id),0) AS balance FROM users u JOIN students s ON s.user_id=u.id')
        headers=['Ученик','Email','Уровень','Цель','Баланс']
    else:
        raise Problem('Неизвестный экспорт.',404)
    output=io.StringIO()
    writer=csv.writer(output,delimiter=';')
    writer.writerow(headers)
    for row in data:
        writer.writerow([("'"+str(v)) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@','\t','\r')) else v for v in row.values()])
    return ('\ufeff'+output.getvalue()).encode('utf-8')
