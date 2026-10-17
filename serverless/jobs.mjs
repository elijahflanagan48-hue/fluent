import { connect, rows, one, settings, now, dt, balance, notify, notifyTeacher, seconds } from './core.mjs';
import * as integrations from './integrations.mjs';
import * as storage from './storage.mjs';
import { backup } from './backup.mjs';
import { scoped } from './service.mjs';

export async function reminders(db) {
  const cfg=await settings(db),current=Date.now();
  for (const lesson of await rows(db,"SELECT * FROM lessons WHERE status='scheduled' AND starts_at>?",[now()])) {
    const hours=(+dt(lesson.starts_at)-current)/3600000;
    if (hours>0 && hours<=cfg.reminder_hours) await notify(db,lesson.student_id,'Скоро урок',lesson.title+' · '+lesson.starts_at,'calendar','reminder:'+lesson.id+':'+lesson.starts_at);
  }
  for (const task of await rows(db,"SELECT * FROM homework WHERE status='assigned'")) {
    const hours=(+dt(task.due_at)-current)/3600000;
    if (hours>0 && hours<=24) await notify(db,task.student_id,'Скоро срок домашнего задания',task.title,'homework','deadline:'+task.id+':'+task.due_at);
  }
  for (const student of await rows(db,"SELECT id FROM users WHERE role='student' AND active=1")) {
    const value=await balance(db,student.id);
    if (value<=cfg.low_balance) { const last=await one(db,'SELECT id FROM ledger WHERE student_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',[student.id]); await notify(db,student.id,'Пора пополнить баланс','Осталось занятий: '+value,'payments','balance:'+(last?.id||'initial')); }
  }
  for (const record of await rows(db,"SELECT token_hash,payload FROM tokens WHERE kind IN ('upload','upload_cleanup') AND expires_at<?",[seconds()])) {
    await storage.remove(JSON.parse(record.payload).pending);
    await rows(db,'DELETE FROM tokens WHERE token_hash=?',[record.token_hash]);
  }
  for (const table of ['sessions','tokens','rate_limits']) await rows(db,`DELETE FROM ${table} WHERE expires_at<?`+(table==='tokens'?" AND kind NOT IN ('upload','upload_cleanup')":''),[seconds()]);
}
async function deliverOne(db) {
  await db.begin();
  const job=await one(db,"SELECT * FROM outbox WHERE (state='pending' OR (state='sending' AND next_attempt<?)) AND next_attempt<=? ORDER BY rowid LIMIT 1",[seconds(),seconds()]);
  if (!job) { await db.commit(); return false; }
  const user=await one(db,'SELECT * FROM users WHERE id=?',[job.user_id],true);
  await rows(db,"UPDATE outbox SET state='sending',next_attempt=? WHERE id=?",[seconds()+120,job.id]); await db.commit();
  let state='skipped',error='',attempts=job.attempts,next=job.next_attempt;
  try {
    if (user.active && job.channel==='telegram' && user.telegram_enabled && user.telegram_chat) { await integrations.sendTelegram(user.telegram_chat,job.subject+'\n'+job.body); state='sent'; }
    if (user.active && job.channel==='email' && (user.email_enabled || job.event_key.startsWith('reset:'))) { await integrations.sendEmail(user.email,job.subject,job.body); state='sent'; }
  } catch { attempts++; state=attempts>=6?'failed':'pending'; next=seconds()+Math.min(3600,60*2**attempts); error='Не удалось доставить уведомление.'; }
  await db.begin(); await rows(db,'UPDATE outbox SET state=?,attempts=?,next_attempt=?,error=? WHERE id=?',[state,attempts,next,error,job.id]); await db.commit(); return true;
}
export async function runJobs() {
  const db=await connect(),deadline=Date.now()+23000;
  try {
    await db.begin(); await reminders(db); await db.commit();
    for (let n=0;n<5 && Date.now()<deadline;n++) if (!await deliverOne(db)) break;
    if (Date.now()<deadline) {
      await db.begin();
      const users=await rows(db,"SELECT u.* FROM users u JOIN integrations i ON i.user_id=u.id WHERE i.provider='google' AND u.active=1");
      for (const user of users) {
        if (Date.now()>deadline) break;
        const [clause,args]=scoped(user),lessons=await rows(db,`SELECT * FROM lessons WHERE ${clause} AND (starts_at>? OR id IN (SELECT lesson_id FROM calendar_events WHERE user_id=?))`,[...args,now(),user.id]);
        try { await integrations.googleSync(db,user,lessons); } catch { await notify(db,user.id,'Не удалось обновить Google Calendar','Проверьте подключение в настройках.','settings','google-error:'+now().slice(0,10)); }
      }
      if (integrations.configured('ZOOM_ACCOUNT_ID','ZOOM_CLIENT_ID','ZOOM_CLIENT_SECRET')) for (const lesson of await rows(db,'SELECT * FROM lessons WHERE zoom_id IS NOT NULL AND starts_at>?',[now()])) {
        if (Date.now()>deadline) break;
        const fingerprint=JSON.stringify([lesson.title,lesson.starts_at,lesson.duration,lesson.status,lesson.zoom_id]);
        if ((await one(db,"SELECT fingerprint FROM sync_state WHERE provider='zoom' AND entity_id=?",[lesson.id]))?.fingerprint===fingerprint) continue;
        try {
          const cancel=['cancelled','late_cancelled'].includes(lesson.status); await integrations.zoomMeeting(lesson,cancel);
          if (cancel) await rows(db,'UPDATE lessons SET zoom_id=NULL,meeting_url=? WHERE id=?',['',lesson.id]);
          await rows(db,"INSERT INTO sync_state VALUES ('zoom',?,?) ON CONFLICT(provider,entity_id) DO UPDATE SET fingerprint=excluded.fingerprint",[lesson.id,fingerprint]);
        } catch { await notifyTeacher(db,'Не удалось обновить Zoom','Проверьте встречу и подключение Zoom в настройках.','settings','zoom-error:'+lesson.id+':'+now().slice(0,10)); }
      }
      await db.commit();
    }
  } catch { await db.rollback().catch(()=>{}); throw new Error('Не удалось выполнить фоновую обработку.'); }
  finally { db.close(); }
}
export async function dailyBackup() {
  const db=await connect();
  try { await db.begin(); await backup(db,false); await db.commit(); }
  catch { await db.rollback().catch(()=>{}); throw new Error('Не удалось создать резервную копию.'); }
  finally { db.close(); }
}
