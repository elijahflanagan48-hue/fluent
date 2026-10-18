import { Problem, one, rows, baseUrl, seconds, dt, digest } from './core.mjs';
import nodemailer from 'nodemailer';
export const configured = (...keys) => keys.every(key => Boolean(process.env[key]));
export async function requestJson(url, data = null, headers = {}, method = null, form = false) {
  let response;
  try {
    response = await fetch(url, { method: method || (data === null ? 'GET' : 'POST'), headers: { ...headers, ...(data === null ? {} : {'Content-Type':form ? 'application/x-www-form-urlencoded' : 'application/json'}) }, body: data === null ? undefined : form ? new URLSearchParams(data).toString() : JSON.stringify(data), redirect:'error',signal:AbortSignal.timeout(15000) });
    if (!response.ok) throw Error();
    const body = await response.text();
    if (body.length > 3*1024**2) throw Error();
    return body ? JSON.parse(body) : {};
  } catch { throw new Problem('Внешний сервис временно недоступен. Повторите позже.',502); }
}
export async function status(db,user,googleConnected) {
  return {zoom:configured('ZOOM_ACCOUNT_ID','ZOOM_CLIENT_ID','ZOOM_CLIENT_SECRET'),google:configured('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET'),google_connected:googleConnected??Boolean(await one(db,"SELECT user_id FROM integrations WHERE user_id=? AND provider='google'",[user.id])),telegram:configured('TELEGRAM_BOT_TOKEN','TELEGRAM_BOT_USERNAME','TELEGRAM_WEBHOOK_SECRET'),telegram_connected:Boolean(user.telegram_chat),email:configured('SMTP_HOST','SMTP_FROM')};
}
export async function zoomMeeting(lesson,cancel=false) {
  if (!configured('ZOOM_ACCOUNT_ID','ZOOM_CLIENT_ID','ZOOM_CLIENT_SECRET')) throw new Problem('Zoom ещё не подключён. Можно вставить готовую ссылку на встречу.',503);
  const auth=Buffer.from(process.env.ZOOM_CLIENT_ID+':'+process.env.ZOOM_CLIENT_SECRET).toString('base64');
  const token=await requestJson('https://zoom.us/oauth/token',{grant_type:'account_credentials',account_id:process.env.ZOOM_ACCOUNT_ID},{Authorization:'Basic '+auth},null,true),headers={Authorization:'Bearer '+token.access_token};
  if (cancel) { if (lesson.zoom_id) await requestJson('https://api.zoom.us/v2/meetings/'+encodeURIComponent(lesson.zoom_id),null,headers,'DELETE'); return {}; }
  const body={topic:lesson.title,type:2,start_time:lesson.starts_at,duration:lesson.duration,timezone:'UTC',settings:{waiting_room:true,join_before_host:false}};
  if (lesson.zoom_id) { await requestJson('https://api.zoom.us/v2/meetings/'+encodeURIComponent(lesson.zoom_id),body,headers,'PATCH'); return {id:lesson.zoom_id,join_url:lesson.meeting_url}; }
  return requestJson('https://api.zoom.us/v2/users/'+encodeURIComponent(process.env.ZOOM_USER_ID||'me')+'/meetings',body,headers);
}
export function googleAuthUrl(state) {
  if (!configured('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET')) throw new Problem('Google Calendar ещё не настроен на сервере.',503);
  return 'https://accounts.google.com/o/oauth2/v2/auth?'+new URLSearchParams({client_id:process.env.GOOGLE_CLIENT_ID,redirect_uri:baseUrl()+'/api/integrations/google/callback',response_type:'code',scope:'https://www.googleapis.com/auth/calendar.events.owned',access_type:'offline',prompt:'consent',state});
}
export const googleExchange=code=>requestJson('https://oauth2.googleapis.com/token',{code,client_id:process.env.GOOGLE_CLIENT_ID,client_secret:process.env.GOOGLE_CLIENT_SECRET,redirect_uri:baseUrl()+'/api/integrations/google/callback',grant_type:'authorization_code'},{},null,true);
export async function googleSync(db,user,lessons) {
  const record=await one(db,"SELECT data FROM integrations WHERE user_id=? AND provider='google'",[user.id],true),token=JSON.parse(record.data);
  if ((token.expires_at||0)<seconds()+90) {
    Object.assign(token,await requestJson('https://oauth2.googleapis.com/token',{client_id:process.env.GOOGLE_CLIENT_ID,client_secret:process.env.GOOGLE_CLIENT_SECRET,refresh_token:token.refresh_token,grant_type:'refresh_token'},{},null,true));
    token.expires_at=seconds()+token.expires_in;
    await rows(db,"UPDATE integrations SET data=? WHERE user_id=? AND provider='google'",[JSON.stringify(token),user.id]);
  }
  const headers={Authorization:'Bearer '+token.access_token},endpoint='https://www.googleapis.com/calendar/v3/calendars/primary/events';
  for (const lesson of lessons) {
    const event=await one(db,'SELECT * FROM calendar_events WHERE user_id=? AND lesson_id=?',[user.id,lesson.id]),id='f'+digest(user.id+lesson.id),cancelled=['cancelled','late_cancelled'].includes(lesson.status);
    if (cancelled && !event) continue;
    const body={summary:lesson.title,description:'fluent. '+lesson.meeting_url,start:{dateTime:lesson.starts_at},end:{dateTime:dt(lesson.starts_at).plus({minutes:lesson.duration}).toISO()},status:cancelled?'cancelled':'confirmed'},provider='google:'+user.id,fingerprint=digest(JSON.stringify(body));
    if ((await one(db,'SELECT fingerprint FROM sync_state WHERE provider=? AND entity_id=?',[provider,lesson.id]))?.fingerprint===fingerprint) continue;
    if (event) await requestJson(endpoint+'/'+id,body,headers,'PATCH');
    else {
      try { await requestJson(endpoint,{...body,id},headers); } catch { await requestJson(endpoint+'/'+id,body,headers,'PUT'); }
      await rows(db,'INSERT INTO calendar_events VALUES (?,?,?) ON CONFLICT(user_id,lesson_id) DO NOTHING',[user.id,lesson.id,id]);
    }
    await rows(db,'INSERT INTO sync_state VALUES (?,?,?) ON CONFLICT(provider,entity_id) DO UPDATE SET fingerprint=excluded.fingerprint',[provider,lesson.id,fingerprint]);
  }
}
export async function sendTelegram(chat,text) {
  if (!configured('TELEGRAM_BOT_TOKEN')) throw new Problem('Telegram не настроен.',503);
  const result=await requestJson('https://api.telegram.org/bot'+process.env.TELEGRAM_BOT_TOKEN+'/sendMessage',{chat_id:chat,text,disable_web_page_preview:true});
  if (!result.ok) throw new Problem('Telegram не принял сообщение.',502);
}
export async function sendEmail(address,subject,body) {
  if (!configured('SMTP_HOST','SMTP_FROM')) throw new Problem('Почта ещё не подключена.',503);
  const port=Number(process.env.SMTP_PORT||587);
  const transport=nodemailer.createTransport({host:process.env.SMTP_HOST,port,secure:port===465,requireTLS:port!==465,connectionTimeout:10000,socketTimeout:15000,...(process.env.SMTP_USER?{auth:{user:process.env.SMTP_USER,pass:process.env.SMTP_PASSWORD||''}}:{})});
  try { await transport.sendMail({from:process.env.SMTP_FROM,to:address,subject,text:body}); }
  catch { throw new Problem('Не удалось отправить письмо.',502); }
  finally { transport.close(); }
}
