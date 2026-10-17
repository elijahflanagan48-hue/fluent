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
