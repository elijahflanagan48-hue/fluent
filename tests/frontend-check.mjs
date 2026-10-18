import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

const source=await fs.readFile(new URL('../app.js',import.meta.url),'utf8');
new vm.Script(source,{filename:'app.js'});
const node={addEventListener(){},close(){},showModal(){},classList:{},innerHTML:''};
const context=vm.createContext({
  document:{querySelector:()=>node,addEventListener(){},visibilityState:'hidden'},
  window:{addEventListener(){}},setInterval(){},setTimeout(){},clearInterval(){},
  Date,Intl,URL,location:{hash:'#overview'},console,
});
vm.runInContext(source.replace(/boot\(\);\s*$/,''),context);
const at=new Date().toISOString();
const fixture={
  user:{id:'teacher',name:'Test Teacher',role:'teacher',timezone:'Europe/Moscow',email:'t@example.test'},
  settings:{studio_name:'fluent.',cancel_hours:24,late_cancel_charge:true,referral_enabled:true,referral_bonus:1,sbp_url:'',sbp_qr_file_id:'',reminder_hours:24,low_balance:2},
  students:[{id:'student',name:'<script>untrusted()</script>',email:'s@example.test',level:'B2',focus:'Practice',balance:4,active:1,referral_code:'ABC123',timezone:'Europe/Moscow',private_notes:'Private'}],
  teachers:[{id:'teacher',name:'Test Teacher'}],lessons:[{id:'lesson',student_id:'student',title:'Speaking',starts_at:at,duration:50,status:'scheduled',meeting_url:'',note:''}],slots:[],
  payments:[{id:'payment',student_id:'student',lessons:4,amount:600000,status:'claimed',created_at:at}],packages:[{id:'package',name:'Practice',lessons:4,amount:600000,active:1,description:'Four lessons'}],
  ledger:[{id:'entry',student_id:'student',delta:4,reason:'Paid',created_at:at}],homework:[{id:'task',student_id:'student',title:'An essay',kind:'Writing',instructions:'Write <not markup>',due_at:at,status:'assigned',answer:'',feedback:'',score:null}],
  goals:[{id:'goal',student_id:'student',title:'Practice',target:5,progress:1,unit:'steps',deadline:''}],progress:[],materials:[{id:'material',title:'Tips',body:'Keep going',student_id:null,url:''}],messages:[],reports:[],notifications:[],templates:[],referrals:[],files:[],integrations:{},deliveries:[]
};
context.fixture=fixture;
vm.runInContext('S.user=fixture.user;S.data=fixture;S.conversation="student";',context);
// Every device uses a time grid; students have no reschedule controls.
vm.runInContext('S.calendarDate=new Date(fixture.lessons[0].starts_at);',context);
for(const [view,count] of [['day',1],['three',3],['week',7]]){
  const html=vm.runInContext('S.calendarView='+JSON.stringify(view)+';calendarPage()',context);
  assert.equal((html.match(/class="day-column"/g)||[]).length,count);
  assert.ok(html.includes('data-drop-time="08:15"'),'quarter-hour targets');
  assert.ok(html.includes('data-drag-lesson="lesson"'));
  assert.ok(!html.includes('mobile-agenda'));
  assert.ok(!html.includes('<script>untrusted()'));
}
vm.runInContext('S.calendarView="month";',context);
assert.equal((vm.runInContext('calendarPage()',context).match(/class="month-cell/g)||[]).length,42);
vm.runInContext('S.studentFilter="another-student";',context);
assert.ok(!vm.runInContext('calendarPage()',context).includes('data-drag-lesson="lesson"'));
vm.runInContext('S.studentFilter="";S.calendarView="week";S.data.lessons.push({...fixture.lessons[0],id:"move",starts_at:new Date(Date.now()+72*3600000).toISOString()});',context);
const originalStart=vm.runInContext('S.data.lessons.find(l=>l.id==="move").starts_at',context);
const proposed=vm.runInContext('calendarDropProposal("move",dateKey(new Date(Date.now()+96*3600000)),"16:15")',context);
assert.ok(vm.runInContext('localInput('+JSON.stringify(proposed.starts_at)+')',context).endsWith('T16:15'));
assert.equal(vm.runInContext('S.data.lessons.find(l=>l.id==="move").starts_at',context),originalStart);
assert.equal(vm.runInContext('canDragLesson({...fixture.lessons[0],status:"completed"})',context),false);
assert.throws(()=>vm.runInContext('calendarDropProposal("move","2000-01-01","16:30")',context),/будущем/);
vm.runInContext('S.user={...fixture.user,id:"student",role:"student"};',context);
assert.equal(vm.runInContext('canDragLesson(S.data.lessons.find(l=>l.id==="move"))',context),false);
assert.throws(()=>vm.runInContext('calendarDropProposal("move","2030-01-01","10:00")',context),/нельзя перенести/);
const studentCalendar=vm.runInContext('calendarPage()',context);
assert.ok(!studentCalendar.includes('data-drag-handle'));
assert.ok(!studentCalendar.includes('data-drag-lesson'));
assert.ok(studentCalendar.includes('Расписание обновляет преподаватель'));
vm.runInContext('S.user={...fixture.user,timezone:"Europe/London"};',context);
assert.throws(()=>vm.runInContext('calendarDropProposal("move","2027-03-28","01:30")',context),/Такого времени нет/);
vm.runInContext('S.user=fixture.user;',context);
const teacherNavigation=vm.runInContext('renderNavigation(2)',context);
assert.equal((teacherNavigation.match(/class="nav-group"/g)||[]).length,3,'teacher navigation is grouped');
for(const route of vm.runInContext('allowedRoutes()',context)){
  assert.equal((teacherNavigation.match(new RegExp(`data-route="${route}"`,'g'))||[]).length,1,route+' appears once in navigation');
}
for(const route of vm.runInContext('allowedRoutes()',context)){
  const html=vm.runInContext(`S.route=${JSON.stringify(route)};renderPage()`,context);
  assert.ok(html.includes('<h1>'),route+' has a heading');
  assert.ok(!html.includes('<script>untrusted()'),route+' escapes student input');
  assert.ok(!html.includes('undefined'),route+' renders defined data');
}
vm.runInContext('S.user={...fixture.user,id:"student",name:"Student",role:"student"};',context);
assert.ok(!vm.runInContext('allowedRoutes()',context).includes('students'));
for(const route of vm.runInContext('allowedRoutes()',context)){
  assert.ok(vm.runInContext(`S.route=${JSON.stringify(route)};renderPage()`,context).includes('<h1>'),route);
}
// An empty studio must be useful rather than crash on missing first records.
vm.runInContext('for(const key of ["students","lessons","slots","payments","packages","ledger","homework","goals","progress","materials","messages","reports","notifications","templates","referrals","files"])S.data[key]=[];S.user=fixture.user;',context);
for(const route of vm.runInContext('allowedRoutes()',context)){
  assert.doesNotThrow(()=>vm.runInContext(`S.route=${JSON.stringify(route)};renderPage()`,context),route+' empty state');
}
console.log('Frontend: all teacher/student routes, escaping and empty states passed.');
await import('./calendar-drag-check.mjs');
await import('./interaction-check.mjs');
