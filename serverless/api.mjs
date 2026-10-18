import { randomBytes } from 'node:crypto';
import { Problem, connect, rows, one, insert, uid, now, seconds, digest, equal, passwordHash, verifyPassword, tokenRecord, makeToken, publicUser, teacher, audit, text, email, baseUrl, rateLimit, dt } from './core.mjs';
import * as service from './service.mjs';
import * as integrations from './integrations.mjs';
import * as storage from './storage.mjs';
import { backup } from './backup.mjs';

export async function sessionUser(db, req) {
  const raw = /(?:^|;\s*)fluent_session=([A-Za-z0-9_-]+)/.exec(req.headers.get('cookie') || '')?.[1] || '';
  return one(db, 'SELECT u.*,s.csrf,s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1', [digest(raw), seconds()]);
}
async function makeSession(db, user) {
  const raw = randomBytes(40).toString('base64url'), csrf = randomBytes(32).toString('base64url');
  await insert(db, 'sessions', { token_hash:digest(raw),user_id:user.id,csrf,expires_at:seconds()+7*86400 });
  return [{user:publicUser(user),csrf},200,{'Set-Cookie':`fluent_session=${raw}; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800${baseUrl().startsWith('https://')?'; Secure':''}`}];
}
async function publicRoute(db, req, path, data, context) {
  const method = req.method;
  if (path === '/api/health' && method === 'GET') { await one(db,'SELECT version FROM schema_version WHERE version=1',[],true); return [{ok:true},200,{}]; }
  if (path === '/api/auth/info' && method === 'GET') {
    const ready = Boolean(await one(db,"SELECT id FROM users WHERE role='teacher'"));
    return [{demo:false,ready,setup_enabled:!ready&&(process.env.FLUENT_SETUP_TOKEN||'').length>=32},200,{}];
  }
  if (['/api/auth/setup','/api/auth/login','/api/auth/forgot','/api/auth/accept'].includes(path) && method === 'POST') {
    // Commit abuse counters separately. Invalid passwords must not roll them back.
    try { await rateLimit(db,(path.endsWith('/setup')?'setup:':'auth:')+(context.ip||'unknown'),path.endsWith('/setup')?8:30,path.endsWith('/setup')?3600:900); }
    finally { await db.commit(); }
    await db.begin();
    if (path.endsWith('/setup')) {
      const expected = process.env.FLUENT_SETUP_TOKEN || '';
      if (expected.length < 32) throw new Problem('Открытие студии через браузер не настроено.',404);
      if (!equal(data.setup_token,expected)) throw new Problem('Код открытия студии не совпадает.',403);
      if (await one(db,"SELECT id FROM users WHERE role='teacher'")) throw new Problem('Студия уже открыта. Войдите в свой кабинет.',409);
      const id=uid();
      await insert(db,'users',{id,email:email(data.email),name:text(data.name,100,true),role:'teacher',password_hash:await passwordHash(data.password),created_at:now()});
      await audit(db,null,'studio.opened',id); return makeSession(db,await one(db,'SELECT * FROM users WHERE id=?',[id],true));
    }
    if (path.endsWith('/login')) {
      const addr=email(data.email);
      try { await rateLimit(db,'login:'+digest(addr),10,900); } finally { await db.commit(); }
      await db.begin();
      const user=await one(db,'SELECT * FROM users WHERE email=? AND active=1',[addr]);
      if (!await verifyPassword(data.password,user?.password_hash)) throw new Problem('Неверный email или пароль.',401);
      await rows(db,'DELETE FROM rate_limits WHERE key=?',['login:'+digest(addr)]); return makeSession(db,user);
    }
    if (path.endsWith('/forgot')) {
      if (!integrations.configured('SMTP_HOST','SMTP_FROM')) throw new Problem('Восстановление по email пока не подключено. Попросите преподавателя выдать новую ссылку входа.',503);
      const addr=email(data.email),user=await one(db,'SELECT * FROM users WHERE email=? AND active=1',[addr]);
      if (user) await insert(db,'outbox',{id:uid(),user_id:user.id,channel:'email',subject:'Восстановление доступа — fluent.',body:baseUrl()+'/#reset='+await makeToken(db,user.id,'reset',3600),event_key:'reset:'+uid()});
      return [{ok:true},200,{}];
    }
    const token=await tokenRecord(db,data.token,data.kind==='reset'?'reset':'invite');
    await rows(db,'UPDATE users SET password_hash=? WHERE id=?',[await passwordHash(data.password),token.user_id]);
    await rows(db,"DELETE FROM tokens WHERE user_id=? AND kind IN ('invite','reset')",[token.user_id]);
    await rows(db,'DELETE FROM sessions WHERE user_id=?',[token.user_id]);
    return makeSession(db,await one(db,'SELECT * FROM users WHERE id=? AND active=1',[token.user_id],true));
  }
  if (path === '/api/webhooks/telegram' && method === 'POST') {
    if (!process.env.TELEGRAM_WEBHOOK_SECRET || !equal(req.headers.get('X-Telegram-Bot-Api-Secret-Token'),process.env.TELEGRAM_WEBHOOK_SECRET)) throw new Problem('Недействительная подпись.',403);
    const message=data.message||{},content=message.text||'',chat=message.chat||{};
    if (chat.type==='private' && typeof content==='string' && content.startsWith('/start ')) {
      let record; try { record=await tokenRecord(db,content.split(' ')[1],'telegram'); } catch { return [{ok:true},200,{}]; }
      await rows(db,'UPDATE users SET telegram_chat=?,telegram_enabled=1 WHERE id=?',[String(chat.id),record.user_id]);
      await rows(db,'DELETE FROM tokens WHERE token_hash=?',[record.token_hash]);
    }
    return [{ok:true},200,{}];
  }
  return null;
}
async function apiRoute(db,req,path,data,user) {
  const method=req.method;
  if (method==='GET') {
    if (path==='/api/auth/me') return [{user:publicUser(user),csrf:user.csrf},200,{}];
    if (path==='/api/bootstrap') return [await service.bootstrap(db,user),200,{}];
    if (path==='/api/activity') return [await service.activity(db,user,new URL(req.url).searchParams.get('conversation')||''),200,{}];
    if (path.startsWith('/api/files/')) {
      const file=await one(db,'SELECT * FROM files WHERE id=?',[path.split('/').pop()],true);
      if (!await service.canReadFile(db,user,file)) throw new Problem('Нет доступа к вложению.',403);
      // Direct short-lived download avoids serverless response-size limits.
      return ['',303,{Location:await storage.signedDownload(file.id,file.filename)}];
    }
    if (path.startsWith('/api/export/')) { const kind=path.split('/').pop(); return [await service.csvExport(db,user,kind),200,{'Content-Type':'text/csv; charset=utf-8','Content-Disposition':`attachment; filename="fluent-${kind}.csv"`}]; }
    if (path.startsWith('/api/backups/')) { teacher(user); const name=path.split('/').pop(); if (!/^fluent-[0-9TZ-]+\.zip$/.test(name)) throw new Problem('Архив не найден.',404); return ['',303,{Location:await storage.signedDownload('backups/'+name,name)}]; }
    if (path==='/api/calendar.ics') {
      const [clause,args]=service.scoped(user),esc=v=>String(v).replaceAll('\\','\\\\').replaceAll(';','\\;').replaceAll(',','\\,').replaceAll('\n','\\n').replaceAll('\r',''),stamp=d=>d.toUTC().toFormat("yyyyMMdd'T'HHmmss'Z'");
      const lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//fluent//Calendar//RU','CALSCALE:GREGORIAN'];
      for (const lesson of await rows(db,`SELECT * FROM lessons WHERE ${clause} AND status='scheduled'`,args)) lines.push('BEGIN:VEVENT','UID:'+lesson.id+'@fluent','DTSTAMP:'+stamp(dt(now())),'DTSTART:'+stamp(dt(lesson.starts_at)),'DTEND:'+stamp(dt(lesson.starts_at).plus({minutes:lesson.duration})),'SUMMARY:'+esc(lesson.title),'DESCRIPTION:'+esc(lesson.meeting_url),'END:VEVENT');
      lines.push('END:VCALENDAR'); return [lines.join('\r\n')+'\r\n',200,{'Content-Type':'text/calendar; charset=utf-8','Content-Disposition':'attachment; filename="fluent.ics"'}];
    }
    if (path==='/api/integrations/google/callback') {
      const q=new URL(req.url).searchParams,record=await tokenRecord(db,q.get('state'),'google');
      if (record.user_id!==user.id) throw new Problem('Подключение начато другим пользователем.',403);
      if (q.get('error')) throw new Problem('Подключение Google отменено.');
      const token=await integrations.googleExchange(q.get('code'));
      if (!token.refresh_token) throw new Problem('Google не выдал доступ для синхронизации. Повторите подключение.');
      token.expires_at=seconds()+token.expires_in;
      await rows(db,"INSERT INTO integrations VALUES (?,'google',?) ON CONFLICT(user_id,provider) DO UPDATE SET data=excluded.data",[user.id,JSON.stringify(token)]);
      await rows(db,'DELETE FROM calendar_events WHERE user_id=?',[user.id]); await rows(db,'DELETE FROM sync_state WHERE provider=?',['google:'+user.id]); await rows(db,'DELETE FROM tokens WHERE token_hash=?',[record.token_hash]);
      return ['',303,{Location:'/#settings'}];
    }
  }
  if (method==='POST') {
    if (path==='/api/auth/logout') { await rows(db,'DELETE FROM sessions WHERE token_hash=?',[user.token_hash]); return [{ok:true},200,{'Set-Cookie':'fluent_session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0'}]; }
    if (path==='/api/auth/password') {
      if (!await verifyPassword(data.current_password,user.password_hash)) throw new Problem('Текущий пароль не совпадает.',403);
      await rows(db,'UPDATE users SET password_hash=? WHERE id=?',[await passwordHash(data.password),user.id]);
      await rows(db,'DELETE FROM sessions WHERE user_id=? AND token_hash<>?',[user.id,user.token_hash]); return [{ok:true},200,{}];
    }
    if (path==='/api/backups') { teacher(user); const name=await backup(db,true); await audit(db,user,'backup.created',name); return [{url:'/api/backups/'+name},200,{}]; }
    if (path==='/api/files/upload-ticket') return [await service.uploadTicket(db,user,data),200,{}];
    if (path==='/api/files/upload-finish') return [await service.uploadFinish(db,user,data),200,{}];
    const routes={'/api/students':service.addStudent,'/api/lessons':service.addLesson,'/api/slots':service.addSlot,'/api/packages':service.savePackage,'/api/payments':service.createPayment,'/api/homework':service.createHomework,'/api/templates':service.addTemplate,'/api/materials':service.addMaterial,'/api/goals':service.addGoal,'/api/progress':service.addProgress,'/api/messages':service.sendMessage,'/api/reports':service.createReport,'/api/files':service.upload,'/api/settings':service.saveSettings,'/api/profile':service.saveProfile};
    if (routes[path]) return [await routes[path](db,user,data),200,{}];
    let match=/^\/api\/(lessons|payments|homework)\/([a-f0-9]{32})\/(\w+)$/.exec(path);
    if (match) return [await {lessons:service.lessonAction,payments:service.paymentAction,homework:service.homeworkAction}[match[1]](db,user,match[2],match[3],data),200,{}];
    match=/^\/api\/(students|goals|packages|templates|reports|slots)\/([a-f0-9]{32})(?:\/(invite|publish|book|delete|balance))?$/.exec(path);
    if (match) {
      const [,kind,id,action]=match;
      if (kind==='students') return [await (action==='balance'?service.adjustBalance(db,user,id,data):action==='invite'?service.newInvite(db,user,id):service.editStudent(db,user,id,data)),200,{}];
      if (kind==='goals') return [await service.updateGoal(db,user,id,data),200,{}];
      if (kind==='packages') return [await service.savePackage(db,user,data,id),200,{}];
      if (kind==='templates') return [await service.addTemplate(db,user,data,id),200,{}];
      if (kind==='reports' && action==='publish') return [await service.publishReport(db,user,id,data),200,{}];
      if (kind==='slots' && action==='book') return [await service.bookSlot(db,user,id),200,{}];
      if (kind==='slots' && action==='delete') { teacher(user); await rows(db,'DELETE FROM slots WHERE id=?',[id]); return [{ok:true},200,{}]; }
    }
    if (path==='/api/notifications/read') return [await service.readNotifications(db,user,data),200,{}];
    if (path==='/api/integrations/google/connect') return [{url:integrations.googleAuthUrl(await makeToken(db,user.id,'google',600))},200,{}];
    if (path==='/api/integrations/google/disconnect') {
      await rows(db,"DELETE FROM integrations WHERE user_id=? AND provider='google'",[user.id]); await rows(db,'DELETE FROM calendar_events WHERE user_id=?',[user.id]); await rows(db,'DELETE FROM sync_state WHERE provider=?',['google:'+user.id]); return [{ok:true},200,{}];
    }
    if (path==='/api/integrations/google/sync') {
      const [clause,args]=service.scoped(user),lessons=await rows(db,`SELECT * FROM lessons WHERE ${clause} AND (starts_at>? OR id IN (SELECT lesson_id FROM calendar_events WHERE user_id=?))`,[...args,now(),user.id]);
      await integrations.googleSync(db,user,lessons); return [{ok:true,count:lessons.length},200,{}];
    }
    if (path==='/api/integrations/telegram/connect') {
      if (!(await integrations.status(db,user)).telegram) throw new Problem('Telegram-бот ещё не подключён.',503);
      return [{url:'https://t.me/'+process.env.TELEGRAM_BOT_USERNAME+'?start='+await makeToken(db,user.id,'telegram',600)},200,{}];
    }
    if (path==='/api/integrations/telegram/disconnect') { await rows(db,'UPDATE users SET telegram_chat=NULL,telegram_enabled=0 WHERE id=?',[user.id]); return [{ok:true},200,{}]; }
  }
  throw new Problem('Неизвестный адрес.',404);
}
export async function handle(req,context={},connector=connect) {
  let db;
  const headers={'X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY','Cache-Control':'no-store','Strict-Transport-Security':'max-age=31536000'};
  try {
    const path=new URL(req.url).pathname;
    if (!path.startsWith('/api/') || !['GET','POST'].includes(req.method)) throw new Problem('Неизвестный адрес.',404);
    if (req.method==='POST' && !path.startsWith('/api/webhooks/') && req.headers.get('Origin') && req.headers.get('Origin')!==baseUrl()) throw new Problem('Недопустимый источник запроса.',403);
    let data={};
    if (req.method==='POST') {
      if (!req.headers.get('Content-Type')?.startsWith('application/json')) throw new Problem('Требуется JSON.',415);
      const content=await req.text(); if (Buffer.byteLength(content)>15*1024**2) throw new Problem('Слишком большой запрос.',413);
      try { data=content?JSON.parse(content):{}; } catch { throw new Problem('Некорректный JSON.'); }
      if (!data || typeof data!=='object' || Array.isArray(data)) throw new Problem('Требуется объект JSON.');
    }
    db=await connector(); await db.begin(req.method==='POST');
    let result=await publicRoute(db,req,path,data,context);
    if (!result) {
      const user=await sessionUser(db,req); if (!user) throw new Problem('Войдите в кабинет.',401);
      if (req.method==='POST' && !equal(req.headers.get('X-CSRF-Token'),user.csrf)) throw new Problem('Обновите страницу и повторите действие.',403);
      result=await apiRoute(db,req,path,data,user);
    }
    await db.commit();
    const [body,status,extra]=result;
    const json=typeof body!=='string' && !Buffer.isBuffer(body);
    return new Response(json?JSON.stringify(body):body,{status,headers:{...headers,...(json?{'Content-Type':'application/json; charset=utf-8'}:{}),...extra}});
  } catch (error) {
    if (db) await db.rollback().catch(()=>{});
    const conflict=['23505','23503','23514','23502'].includes(error.code),status=error instanceof Problem?error.status:conflict?409:500;
    // Provider exceptions may contain credentials, SQL, personal data: never log them.
    return Response.json({error:error instanceof Problem?error.message:conflict?'Запись с такими данными уже существует. Обновите страницу.':'Не удалось выполнить действие. Повторите позже.'},{status,headers});
  } finally { if (db) db.close(); }
}
