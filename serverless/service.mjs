import { DateTime } from 'luxon';
import { randomBytes } from 'node:crypto';
import { Problem, text, integer, email, safeUrl, zone, uid, now, dt, iso, rows, one, insert, settings, teacher, owns, studentExists, balance, scalar, audit, notify, notifyTeacher, makeToken, tokenRecord, rateLimit, digest, baseUrl, publicUser, readBatch } from './core.mjs';
import * as integrations from './integrations.mjs';
import * as storage from './storage.mjs';

export const scoped = (user, column = 'student_id') => user.role === 'teacher' ? ['1=1', []] : [column + '=?', [user.id]];
export async function canReadFile(db, user, file) {
  if (user.role === 'teacher' || file.owner_id === user.id) return true;
  if (file.entity_type === 'sbp_qr') return (await settings(db)).sbp_qr_file_id === file.id;
  return (file.entity_type === 'material' && file.student_id === null) || file.student_id === user.id;
}
export async function bootstrap(db, user) {
  const [clause,args]=scoped(user),entries=[
    ['settings','SELECT * FROM settings',[]],
    ['students','SELECT u.id,u.name,u.email,u.timezone,u.active,s.level,s.focus,s.private_notes,COALESCE((SELECT SUM(delta) FROM ledger WHERE student_id=u.id),0) AS balance FROM users u JOIN students s ON s.user_id=u.id WHERE '+(user.role==='teacher'?'1=1':'u.id=?')+' ORDER BY u.name',args],
    ['teachers',"SELECT id,name FROM users WHERE role='teacher'",[]],
    ...[['lessons','lessons','starts_at'],['payments','payments','created_at DESC'],['ledger','ledger','created_at DESC'],['homework','homework','due_at'],['goals','goals','created_at DESC'],['progress','progress_notes','created_at'],['messages','messages','created_at,rowid']].map(([key,table,order])=>[key,'SELECT * FROM '+table+' WHERE '+clause+' ORDER BY '+order,args]),
    ['slots','SELECT * FROM slots WHERE starts_at>? ORDER BY starts_at',[now()]],
    ['packages','SELECT * FROM packages '+(user.role==='teacher'?'':'WHERE active=1')+' ORDER BY lessons',[]],
    ['materials','SELECT * FROM materials WHERE '+clause+' OR student_id IS NULL ORDER BY created_at DESC',args],
    ['reports','SELECT * FROM reports WHERE '+clause+(user.role==='teacher'?'':' AND published_at IS NOT NULL')+' ORDER BY month DESC',args],
    ['notifications',"SELECT * FROM notifications WHERE user_id=? AND route<>'referrals' ORDER BY created_at DESC LIMIT 100",[user.id]],
    ['notificationCounts',"SELECT route,COUNT(*) AS count FROM notifications WHERE user_id=? AND read_at IS NULL AND route<>'referrals' GROUP BY route",[user.id]],
    ['google',"SELECT user_id FROM integrations WHERE user_id=? AND provider='google'",[user.id]],
    ['templates',user.role==='teacher'?'SELECT * FROM templates ORDER BY title':'SELECT * FROM templates WHERE 1=0',[]],
    ['files',user.role==='teacher'?'SELECT * FROM files':"SELECT * FROM files WHERE owner_id=? OR student_id=? OR (entity_type='material' AND student_id IS NULL) OR entity_type='sbp_qr'",user.role==='teacher'?[]:[user.id,user.id]],
    ['deliveries',user.role==='teacher'?'SELECT channel,state,COUNT(*) AS count FROM outbox GROUP BY channel,state':'SELECT channel,state FROM outbox WHERE 1=0',[]]
  ];
  const values=await readBatch(db,entries.map(([,sql,params])=>[sql,params]));
  const result=Object.fromEntries(entries.map(([key],i)=>[key,values[i]]));
  const cfg=Object.fromEntries(result.settings.map(r=>[r.key,JSON.parse(r.value)]));
  cfg.referral_enabled=false;cfg.referral_bonus=0;result.settings=cfg;
  result.hosting={free:true,serverless:true};result.user=publicUser(user);result.referrals=[];
  result.notification_counts=Object.fromEntries(result.notificationCounts.map(r=>[r.route,Number(r.count)]));delete result.notificationCounts;
  for(const person of result.students)if(user.role!=='teacher')delete person.private_notes;
  for(const payment of result.payments)for(const key of ['request_key','provider_id','qr_svg','error'])delete payment[key];
  result.files=result.files.filter(f=>user.role==='teacher'||f.owner_id===user.id||(f.entity_type==='sbp_qr'&&cfg.sbp_qr_file_id===f.id)||(f.entity_type==='material'&&f.student_id===null)||f.student_id===user.id);
  result.integrations=await integrations.status(db,user,Boolean(result.google.length));delete result.google;
  return result;
}
export async function addStudent(db, user, data) {
  teacher(user);
  const addr = email(data.email), name = text(data.name, 100, true);
  if (await one(db, 'SELECT id FROM users WHERE email=?', [addr])) throw new Problem('Ученик с таким email уже существует.', 409);
  const referrer = null;
  const id = uid();
  await insert(db, 'users', { id, email: addr, name, role: 'student', timezone: zone(data.timezone ?? 'Europe/Moscow'), created_at: now() });
  await insert(db, 'students', { user_id: id, level: text(data.level ?? 'B1', 20), focus: text(data.focus ?? '', 1000), private_notes: text(data.private_notes ?? '', 5000), referral_code: randomBytes(4).toString('hex').toUpperCase(), referrer_id: referrer });
  await audit(db, user, 'student.created', id);
  return { id, invite_url: baseUrl() + '/#invite=' + await makeToken(db, id, 'invite', 7 * 86400) };
}
export async function newInvite(db, user, id) {
  teacher(user); await studentExists(db, id);
  await rows(db, "DELETE FROM tokens WHERE user_id=? AND kind='invite'", [id]);
  return { invite_url: baseUrl() + '/#invite=' + await makeToken(db, id, 'invite', 7 * 86400) };
}
export async function editStudent(db, user, id, data) {
  teacher(user); await studentExists(db, id);
  await rows(db, 'UPDATE users SET name=?,timezone=?,active=? WHERE id=?', [text(data.name, 100, true), zone(data.timezone ?? 'Europe/Moscow'), Number(Boolean(data.active ?? true)), id]);
  await rows(db, 'UPDATE students SET level=?,focus=?,private_notes=? WHERE user_id=?', [text(data.level ?? '', 20), text(data.focus ?? '', 1000), text(data.private_notes ?? '', 5000), id]);
  if (!(data.active ?? true)) await rows(db, 'DELETE FROM sessions WHERE user_id=?', [id]);
  await audit(db, user, 'student.updated', id); return { ok: true };
}
export async function noConflict(db, start, duration, exclude = '') {
  const a = +dt(start), b = a + duration * 60000;
  for (const lesson of await rows(db, "SELECT id,starts_at,duration FROM lessons WHERE status='scheduled' AND id<>?", [exclude])) {
    const c = +dt(lesson.starts_at);
    if (a < c + lesson.duration * 60000 && b > c) throw new Problem('Это время уже занято другим уроком.', 409);
  }
}
export async function consumeSlots(db, start, duration) {
  const a = +dt(start), b = a + duration * 60000;
  for (const slot of await rows(db, 'SELECT * FROM slots')) if (a < +dt(slot.starts_at) + slot.duration * 60000 && b > +dt(slot.starts_at)) await rows(db, 'DELETE FROM slots WHERE id=?', [slot.id]);
}
export async function addLesson(db, user, data) {
  teacher(user); await studentExists(db, data.student_id);
  const start = iso(data.starts_at), duration = integer(data.duration ?? 50, 15, 240);
  if (+dt(start) < Date.now()) throw new Problem('Новый урок должен быть в будущем.');
  const title = text(data.title, 150, true), link = safeUrl(data.meeting_url ?? ''), repeat = integer(data.repeat ?? 1, 1, 26), ids = [];
  for (let week = 0; week < repeat; week++) {
    const at = dt(start).setZone(user.timezone).plus({ weeks: week }).toUTC().toISO({ suppressMilliseconds: true });
    await noConflict(db, at, duration);
    const id = uid();
    await insert(db, 'lessons', { id, student_id: data.student_id, title, starts_at: at, duration, meeting_url: link, created_at: now(), updated_at: now() });
    await consumeSlots(db, at, duration); ids.push(id);
  }
  await notify(db, data.student_id, 'В расписании новый урок', title, 'calendar', 'lesson.created:' + ids[0]);
  await audit(db, user, 'lesson.created', ids[0]); return { ids };
}
export async function addSlot(db, user, data) {
  teacher(user); const start = iso(data.starts_at), duration = integer(data.duration ?? 50, 15, 240);
  if (+dt(start) <= Date.now()) throw new Problem('Свободное окно должно быть в будущем.');
  await noConflict(db, start, duration); return insert(db, 'slots', { id: uid(), starts_at: start, duration });
}
export async function bookSlot(db, user, id) {
  if (user.role !== 'student') throw new Problem('Записаться может ученик.');
  const slot = await one(db, 'SELECT * FROM slots WHERE id=?', [id], true);
  if (+dt(slot.starts_at) <= Date.now()) throw new Problem('Окно уже прошло.');
  if (await balance(db, user.id) <= 0) throw new Problem('Сначала пополните баланс занятий.');
  await noConflict(db, slot.starts_at, slot.duration);
  const lesson = uid();
  await insert(db, 'lessons', { id: lesson, student_id: user.id, title: 'Индивидуальный урок', starts_at: slot.starts_at, duration: slot.duration, created_at: now(), updated_at: now() });
  await consumeSlots(db, slot.starts_at, slot.duration);
  await notifyTeacher(db, 'Запись на урок', user.name + ' записался на занятие.', 'calendar', 'book:' + lesson);
  await notify(db, user.id, 'Вы записаны на урок', 'Занятие появилось в расписании.', 'calendar', 'book:' + lesson);
  return { id: lesson };
}
export async function lessonAction(db, user, id, action, data) {
  if (action === 'reschedule') teacher(user);
  const lesson = await one(db, 'SELECT * FROM lessons WHERE id=?', [id], true); owns(user, lesson.student_id);
  if (action === 'link' || action === 'note') {
    teacher(user); const column = action === 'link' ? 'meeting_url' : 'note';
    await rows(db, `UPDATE lessons SET ${column}=?,updated_at=? WHERE id=?`, [action === 'link' ? safeUrl(data.meeting_url ?? '') : text(data.note ?? ''), now(), id]); return { ok: true };
  }
  if (action === 'zoom') {
    teacher(user); if (lesson.status !== 'scheduled') throw new Problem('Урок уже завершён или отменён.');
    const meeting = await integrations.zoomMeeting(lesson);
    await rows(db, 'UPDATE lessons SET zoom_id=?,meeting_url=? WHERE id=?', [String(meeting.id), safeUrl(meeting.join_url), id]); return { ok: true };
  }
  if (action === 'complete') {
    teacher(user); if (lesson.status === 'completed') return { ok: true };
    if (lesson.status !== 'scheduled') throw new Problem('Отменённый урок нельзя завершить.', 409);
    if (+dt(lesson.starts_at) > Date.now()) throw new Problem('Урок ещё не начался.');
    await rows(db, "UPDATE lessons SET status='completed',updated_at=?,note=? WHERE id=?", [now(), text(data.note ?? lesson.note), id]);
    await insert(db, 'ledger', { id: uid(), student_id: lesson.student_id, delta: -1, reason: 'Проведён урок: ' + lesson.title, source: 'lesson:' + id, created_at: now() });
  } else if (['cancel', 'reschedule'].includes(action)) {
    if (lesson.status !== 'scheduled') throw new Problem('Урок уже завершён или отменён.', 409);
    const cfg = await settings(db), late = +dt(lesson.starts_at) - Date.now() < cfg.cancel_hours * 3600000;
    if (action === 'cancel') {
      const charged = late && cfg.late_cancel_charge && user.role === 'student';
      if (charged && !data.accept_charge) throw new Problem('Поздняя отмена: будет списано одно занятие. Подтвердите правило.', 409);
      await rows(db, 'UPDATE lessons SET status=?,updated_at=? WHERE id=?', [charged ? 'late_cancelled' : 'cancelled', now(), id]);
      if (charged) await insert(db, 'ledger', { id: uid(), student_id: lesson.student_id, delta: -1, reason: 'Поздняя отмена урока', source: 'lesson:' + id, created_at: now() });
    } else {
      if (data.expected_starts_at && +dt(data.expected_starts_at) !== +dt(lesson.starts_at)) throw new Problem('Урок уже изменён. Обновите календарь.', 409);
      if (data.expected_duration !== undefined && integer(data.expected_duration,15,240) !== lesson.duration) throw new Problem('Длительность урока уже изменена. Обновите календарь.', 409);
      const start = iso(data.starts_at), duration = integer(data.duration ?? lesson.duration, 15, 240);
      if (+dt(start) <= Date.now()) throw new Problem('Выберите время в будущем.');
      await noConflict(db, start, duration, id);
      await rows(db, 'UPDATE lessons SET starts_at=?,duration=?,updated_at=? WHERE id=?', [start, duration, now(), id]);
      await rows(db,"UPDATE homework SET due_at=? WHERE student_id=? AND status='assigned' AND due_at=?",[start,lesson.student_id,lesson.starts_at]);
      await consumeSlots(db, start, duration);
    }
  } else throw new Problem('Неизвестное действие.', 404);
  const label = { complete: 'Урок завершён', cancel: 'Урок отменён', reschedule: 'Урок перенесён' }[action], key = 'lesson:' + id + ':' + action + ':' + uid();
  await notify(db, lesson.student_id, label, lesson.title, 'calendar', key);
  if (user.role === 'student') await notifyTeacher(db, label, user.name + ': ' + lesson.title, 'calendar', key);
  await audit(db, user, 'lesson.' + action, id); return { ok: true };
}
export async function savePackage(db, user, data, id = null) {
  teacher(user);
  const values = [text(data.name, 100, true), integer(data.lessons, 1, 100), integer(data.amount, 1000, 100000000), Number(Boolean(data.active ?? true)), safeUrl(data.sbp_url ?? ''), text(data.description ?? '', 1000)];
  if (id) { await one(db, 'SELECT id FROM packages WHERE id=?', [id], true); await rows(db, 'UPDATE packages SET name=?,lessons=?,amount=?,active=?,sbp_url=?,description=? WHERE id=?', [...values, id]); }
  else { id = uid(); await rows(db, 'INSERT INTO packages(id,name,lessons,amount,active,sbp_url,description) VALUES (?,?,?,?,?,?,?)', [id, ...values]); }
  return { id };
}
export async function createPayment(db, user, data) {
  const student = user.role === 'student' ? user.id : data.student_id; await studentExists(db, student);
  const pack = await one(db, 'SELECT * FROM packages WHERE id=? AND active=1', [data.package_id], true);
  const old = await one(db, "SELECT * FROM payments WHERE student_id=? AND package_id=? AND status IN ('pending','claimed') ORDER BY created_at DESC LIMIT 1", [student, pack.id]);
  if (old) return { id: old.id };
  const id = uid(); await insert(db, 'payments', { id, student_id: student, package_id: pack.id, lessons: pack.lessons, amount: pack.amount, payment_url: pack.sbp_url || (await settings(db)).sbp_url, request_key: uid(), created_at: now() }); return { id };
}
export async function paymentAction(db, user, id, action, data) {
  const payment = await one(db, 'SELECT * FROM payments WHERE id=?', [id], true); owns(user, payment.student_id);
  if (action === 'claim') {
    if (payment.status === 'claimed') return { ok: true };
    if (payment.status !== 'pending') throw new Problem('Оплата уже обработана.', 409);
    await rows(db, "UPDATE payments SET status='claimed' WHERE id=?", [id]);
    await notifyTeacher(db, 'Ученик отметил оплату', 'Проверьте поступление ' + Math.floor(payment.amount / 100) + ' ₽ в банке.', 'payments', 'claimed:' + id); return { ok: true };
  }
  teacher(user);
  if (action === 'confirm') {
    if (payment.status === 'paid') return { ok: true };
    if (!['pending', 'claimed'].includes(payment.status)) throw new Problem('Этот платёж нельзя подтвердить.', 409);
    await rows(db, "UPDATE payments SET status='paid',paid_at=?,reference=? WHERE id=?", [now(), text(data.reference ?? '', 100), id]);
    await insert(db, 'ledger', { id: uid(), student_id: payment.student_id, delta: payment.lessons, reason: 'Оплата пакета занятий', source: 'payment:' + id, created_at: now() });
    await notify(db, payment.student_id, 'Оплата подтверждена', 'Зачислено занятий: ' + payment.lessons, 'payments', 'paid:' + id);
    await audit(db, user, 'payment.confirmed', id);
  } else if (action === 'reject') {
    if (!['pending', 'claimed'].includes(payment.status)) throw new Problem('Подтверждённый платёж нельзя отклонить.', 409);
    const reason = text(data.reason, 500, true);
    await rows(db, "UPDATE payments SET status='rejected',reference=? WHERE id=?", [reason, id]);
    await notify(db, payment.student_id, 'Поступление пока не найдено', reason, 'payments', 'rejected:' + id); await audit(db, user, 'payment.rejected', id);
  } else if (action === 'void') {
    if (payment.status === 'voided') return { ok: true };
    if (payment.status !== 'paid') throw new Problem('Можно исправить только подтверждённый платёж.', 409);
    const reason = text(data.reason, 500, true);
    await rows(db, "UPDATE payments SET status='voided',reference=? WHERE id=?", [reason, id]);
    await insert(db, 'ledger', { id: uid(), student_id: payment.student_id, delta: -payment.lessons, reason: 'Исправление оплаты: ' + reason, source: 'payment-void:' + id, created_at: now() });
    const reward = await one(db, 'SELECT * FROM ledger WHERE source=?', ['referral:' + id]);
    if (reward) {
      await insert(db, 'ledger', { id: uid(), student_id: reward.student_id, delta: -reward.delta, reason: 'Исправление реферального бонуса', source: 'referral-void:' + id, created_at: now() });
      await rows(db, 'UPDATE students SET referral_rewarded=0 WHERE user_id=?', [payment.student_id]);
    }
    await notify(db, payment.student_id, 'Уточнение оплаты', reason, 'payments', 'void:' + id); await audit(db, user, 'payment.voided', id);
  } else throw new Problem('Неизвестное действие.', 404);
  return { ok: true };
}
export async function createHomework(db, user, data) {
  teacher(user); await studentExists(db, data.student_id); const id = uid();
  const lesson=await one(db,"SELECT starts_at FROM lessons WHERE student_id=? AND status='scheduled' AND starts_at>? ORDER BY starts_at LIMIT 1",[data.student_id,now()]);
  if(!lesson)throw new Problem('Сначала запланируйте следующее занятие с этим учеником.');
  const title=text(data.title||'Hometask',150,true);
  await insert(db, 'homework', { id, student_id: data.student_id, title, kind:'Practice', instructions: text(data.instructions, 15000, true), due_at: lesson.starts_at, created_at: now() });
  await notify(db, data.student_id, 'Новое домашнее задание', title, 'homework', 'homework:' + id); return { id, due_at:lesson.starts_at };
}
export async function homeworkAction(db, user, id, action, data) {
  const task = await one(db, 'SELECT * FROM homework WHERE id=?', [id], true); owns(user, task.student_id);
  if (action === 'submit') {
    if (user.role !== 'student') throw new Problem('Работу отправляет ученик.', 403);
    if (task.status === 'reviewed') throw new Problem('Работа уже проверена.', 409);
    const answer = text(data.answer ?? '', 20000), attachment = await one(db, "SELECT id FROM files WHERE entity_type='homework' AND entity_id=? AND owner_id=?", [id, user.id]);
    if (!answer && !attachment) throw new Problem('Добавьте ответ, файл или голосовую запись.');
    await rows(db, "UPDATE homework SET answer=?,status='submitted',submitted_at=? WHERE id=?", [answer, now(), id]);
    await notifyTeacher(db, 'Работа ждёт проверки', user.name + ': ' + task.title, 'homework', 'submission:' + id + ':' + uid());
  } else if (action === 'review') {
    teacher(user); if (task.status !== 'submitted') throw new Problem('Ученик ещё не отправил работу.', 409);
    await rows(db, "UPDATE homework SET feedback=?,score=?,status='reviewed',reviewed_at=? WHERE id=?", [text(data.feedback, 15000, true), integer(data.score, 0, 100), now(), id]);
    await notify(db, task.student_id, 'Домашнее задание проверено', task.title, 'homework', 'review:' + id + ':' + uid());
  } else if (action === 'reopen') {
    teacher(user); await rows(db, "UPDATE homework SET status='assigned' WHERE id=?", [id]);
    await notify(db, task.student_id, 'Задание возвращено на доработку', task.title, 'homework', 'reopen:' + id + ':' + uid());
  } else throw new Problem('Неизвестное действие.', 404);
  return { ok: true };
}
export async function addTemplate(db, user, data, id = null) {
  teacher(user); const values = [text(data.title, 150, true), text(data.kind ?? 'Practice', 40), text(data.instructions, 15000, true)];
  if (id) { await one(db, 'SELECT id FROM templates WHERE id=?', [id], true); await rows(db, 'UPDATE templates SET title=?,kind=?,instructions=? WHERE id=?', [...values, id]); }
  else { id = uid(); await rows(db, 'INSERT INTO templates(id,title,kind,instructions,created_at) VALUES (?,?,?,?,?)', [id, ...values, now()]); } return { id };
}
export async function addMaterial(db, user, data) {
  teacher(user); const student = data.student_id || null, id = uid(); if (student) await studentExists(db, student);
  await insert(db, 'materials', { id, student_id: student, title: text(data.title, 150, true), body: text(data.body ?? '', 15000), url: safeUrl(data.url ?? ''), created_at: now() });
  const recipients=student?[{id:student}]:await rows(db,"SELECT id FROM users WHERE role='student' AND active=1");
  for(const person of recipients)await notify(db,person.id,'Новый материал',data.title,'materials','material:'+id);
  return { id };
}
export async function addGoal(db, user, data) {
  const student = user.role === 'student' ? user.id : data.student_id; await studentExists(db, student);
  const deadline = text(data.deadline ?? '', 10), id = uid();
  if (deadline && (!/^\d{4}-\d\d-\d\d$/.test(deadline) || !DateTime.fromISO(deadline).isValid)) throw new Problem('Некорректный срок цели.');
  await insert(db, 'goals', { id, student_id: student, title: text(data.title, 150, true), target: integer(data.target, 1, 10000), unit: text(data.unit ?? 'шагов', 40), deadline, created_at: now() });
  if(user.role==='teacher')await notify(db,student,'Новая цель',data.title,'progress','goal:'+id);
  else await notifyTeacher(db,'Ученик добавил цель',data.title,'progress','goal:'+id);
  return { id };
}
export async function updateGoal(db, user, id, data) {
  const goal = await one(db, 'SELECT * FROM goals WHERE id=?', [id], true); owns(user, goal.student_id);
  await rows(db, 'UPDATE goals SET progress=? WHERE id=?', [integer(data.progress, 0, goal.target), id]);
  if(user.role==='teacher')await notify(db,goal.student_id,'Обновлён прогресс цели',goal.title,'progress','goal-updated:'+id+':'+uid());
  else await notifyTeacher(db,'Ученик обновил цель',goal.title,'progress','goal-updated:'+id+':'+uid());
  return { ok: true };
}
export async function addProgress(db, user, data) {
  teacher(user); await studentExists(db, data.student_id); const skill = text(data.skill, 40, true);
  if (!['Speaking', 'Listening', 'Reading', 'Writing', 'Grammar', 'Pronunciation'].includes(skill)) throw new Problem('Неизвестный навык.');
  const id=uid();await insert(db, 'progress_notes', { id, student_id: data.student_id, skill, score: integer(data.score, 0, 100), note: text(data.note ?? '', 5000), created_at: now() });
  await notify(db,data.student_id,'Новая оценка навыка',skill,'progress','progress:'+id);return { ok: true };
}
export async function sendMessage(db, user, data) {
  const student = user.role === 'student' ? user.id : data.student_id; await studentExists(db, student);
  const id=data.client_id||uid(),body=text(data.body,10000,true);
  if(typeof id!=='string'||!/^[a-f0-9]{32}$/.test(id))throw new Problem('Некорректный идентификатор сообщения.');
  const existing=await one(db,'SELECT * FROM messages WHERE id=?',[id]);
  if(existing){if(existing.author_id!==user.id||existing.student_id!==student||existing.body!==body)throw new Problem('Это сообщение уже существует.',409);return {id,message:existing};}
  const message={id,student_id:student,author_id:user.id,body,created_at:now()};
  await insert(db, 'messages', message);
  if (user.role === 'teacher') await notify(db, student, 'Сообщение преподавателя', body.slice(0, 200), 'messages', 'message:' + id);
  else await notifyTeacher(db, 'Сообщение ученика', user.name + ': ' + body.slice(0, 180), 'messages', 'message:' + id); return { id,message };
}
const notificationRoutes=['overview','calendar','students','payments','homework','materials','progress','reports','analytics','templates','messages','settings'];
export async function activity(db,user,conversation=''){
  if(conversation){owns(user,conversation);await studentExists(db,conversation);}
  const queries=[
    ["SELECT route,COUNT(*) AS count FROM notifications WHERE user_id=? AND read_at IS NULL AND route<>'referrals' GROUP BY route",[user.id]],
    ["SELECT * FROM notifications WHERE user_id=? AND route<>'referrals' ORDER BY created_at DESC LIMIT 100",[user.id]]
  ];
  if(conversation)queries.push(['SELECT * FROM messages WHERE student_id=? ORDER BY created_at,rowid',[conversation]]);
  const result=await readBatch(db,queries);
  return {notification_counts:Object.fromEntries(result[0].map(r=>[r.route,Number(r.count)])),notifications:result[1],...(conversation?{messages:result[2]}:{})};
}
export async function readNotifications(db,user,data){
  let where='user_id=? AND read_at IS NULL',args=[user.id];
  if(data.route){if(!notificationRoutes.includes(data.route))throw new Problem('Неизвестный раздел.');where+=' AND route=?';args.push(data.route);}
  if(data.route==='messages'&&data.student_id){
    owns(user,data.student_id);await studentExists(db,data.student_id);
    where+=" AND event_key IN (SELECT ? || ':message:' || id FROM messages WHERE student_id=?)";args.push(user.id,data.student_id);
  }
  await rows(db,'UPDATE notifications SET read_at=? WHERE '+where,[now(),...args]);
  const counts=await rows(db,"SELECT route,COUNT(*) AS count FROM notifications WHERE user_id=? AND read_at IS NULL AND route<>'referrals' GROUP BY route",[user.id]);
  return {ok:true,notification_counts:Object.fromEntries(counts.map(r=>[r.route,Number(r.count)]))};
}
export async function reportData(db, studentId, month) {
  const student = await one(db, 'SELECT * FROM users WHERE id=?', [studentId], true);
  const start = DateTime.fromFormat(month, 'yyyy-MM', { zone: student.timezone });
  if (!/^\d{4}-\d\d$/.test(month) || !start.isValid) throw new Problem('Выберите месяц отчёта.');
  const a = start.toUTC().toISO({ suppressMilliseconds: true }), b = start.plus({ months: 1 }).toUTC().toISO({ suppressMilliseconds: true });
  const lessons = await rows(db, "SELECT * FROM lessons WHERE student_id=? AND starts_at>=? AND starts_at<? AND status='completed'", [studentId, a, b]);
  const work = await rows(db, 'SELECT * FROM homework WHERE student_id=? AND reviewed_at>=? AND reviewed_at<?', [studentId, a, b]), scores = work.filter(w => w.score !== null).map(w => w.score);
  return { student: student.name, month, lessons: lessons.length, minutes: lessons.reduce((s, l) => s + l.duration, 0), homework: work.length, average: scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null, balance: await balance(db, studentId), goals: await rows(db, 'SELECT * FROM goals WHERE student_id=?', [studentId]), progress: await rows(db, 'SELECT * FROM progress_notes WHERE student_id=? AND created_at>=? AND created_at<? ORDER BY created_at', [studentId, a, b]), topics: lessons.map(l => l.title) };
}
export async function createReport(db, user, data) {
  teacher(user); await studentExists(db, data.student_id);
  const content = await reportData(db, data.student_id, text(data.month, 7, true)); content.comment = text(data.comment ?? '');
  const existing = await one(db, 'SELECT * FROM reports WHERE student_id=? AND month=?', [data.student_id, data.month]);
  if (existing?.published_at) throw new Problem('Этот отчёт уже опубликован. Для изменения отредактируйте комментарий.', 409);
  const id = existing?.id || uid();
  if (existing) await rows(db, 'UPDATE reports SET body=? WHERE id=?', [JSON.stringify(content), id]);
  else await insert(db, 'reports', { id, student_id: data.student_id, month: data.month, body: JSON.stringify(content), created_at: now() }); return { id };
}
export async function publishReport(db, user, id, data) {
  teacher(user); const report = await one(db, 'SELECT * FROM reports WHERE id=?', [id], true), content = JSON.parse(report.body);
  content.comment = text(data.comment ?? content.comment ?? '');
  await rows(db, 'UPDATE reports SET body=?,published_at=? WHERE id=?', [JSON.stringify(content), now(), id]);
  await notify(db, report.student_id, 'Ваш отчёт за месяц готов', report.month, 'reports', 'report:' + id); return { ok: true };
}
export async function saveSettings(db, user, data) {
  teacher(user); const allowed = { studio_name: v => text(v, 80, true), cancel_hours: v => integer(v, 0, 168), late_cancel_charge: Boolean, sbp_url: safeUrl, reminder_hours: v => integer(v, 1, 72), low_balance: v => integer(v, 0, 10) };
  for (const [key, validate] of Object.entries(allowed)) if (key in data) await rows(db, 'UPDATE settings SET value=? WHERE key=?', [JSON.stringify(validate(data[key])), key]);
  await audit(db, user, 'settings.updated', 'studio'); return { ok: true };
}
export async function saveProfile(db, user, data) {
  await rows(db, 'UPDATE users SET name=?,timezone=?,email_enabled=?,telegram_enabled=? WHERE id=?', [text(data.name ?? user.name, 100, true), zone(data.timezone ?? user.timezone), Number(Boolean(data.email_enabled ?? user.email_enabled)), Number(Boolean(data.telegram_enabled ?? user.telegram_enabled)), user.id]); return { ok: true };
}
export async function adjustBalance(db, user, id, data) {
  teacher(user); await studentExists(db, id); const source = 'adjustment:' + text(data.request_key, 100, true);
  if (await one(db, 'SELECT id FROM ledger WHERE source=?', [source])) return { ok: true };
  const delta = integer(data.delta, -1000, 1000); if (!delta) throw new Problem('Укажите ненулевую корректировку.');
  await insert(db, 'ledger', { id: uid(), student_id: id, delta, reason: text(data.reason, 500, true), source, created_at: now() }); await audit(db, user, 'balance.adjusted', id); return { ok: true };
}
export async function csvExport(db, user, kind) {
  teacher(user); let data, headers;
  if (kind === 'payments') { data = await rows(db, 'SELECT u.name,u.email,p.created_at,p.paid_at,p.amount,p.lessons,p.status,p.reference FROM payments p JOIN users u ON u.id=p.student_id ORDER BY p.created_at DESC'); headers = ['Ученик', 'Email', 'Создан', 'Подтверждён', 'Копейки', 'Занятия', 'Статус', 'Номер операции']; }
  else if (kind === 'students') { data = await rows(db, 'SELECT u.name,u.email,s.level,s.focus,COALESCE((SELECT SUM(delta) FROM ledger WHERE student_id=u.id),0) AS balance FROM users u JOIN students s ON s.user_id=u.id'); headers = ['Ученик', 'Email', 'Уровень', 'Цель', 'Баланс']; }
  else throw new Problem('Неизвестный экспорт.', 404);
  const esc = v => { let s = String(v ?? ''); if (typeof v === 'string' && /^[=+\-@\t\r]/.test(v.trimStart())) s = "'" + s; return /[;"\r\n]/.test(s) ? '"' + s.replaceAll('"', '""') + '"' : s; };
  return '\uFEFF' + [headers, ...data.map(Object.values)].map(row => row.map(esc).join(';')).join('\r\n') + '\r\n';
}
export async function upload(db, user, data, rawOverride = null) {
  let type = data.entity_type, entity = data.entity_id, student = null;
  const table = { homework: 'homework', payment: 'payments', material: 'materials' }[type];
  if (table) {
    const target = await one(db, `SELECT * FROM ${table} WHERE id=?`, [entity], true); student = target.student_id;
    if (type === 'material') teacher(user); else owns(user, student);
    if (type === 'payment' && ['paid', 'rejected', 'voided'].includes(target.status)) throw new Problem('Вложения обработанного платежа нельзя изменять.', 409);
    if (type === 'homework' && user.role === 'student' && target.status === 'reviewed') throw new Problem('Задание уже проверено.', 409);
  } else if (type === 'sbp_qr') { teacher(user); entity = 'studio'; }
  else throw new Problem('Неизвестный тип вложения.');
  const filename = text(data.filename, 180, true).split(/[\\/]/).pop(), raw = rawOverride || storage.decodeUpload(data.data), mime = storage.detectMime(raw, filename);
  if (!raw.length || raw.length>10*1024**2) throw new Problem('Размер файла должен быть от 1 байта до 10 МБ.');
  if (!mime || (type === 'sbp_qr' && !mime.startsWith('image/'))) throw new Problem('Поддерживаются PDF, PNG, JPEG, WebP, TXT, Office и аудио. Для QR нужен PNG или JPEG.');
  if (Number(await scalar(db, 'SELECT COALESCE(SUM(size),0) FROM files WHERE owner_id=?', [user.id])) + raw.length > 1024 ** 3) throw new Problem('Достигнут лимит вложений 1 ГБ.', 413);
  if (Number(await scalar(db, 'SELECT COALESCE(SUM(size),0) FROM files')) + raw.length > 800 * 1024 ** 2) throw new Problem('Достигнут общий лимит файлов бесплатной студии 800 МБ. Новые загрузки приостановлены.', 413);
  const id = uid(); await insert(db, 'files', { id, owner_id: user.id, student_id: student, entity_type: type, entity_id: entity, filename, mime, size: raw.length, created_at: now() });
  await storage.put(id, raw, mime);
  if (type === 'sbp_qr') await rows(db, 'UPDATE settings SET value=? WHERE key=?', [JSON.stringify(id), 'sbp_qr_file_id']); return { id, filename };
}
export async function uploadTicket(db,user,data) {
  await rateLimit(db,'upload:'+user.id,40,3600);
  const size=integer(data.size,1,10*1024**2),filename=text(data.filename,180,true),type=data.entity_type,table={homework:'homework',payment:'payments',material:'materials'}[type];
  if (table) {
    const target=await one(db,`SELECT * FROM ${table} WHERE id=?`,[data.entity_id],true);
    if (type==='material') teacher(user); else owns(user,target.student_id);
    if (type==='payment' && ['paid','rejected','voided'].includes(target.status)) throw new Problem('Вложения обработанного платежа нельзя изменять.',409);
    if (type==='homework' && user.role==='student' && target.status==='reviewed') throw new Problem('Задание уже проверено.',409);
  } else if (type==='sbp_qr') teacher(user); else throw new Problem('Неизвестный тип вложения.');
  if (Number(await scalar(db,'SELECT COALESCE(SUM(size),0) FROM files'))+size>800*1024**2) throw new Problem('Достигнут лимит файлов бесплатной студии.',413);
  const pending=uid(),token=await makeToken(db,user.id,'upload',600);
  await rows(db,'UPDATE tokens SET payload=? WHERE token_hash=?',[JSON.stringify({pending,size,filename,entity_type:type,entity_id:data.entity_id}),digest(token)]);
  return {token,url:await storage.signedUpload(pending)};
}
export async function uploadFinish(db,user,data) {
  const record=await tokenRecord(db,data.token,'upload');
  if (record.user_id!==user.id) throw new Problem('Нет доступа к загрузке.',403);
  const payload=JSON.parse(record.payload),raw=await storage.get(payload.pending);
  if (raw.length!==payload.size) throw new Problem('Размер загруженного файла не совпадает.');
  const result=await upload(db,user,payload,raw);
  // Keep the staging object's cleanup record for at least the 2-hour signed URL
  // lifetime: the URL must never address the final, immutable attachment.
  await rows(db,"UPDATE tokens SET kind='upload_cleanup',expires_at=? WHERE token_hash=?",[Date.now()/1000+7500,record.token_hash]);
  await storage.remove(payload.pending);
  return result;
}
