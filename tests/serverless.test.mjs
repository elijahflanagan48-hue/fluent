import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { passwordHash, verifyPassword, equal, integer, safeUrl, placeholders, uid, now, digest, insert, balance } from '../serverless/core.mjs';
import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import pg from 'pg';
import { handle } from '../serverless/api.mjs';
import { detectMime, decodeUpload } from '../serverless/storage.mjs';
import { DateTime } from 'luxon';
import { backup } from '../serverless/backup.mjs';
import { unzipSync } from 'fflate';

let admin,runtime;
if (process.env.FLUENT_TEST_POSTGRES_DSN) {
  const address=new URL(process.env.FLUENT_TEST_POSTGRES_DSN);
  if (!['127.0.0.1','localhost'].includes(address.hostname) || address.pathname!=='/fluent_test') throw Error('Refusing to modify a non-disposable database');
  admin=new pg.Client({connectionString:address.href}); await admin.connect();
  await admin.query(execFileSync('python3',['-B','manage.py','cloud-schema'],{encoding:'utf8'}));
  const password=randomBytes(24).toString('hex'); await admin.query("ALTER ROLE fluent_runtime LOGIN PASSWORD '"+password+"'");
  runtime=new pg.Client({host:address.hostname,port:Number(address.port||5432),database:'fluent_test',user:'fluent_runtime',password}); await runtime.connect();
  await runtime.query('SET search_path TO fluent, pg_catalog');
  after(async()=>{await runtime.end();await admin.end();});
}
async function database() {
  if (runtime) {
    const tables=(await admin.query("SELECT tablename FROM pg_tables WHERE schemaname='fluent' AND tablename<>'schema_version'")).rows.map(r=>'fluent.'+r.tablename);
    await admin.query('TRUNCATE '+tables.join(',')+' RESTART IDENTITY CASCADE');
    return {async query(sql,args=[]) {return (await runtime.query(placeholders(sql),args)).rows;},async begin(){await runtime.query('BEGIN');await runtime.query('SELECT pg_advisory_xact_lock(1937036646)');},async commit(){await runtime.query('COMMIT');},async rollback(){await runtime.query('ROLLBACK');},close(){}};
  }
  const raw=new DatabaseSync(':memory:'); raw.exec(readFileSync('backend/schema.sql','utf8'));
  // Cloud schema has explicit rowid columns; SQLite exposes an implicit rowid.
  return {raw,async query(sql,args=[]) { return raw.prepare(sql).all(...args.map(v=>v===undefined?null:v)); },async begin(){raw.exec('BEGIN IMMEDIATE');},async commit(){raw.exec('COMMIT');},async rollback(){raw.exec('ROLLBACK');},close(){}};
}
const defaults={studio_name:'fluent.',teacher_name:'Преподаватель',cancel_hours:24,late_cancel_charge:true,referral_bonus:1,referral_enabled:false,payment_mode:'manual_sbp',sbp_url:'',sbp_qr_file_id:'',reminder_hours:24,low_balance:2,currency:'RUB'};
async function fixture() {
  const db=await database(); for (const [key,value] of Object.entries(defaults)) await insert(db,'settings',{key,value:JSON.stringify(value)});
  const teacher=uid(),student=uid(),other=uid();
  for (const [id,role] of [[teacher,'teacher'],[student,'student'],[other,'student']]) {
    await insert(db,'users',{id,role,name:role,email:id+'@example.test',created_at:now()});
    if (role==='student') await insert(db,'students',{user_id:id,referral_code:id.slice(0,8)});
    await insert(db,'sessions',{token_hash:digest(id),user_id:id,csrf:'csrf-'+id,expires_at:Date.now()/1000+1000});
  }
  const request=async(path,data,id=teacher,headers={})=>{
    const req=new Request('https://fluent.example.test/api'+path,{method:data===undefined?'GET':'POST',headers:{...(data===undefined?{}:{'Content-Type':'application/json','Origin':'https://fluent.example.test'}),...(id?{Cookie:'fluent_session='+id,'X-CSRF-Token':'csrf-'+id}:{}),...headers},body:data===undefined?undefined:JSON.stringify(data)});
    const response=await handle(req,{ip:'test'},async()=>db); const body=await response.json(); return {status:response.status,body,headers:response.headers};
  };
  return {db,teacher,student,other,request};
}
process.env.FLUENT_BASE_URL='https://fluent.example.test';
test('existing Python PBKDF2 format remains compatible',async()=>{
  const hash=await passwordHash('Existing-password-2026'); assert.match(hash,/^pbkdf2_sha256\$600000\$/);
  assert.equal(await verifyPassword('Existing-password-2026',hash),true); assert.equal(await verifyPassword('wrong',hash),false);
  assert.equal(equal(undefined,''),false); assert.equal(equal('a','a'),true);
});
test('SQL placeholders preserve strings and percent signs',()=>assert.equal(placeholders("SELECT '?' AS literal,x FROM t WHERE a=? AND b LIKE '10%'"),"SELECT '?' AS literal,x FROM t WHERE a=$1 AND b LIKE '10%'"));
test('input validation rejects unsafe links and non-integral numbers',()=>{
  for(const n of [true,'1.0','01',1.1,null]) assert.throws(()=>integer(n));
  for(const u of ['javascript:alert(1)','https://user:pass@example.org','http://example.org']) assert.throws(()=>safeUrl(u));
  assert.equal(integer('4'),4); assert.equal(safeUrl('https://example.org'),'https://example.org');
});
test('upload validation checks signature and base64',()=>{
  assert.equal(detectMime(Buffer.from('%PDF-1.7'),'file.pdf'),'application/pdf'); assert.equal(detectMime(Buffer.from('<script>'),'file.png'),null);
  assert.throws(()=>decodeUpload('not valid!')); assert.throws(()=>decodeUpload('')); assert.equal(decodeUpload('YQ==').toString(),'a');
});
test('authentication, origin and CSRF are enforced',async()=>{
  const f=await fixture(); assert.equal((await f.request('/bootstrap',undefined,null)).status,401);
  assert.equal((await f.request('/settings',{studio_name:'bad'},f.teacher,{'X-CSRF-Token':'wrong'})).status,403);
  assert.equal((await f.request('/settings',{studio_name:'bad'},f.teacher,{Origin:'https://attacker.test'})).status,403);
  assert.equal((await f.request('/settings',{studio_name:'bad'},f.student)).status,403);
});
test('student records and private notes do not leak in bootstrap',async()=>{
  const f=await fixture(); await f.db.query('UPDATE students SET private_notes=?',['teacher only']);
  const r=await f.request('/bootstrap',undefined,f.student); assert.equal(r.status,200); assert.equal(r.body.students.length,1); assert.equal(r.body.students[0].id,f.student); assert.equal('private_notes' in r.body.students[0],false); assert.equal('referrer_id' in r.body.students[0],false); assert.equal(r.body.hosting.serverless,true);
});
test('payment claim does not credit lessons; confirmation is idempotent',async()=>{
  const f=await fixture(); const pack=await f.request('/packages',{name:'Eight',lessons:8,amount:800000}); assert.equal(pack.status,200);
  const order=await f.request('/payments',{package_id:pack.body.id},f.student); assert.equal(order.status,200);
  assert.equal((await f.request('/payments',{package_id:pack.body.id},f.student)).body.id,order.body.id);
  assert.equal((await f.request('/payments/'+order.body.id+'/claim',{},f.other)).status,403);
  assert.equal((await f.request('/payments/'+order.body.id+'/claim',{},f.student)).status,200); assert.equal(await balance(f.db,f.student),0);
  assert.equal((await f.request('/payments/'+order.body.id+'/confirm',{},f.student)).status,403);
  for(let i=0;i<2;i++) assert.equal((await f.request('/payments/'+order.body.id+'/confirm',{})).status,200);
  assert.equal(await balance(f.db,f.student),8);
  for(let i=0;i<2;i++) assert.equal((await f.request('/payments/'+order.body.id+'/void',{reason:'Correction'})).status,200);
  assert.equal(await balance(f.db,f.student),0);
});
test('lesson completion and late-cancel charge apply once',async()=>{
  const f=await fixture(),id=uid(); await insert(f.db,'lessons',{id,student_id:f.student,title:'Past',starts_at:'2020-01-01T10:00:00Z',duration:50,created_at:now(),updated_at:now()});
  assert.equal((await f.request('/lessons/'+id+'/complete',{},f.student)).status,403);
  for(let i=0;i<2;i++) assert.equal((await f.request('/lessons/'+id+'/complete',{})).status,200);
  assert.equal(await balance(f.db,f.student),-1);
  const future=DateTime.utc().plus({hours:1}).toISO({suppressMilliseconds:true}),created=await f.request('/lessons',{student_id:f.student,title:'Near',starts_at:future}); const next=created.body.ids[0];
  assert.equal((await f.request('/lessons/'+next+'/cancel',{},f.student)).status,409);
  assert.equal((await f.request('/lessons/'+next+'/cancel',{accept_charge:true},f.student)).status,200); assert.equal(await balance(f.db,f.student),-2);
  assert.equal((await f.request('/lessons/'+next+'/cancel',{accept_charge:true},f.student)).status,409); assert.equal(await balance(f.db,f.student),-2);
});
test('recurring lessons preserve wall time across DST and reject conflicts',async()=>{
  const f=await fixture(); await f.db.query('UPDATE users SET timezone=? WHERE id=?',['Europe/London',f.teacher]);
  const r=await f.request('/lessons',{student_id:f.student,title:'Weekly',starts_at:'2027-03-21T10:00:00Z',repeat:2}); assert.equal(r.status,200);
  const lessons=await f.db.query('SELECT starts_at FROM lessons ORDER BY starts_at'); assert.equal(lessons[1].starts_at,'2027-03-28T09:00:00Z');
  assert.equal((await f.request('/lessons',{student_id:f.other,title:'Conflict',starts_at:'2027-03-28T09:10:00Z'})).status,409);
});
test('teacher-only calendar reschedule rejects student requests and stale moves without changing balance',async()=>{
  const f=await fixture(),start=DateTime.utc().plus({days:10}).startOf('hour');
  const create=async(student,when)=>{const r=await f.request('/lessons',{student_id:student,title:'Drag fixture',starts_at:when.toISO(),duration:50});assert.equal(r.status,200);return r.body.ids[0];};
  const id=await create(f.student,start),blocked=await create(f.other,start.plus({hours:2}));
  const original=(await f.db.query('SELECT starts_at FROM lessons WHERE id=?',[id]))[0].starts_at;
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{starts_at:start.plus({hours:2,minutes:10}).toISO(),duration:50})).status,409);
  assert.equal((await f.db.query('SELECT starts_at FROM lessons WHERE id=?',[id]))[0].starts_at,original,'conflict leaves original lesson intact');
  const moved=start.plus({days:1});
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{starts_at:moved.toISO(),duration:50})).status,200);
  assert.equal(+DateTime.fromISO((await f.db.query('SELECT starts_at FROM lessons WHERE id=?',[id]))[0].starts_at),+moved);
  const slot=await f.request('/slots',{starts_at:start.plus({days:2}).toISO(),duration:60});assert.equal(slot.status,200);
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{slot_id:slot.body.id},f.other)).status,403);
  assert.notEqual((await f.request('/lessons/'+id+'/reschedule',{starts_at:start.plus({days:3}).toISO()},f.student)).status,200,'student cannot choose arbitrary time');
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{slot_id:slot.body.id},f.student)).status,403);
  assert.equal((await f.db.query('SELECT duration FROM lessons WHERE id=?',[id]))[0].duration,50);
  assert.equal((await f.db.query('SELECT id FROM slots WHERE id=?',[slot.body.id])).length,1,'student cannot consume slot by rescheduling');
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{starts_at:start.plus({days:3}).toISO(),expected_starts_at:original,duration:50})).status,409,'stale drag/undo cannot overwrite a newer move');
  assert.equal((await f.request('/lessons/'+id+'/reschedule',{starts_at:start.plus({days:3}).toISO(),expected_starts_at:moved.toISO(),expected_duration:60,duration:50})).status,409,'stale duration rejected');
  const near=await create(f.student,DateTime.utc().plus({hours:1}));
  assert.equal((await f.request('/lessons/'+near+'/reschedule',{slot_id:'missing'},f.student)).status,403,'students can never reschedule');
  await f.db.query("UPDATE lessons SET status='completed' WHERE id=?",[blocked]);
  assert.equal((await f.request('/lessons/'+blocked+'/reschedule',{starts_at:start.plus({days:4}).toISO()})).status,409);
  assert.equal(await balance(f.db,f.student),0,'rescheduling never charges or credits lessons');
});
test('booking consumes overlapping slots and cannot double book',async()=>{
  const f=await fixture(); await insert(f.db,'ledger',{id:uid(),student_id:f.student,delta:2,reason:'Initial',source:uid(),created_at:now()});
  const slot=await f.request('/slots',{starts_at:'2027-09-01T12:00:00Z',duration:50});
  assert.equal((await f.request('/slots/'+slot.body.id+'/book',{},f.student)).status,200);
  assert.equal((await f.request('/slots/'+slot.body.id+'/book',{},f.other)).status,404);
});
test('homework submission, review, reopen and ownership',async()=>{
  const f=await fixture(),task=await f.request('/homework',{student_id:f.student,title:'Essay',instructions:'Write it',due_at:'2027-01-01T12:00:00Z'}),id=task.body.id;
  assert.equal((await f.request('/homework/'+id+'/submit',{answer:'x'},f.other)).status,403);
  assert.equal((await f.request('/homework/'+id+'/submit',{answer:''},f.student)).status,400);
  assert.equal((await f.request('/homework/'+id+'/submit',{answer:'My essay'},f.student)).status,200);
  assert.equal((await f.request('/homework/'+id+'/review',{feedback:'Good',score:85})).status,200);
  assert.equal((await f.request('/homework/'+id+'/submit',{answer:'Edit'},f.student)).status,409);
  assert.equal((await f.request('/homework/'+id+'/reopen',{})).status,200);
});
test('goals, reports and materials retain role permissions',async()=>{
  const f=await fixture(),goal=await f.request('/goals',{student_id:f.other,title:'Own',target:4},f.student);
  assert.equal(goal.status,200); assert.equal((await f.db.query('SELECT student_id FROM goals'))[0].student_id,f.student);
  assert.equal((await f.request('/goals/'+goal.body.id,{progress:2},f.other)).status,403);
  assert.equal((await f.request('/materials',{title:'Private',body:'x',student_id:f.student},f.student)).status,403);
  const report=await f.request('/reports',{student_id:f.student,month:'2026-10'}); assert.equal(report.status,200);
  assert.equal((await f.request('/bootstrap',undefined,f.student)).body.reports.length,0);
  await f.request('/reports/'+report.body.id+'/publish',{}); assert.equal((await f.request('/bootstrap',undefined,f.student)).body.reports.length,1);
});
test('login abuse counter persists on failed authentication',async()=>{
  const f=await fixture(); for(let i=0;i<10;i++) assert.equal((await f.request('/auth/login',{email:'unknown@example.test',password:'bad'},null)).status,401);
  assert.equal((await f.request('/auth/login',{email:'unknown@example.test',password:'bad'},null)).status,429);
});
test('owner setup remains closed after first owner exists',async()=>{
  const f=await fixture(); process.env.FLUENT_SETUP_TOKEN='a'.repeat(64);
  assert.equal((await f.request('/auth/setup',{setup_token:'a'.repeat(64),email:'new@example.test',name:'Owner',password:'Test-password-2026'},null)).status,409);
  assert.equal((await f.request('/auth/info',undefined,null)).body.setup_enabled,false);
});
test('direct file upload checks owner, content and one-use completion',async()=>{
  const f=await fixture(),task=await f.request('/homework',{student_id:f.student,title:'Files',instructions:'Send',due_at:'2027-01-01T12:00:00Z'});
  process.env.SUPABASE_URL='https://lvuugfbefimwnskcnvyg.supabase.co'; process.env.SUPABASE_SECRET_KEY='test-only-not-a-real-key';
  const saved=globalThis.fetch,bytes=Buffer.from('%PDF-1.7\nTest'),calls=[];
  globalThis.fetch=async(url,options={})=>{
    calls.push({url,options});
    assert.equal(options.redirect,'error'); assert.equal(options.headers.apikey,'test-only-not-a-real-key');
    const path=new URL(url).pathname;
    if(path.includes('/upload/sign/'))return Response.json({url:'/object/upload/sign/fluent-private/'+path.split('/').pop()+'?token=disposable-test'});
    if(path.includes('/authenticated/'))return new Response(bytes);
    return Response.json([]);
  };
  try {
    const payload={filename:'answer.pdf',size:bytes.length,entity_type:'homework',entity_id:task.body.id};
    assert.equal((await f.request('/files/upload-ticket',payload,f.other)).status,403); assert.equal(calls.length,0);
    const ticket=await f.request('/files/upload-ticket',payload,f.student); assert.equal(ticket.status,200); assert.match(ticket.body.url,/^https:\/\/lvuugfbefimwnskcnvyg\.supabase\.co\/storage\/v1\/object\/upload\/sign\//);
    assert.equal((await f.request('/files/upload-finish',{token:ticket.body.token},f.other)).status,403);
    const finished=await f.request('/files/upload-finish',{token:ticket.body.token},f.student); assert.equal(finished.status,200);
    assert.equal((await f.db.query('SELECT mime,size,student_id FROM files'))[0].mime,'application/pdf');
    assert.equal((await f.request('/files/upload-finish',{token:ticket.body.token},f.student)).status,400);
  } finally {globalThis.fetch=saved;delete process.env.SUPABASE_SECRET_KEY;}
});
test('backup uses separate private bucket and portable SQLite without sessions',async()=>{
  const f=await fixture(),saved=globalThis.fetch;let archived;
  process.env.SUPABASE_URL='https://lvuugfbefimwnskcnvyg.supabase.co';process.env.SUPABASE_SECRET_KEY='test-only-not-a-real-key';
  globalThis.fetch=async(url,options={})=>{
    assert.ok(url.includes('/fluent-backups'));
    if(url.includes('/object/list/'))return Response.json([]);
    archived=options.body;return Response.json({});
  };
  try {
    const name=await backup(f.db,false);assert.match(name,/^fluent-[0-9TZ-]+\.zip$/);
    const files=unzipSync(archived);assert.deepEqual(Object.keys(files),['fluent.sqlite3']);
    assert.equal(Buffer.from(files['fluent.sqlite3']).subarray(0,15).toString(),'SQLite format 3');
  } finally {globalThis.fetch=saved;delete process.env.SUPABASE_SECRET_KEY;}
});
