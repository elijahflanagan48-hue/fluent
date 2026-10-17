import { Problem } from './core.mjs';
export function decodeUpload(value) {
  if (typeof value !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) throw new Problem('Файл повреждён.');
  const raw = Buffer.from(value, 'base64');
  if (!raw.length || raw.length > 10 * 1024 ** 2) throw new Problem('Размер файла должен быть от 1 байта до 10 МБ.');
  return raw;
}
export function detectMime(raw, name) {
  const ext = name.split('.').pop().toLowerCase(), starts = bytes => raw.subarray(0, bytes.length).equals(Buffer.from(bytes));
  if (ext === 'png' && starts([137,80,78,71,13,10,26,10])) return 'image/png';
  if (['jpg','jpeg'].includes(ext) && starts([255,216,255])) return 'image/jpeg';
  if (ext === 'webp' && raw.toString('ascii',0,4) === 'RIFF' && raw.toString('ascii',8,12) === 'WEBP') return 'image/webp';
  if (ext === 'pdf' && raw.toString('ascii',0,5) === '%PDF-') return 'application/pdf';
  if (ext === 'webm' && starts([26,69,223,163])) return 'audio/webm';
  if (ext === 'ogg' && raw.toString('ascii',0,4) === 'OggS') return 'audio/ogg';
  if (ext === 'wav' && raw.toString('ascii',0,4) === 'RIFF' && raw.toString('ascii',8,12) === 'WAVE') return 'audio/wav';
  if (['m4a','mp4'].includes(ext) && raw.toString('ascii',4,8) === 'ftyp') return 'audio/mp4';
  if (ext === 'mp3' && (raw.toString('ascii',0,3) === 'ID3' || (raw[0] === 255 && [251,243,242].includes(raw[1])))) return 'audio/mpeg';
  if (['docx','pptx','xlsx'].includes(ext) && starts([80,75,3,4])) return 'application/octet-stream';
  if (ext === 'txt') { try { new TextDecoder('utf-8',{fatal:true}).decode(raw); return 'text/plain'; } catch {} }
  return null;
}
function config() {
  const url = process.env.SUPABASE_URL || '', bucket = process.env.SUPABASE_BUCKET || 'fluent-private', key = process.env.SUPABASE_SECRET_KEY;
  if (!/^https:\/\/[a-z0-9]+\.supabase\.co$/.test(url) || !/^[a-z0-9-]+$/.test(bucket) || !key) throw new Problem('Закрытое хранилище пока не настроено.',503);
  return { url, bucket, key };
}
export async function request(path, options = {}) {
  const { url, key } = config();
  let response;
  try { response = await fetch(url + '/storage/v1/' + path, { ...options, headers: { ...options.headers, apikey: key }, redirect: 'error', signal: AbortSignal.timeout(45000) }); }
  catch { throw new Problem('Хранилище временно недоступно. Повторите позже.',502); }
  if (!response.ok) throw new Problem(response.status === 404 ? 'Файл не найден.' : 'Хранилище временно недоступно. Повторите позже.',response.status === 404 ? 404 : 502);
  return response;
}
function objectPath(name, authenticated = false) {
  if (!/^(?:[a-f0-9]{32}|backups\/fluent-[0-9TZ-]+\.zip)$/.test(name)) throw new Problem('Файл не найден.',404);
  return 'object/' + (authenticated ? 'authenticated/' : '') + bucketFor(name) + '/' + name;
}
const bucketFor=name=>name.startsWith('backups/')?(process.env.SUPABASE_BACKUP_BUCKET||'fluent-backups'):config().bucket;
export const put = (name, data, mime = 'application/octet-stream') => request(objectPath(name), {method:'POST', headers:{'Content-Type':mime}, body:data});
export async function get(name) {
  const response = await request(objectPath(name,true));
  if (Number(response.headers.get('Content-Length') || 0) > 128 * 1024 ** 2) throw new Problem('Архив слишком большой.',413);
  const value = Buffer.from(await response.arrayBuffer());
  if (value.length > 128 * 1024 ** 2) throw new Problem('Архив слишком большой.',413);
  return value;
}
export async function retainBackups() {
  const bucket = bucketFor('backups/');
  const response = await request('object/list/' + bucket, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefix:'backups',limit:1000,sortBy:{column:'name',order:'desc'}})});
  const objects = await response.json(), extra = objects.filter(o => /^fluent-[0-9TZ-]+\.zip$/.test(o.name)).slice(7);
  if (extra.length) await request('object/' + bucket,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefixes:extra.map(o=>'backups/'+o.name)})});
}
function signedUrl(path) {
  const {url}=config(),result=new URL('/storage/v1'+path,url);
  if (result.origin!==url || !result.pathname.startsWith('/storage/v1/object/')) throw new Problem('Некорректный ответ хранилища.',502);
  return result.href;
}
export async function signedDownload(name,filename) {
  const response=await request('object/sign/'+bucketFor(name)+'/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({expiresIn:60})}),data=await response.json();
  const result=new URL(signedUrl(data.signedURL));
  // Images/audio should remain inline; archives/documents are downloads.
  if (filename && !/\.(png|jpe?g|webp|webm|ogg|wav|mp3|mp4|m4a)$/i.test(filename)) result.searchParams.set('download',filename);
  return result.href;
}
export async function signedUpload(name) {
  if (!/^[a-f0-9]{32}$/.test(name)) throw new Problem('Некорректный файл.');
  const response=await request('object/upload/sign/'+config().bucket+'/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),data=await response.json();
  return signedUrl(data.url);
}
export async function remove(name) {
  if (!/^[a-f0-9]{32}$/.test(name)) throw new Problem('Некорректный файл.');
  return request('object/'+config().bucket,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefixes:[name]})});
}
