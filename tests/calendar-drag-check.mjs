import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

// Touch events are exercised outside the browser; no live schedule is used.
const handlers=new Map(),timers=new Map();let timerId=0;
const classes=()=>({add(){},remove(){}});
const node={innerHTML:'',textContent:'',open:false,classList:classes(),addEventListener(){},close(){this.open=false;},showModal(){this.open=true;}};
const originalDate=new Date(Date.now()+30*86400000);originalDate.setUTCHours(7,0,0,0);
const originalStart=originalDate.toISOString(),targetDay=new Date(+originalDate+86400000).toISOString().slice(0,10);
const target={dataset:{dropDate:targetDay},classList:classes()};
const hit={closest:s=>s==='[data-drop-date]'?target:null};
const document={
  querySelector:()=>node,querySelectorAll:()=>[],visibilityState:'visible',
  body:{classList:classes(),append(){}},
  createElement:()=>({classList:classes(),style:{},remove(){}}),
  elementFromPoint:()=>hit,
  addEventListener(type,fn){if(!handlers.has(type))handlers.set(type,[]);handlers.get(type).push(fn);},
};
const context=vm.createContext({document,window:{addEventListener(){},innerWidth:390,innerHeight:844,scrollBy(){}},
  Date,Intl,URL,location:{hash:'#calendar'},console,
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
  setInterval(){},clearInterval(){},requestAnimationFrame:()=>1,cancelAnimationFrame(){},
  fetch(){throw new Error('A drag must not call the API.');},
});
const source=await fs.readFile(new URL('../app.js',import.meta.url),'utf8');
vm.runInContext(source.replace(/boot\(\);\s*$/,''),context);
vm.runInContext(`S.user={id:'t',role:'teacher',timezone:'Europe/Moscow'};
  S.data={settings:{cancel_hours:24},students:[{id:'s',name:'Demo',active:1}],slots:[],lessons:[{id:'l',student_id:'s',title:'Demo',starts_at:${JSON.stringify(originalStart)},duration:50,status:'scheduled'}]};`,context);
const handle={dataset:{dragHandle:'l'},classList:classes(),setPointerCapture(){}};
const event=(x,y)=>({target:{closest:s=>s==='[data-drag-handle]'?handle:null},pointerType:'touch',pointerId:1,button:0,clientX:x,clientY:y,preventDefault(){}});
const emit=(type,e)=>handlers.get(type)?.forEach(fn=>fn(e));
const state=()=>vm.runInContext('calendarDrag.lessonId',context);
emit('pointerdown',event(320,350));
assert.equal(state(),'','touch waits for a deliberate hold');
assert.ok([...timers.values()].some(t=>t.ms===320));
emit('pointermove',event(320,380));
assert.equal(state(),'','moving before hold cancels');
assert.equal(timers.size,0);
emit('pointerdown',event(320,350));
[...timers.values()].find(t=>t.ms===320).fn();
assert.equal(state(),'l','long press activates dragging');
emit('pointercancel',{});
assert.equal(state(),'','interrupted touch cancels cleanly');
emit('pointerdown',event(320,350));
[...timers.values()].find(t=>t.ms===320).fn();
emit('pointermove',event(200,500));
emit('pointerup',event(200,500));
assert.equal(state(),'','drop clears gesture');
assert.equal(node.open,true,'drop asks for confirmation');
assert.ok(node.innerHTML.includes(targetDay+'T10:00'),'drop preserves account-local time');
assert.equal(vm.runInContext('S.data.lessons[0].starts_at',context),originalStart,'drop does not mutate lesson');
console.log('Calendar drag: touch hold, accidental swipe, cancel, confirmation and no-write checks passed.');
