import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const chat={innerHTML:'',dataset:{},scrollTop:0,scrollHeight:100,clientHeight:100};
const drawer={classList:{toggle(_name,value){drawer.open=value;}},dataset:{userId:'teacher'}};
const draft={value:'A new draft'};
const modal={open:false,addEventListener(){}};
const document={
  querySelector(s){return s==='.chat-body'?chat:s==='#sidebar'?drawer:s==='#modal'?modal:s==='[name=body]'?draft:null;},
  querySelectorAll(){return [];},addEventListener(){},visibilityState:'visible',
  body:{classList:{toggle(){}}}
};
const context=vm.createContext({document,window:{addEventListener(){}},Date,Intl,URL,
  location:{hash:'#messages'},console,crypto:webcrypto,Uint8Array,
  setInterval(){},setTimeout(){},clearInterval(){}});
const source=await fs.readFile(new URL('../app.js',import.meta.url),'utf8');
vm.runInContext(source.replace(/boot\(\);\s*$/,''),context);
vm.runInContext(`S.user={id:'teacher',role:'teacher',timezone:'Europe/Moscow'};
  S.route='messages';S.conversation='student';
  S.data={messages:[],notifications:[],notification_counts:{messages:3},homework:[]};
  var request,resolveRequest,rejectRequest;
  api=async(path,payload)=>{request={path,payload};return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});};`,context);
const send=vm.runInContext("sendChatMessage('<safe text>','student')",context);
assert.ok(chat.innerHTML.includes('&lt;safe text&gt;'));
assert.ok(chat.innerHTML.includes('Отправляется'));
assert.equal(draft.value,'A new draft','sending preserves the next draft');
assert.equal(vm.runInContext("sectionCount('messages')",context),3);
const id=vm.runInContext('request.payload.client_id',context);
context.confirmed={id,student_id:'student',author_id:'teacher',body:'<safe text>',created_at:new Date().toISOString()};
vm.runInContext('resolveRequest({id:confirmed.id,message:confirmed})',context);await send;
assert.equal(vm.runInContext('S.pendingMessages.length',context),0);
assert.equal(vm.runInContext('S.data.messages.length',context),1);
assert.ok(!chat.innerHTML.includes('Отправляется'));
const failed=vm.runInContext("sendChatMessage('Retry me','student')",context);
const retryId=vm.runInContext('request.payload.client_id',context);
vm.runInContext("rejectRequest(new Error('offline'))",context);await failed;
assert.ok(chat.innerHTML.includes('Не отправлено'));assert.ok(chat.innerHTML.includes('Повторить отправку'));
context.retryId=retryId;
const retry=vm.runInContext("sendChatMessage('Retry me','student',retryId)",context);
assert.equal(vm.runInContext('request.payload.client_id',context),retryId);
context.confirmed={id:retryId,student_id:'student',author_id:'teacher',body:'Retry me',created_at:new Date().toISOString()};
vm.runInContext('resolveRequest({id:confirmed.id,message:confirmed})',context);await retry;
assert.equal(vm.runInContext('S.data.messages.length',context),2);
vm.runInContext('setMenuOpen(true)',context);assert.equal(drawer.open,true);
vm.runInContext('updateNavBadges()',context);assert.equal(drawer.open,true,'notification updates do not close drawer');
console.log('Interactions: optimistic messages, escaping, drafts, safe retries, counts and drawer persistence passed.');
