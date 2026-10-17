import { randomBytes, createHash, pbkdf2, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import pg from 'pg';
import { DateTime } from 'luxon';

export class Problem extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}
export const now = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
export const uid = () => randomBytes(16).toString('hex');
export const seconds = () => Date.now() / 1000;
export const digest = value => createHash('sha256').update(value).digest('hex');
export const equal = (a, b) => typeof a === 'string' && typeof b === 'string' && timingSafeEqual(Buffer.from(digest(a)), Buffer.from(digest(b)));
export const baseUrl = () => (process.env.FLUENT_BASE_URL || process.env.URL || 'http://127.0.0.1:4175').replace(/\/$/, '');
export function text(value, limit = 10000, required = false) {
  if (typeof value !== 'string' || [...value].length > limit) throw new Problem('Проверьте длину текстового поля.');
  value = value.trim();
  if (required && !value) throw new Problem('Заполните обязательные поля.');
  return value;
}
export function integer(value, low = 0, high = 100000000) {
  if (!['string', 'number'].includes(typeof value) || !/^-?\d+$/.test(String(value)) || String(Number(value)) !== String(value) || !Number.isSafeInteger(Number(value)) || Number(value) < low || Number(value) > high) throw new Problem('Некорректное число.');
  return Number(value);
}
export function email(value) {
  const result = text(value, 254, true).toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(result)) throw new Problem('Проверьте email.');
  return result;
}
export function safeUrl(value) {
  value = text(value, 2000);
  if (value) {
    try { const u = new URL(value); if (u.protocol !== 'https:' || !u.hostname || u.username || u.password) throw Error(); }
    catch { throw new Problem('Нужна ссылка, начинающаяся с https://.'); }
  }
  return value;
}
export function zone(value) {
  if (typeof value !== 'string' || !DateTime.now().setZone(value).isValid) throw new Problem('Неизвестный часовой пояс.');
  return value;
}
export function dt(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value)) throw new Problem('Укажите дату, время и часовой пояс.');
  const result = DateTime.fromISO(value, { setZone: true }).toUTC();
  if (!result.isValid) throw new Problem('Укажите дату, время и часовой пояс.');
  return result;
}
export const iso = value => dt(value).toISO({ suppressMilliseconds: true });
const pbkdf = promisify(pbkdf2);
export async function passwordHash(password) {
  if (typeof password !== 'string' || [...password].length < 12 || [...password].length > 200) throw new Problem('Пароль должен содержать от 12 до 200 символов.');
  const salt = randomBytes(16), hash = await pbkdf(password, salt, 600000, 32, 'sha256');
  return `pbkdf2_sha256$600000$${salt.toString('hex')}$${hash.toString('hex')}`;
}
export async function verifyPassword(password, encoded) {
  if (typeof password !== 'string' || [...password].length > 200) return false;
  const parts = typeof encoded === 'string' ? encoded.split('$') : [];
  if (parts.length !== 4 || parts[0] !== 'pbkdf2_sha256' || !/^\d+$/.test(parts[1]) || +parts[1] < 600000 || +parts[1] > 2000000 || !/^[a-f0-9]{32}$/.test(parts[2]) || !/^[a-f0-9]{64}$/.test(parts[3])) {
    // Same expensive operation for unknown accounts, reducing account enumeration.
    await pbkdf(password, Buffer.alloc(16), 600000, 32, 'sha256');
    return false;
  }
  const result = await pbkdf(password, Buffer.from(parts[2], 'hex'), +parts[1], 32, 'sha256');
  return timingSafeEqual(result, Buffer.from(parts[3], 'hex'));
}
export function placeholders(sql) {
  let i = 0;
  return sql.split(/('(?:''|[^'])*'|"(?:""|[^"])*")/).map((part, n) => n % 2 ? part : part.replace(/\?/g, () => '$' + (++i))).join('');
}
pg.types.setTypeParser(20, value => Number(value));
let pool;
export async function connect() {
  if (!pool) {
    const url = process.env.DATABASE_URL;
    if (!url) throw new Problem('Сервер пока не настроен.', 503);
    const parsed = new URL(url);
    const local = ['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname);
    // pg's connection string TLS parameters must not override certificate verification.
    for (const key of ['sslmode', 'sslcert', 'sslkey', 'sslrootcert']) parsed.searchParams.delete(key);
    const ca = readFileSync(join(process.cwd(), 'backend/certs/supabase-ca.crt'), 'utf8');
    pool = new pg.Pool({ connectionString: parsed.toString(), max: 2, idleTimeoutMillis: 5000,
      connectionTimeoutMillis: 15000, allowExitOnIdle: true,
      ssl: local ? false : { rejectUnauthorized: true, ca } });
    pool.on('error', () => {}); // Never log database URLs or provider error objects.
  }
  const client = await pool.connect();
  await client.query('SET search_path TO fluent, pg_catalog');
  await client.query("SET statement_timeout = '25s'");
  await client.query("SET lock_timeout = '12s'");
  return {
    async query(sql, args = []) { return (await client.query(placeholders(sql), args)).rows; },
    async begin(write = true) { await client.query('BEGIN'); if (write) await client.query('SELECT pg_advisory_xact_lock(1937036646)'); },
    async commit() { await client.query('COMMIT'); }, async rollback() { await client.query('ROLLBACK'); },
    close() { client.release(); }
  };
}
export const rows = (db, sql, args = []) => db.query(sql, args);
export async function one(db, sql, args = [], required = false) {
  const value = (await rows(db, sql, args))[0] || null;
  if (!value && required) throw new Problem('Запись не найдена.', 404);
  return value;
}
export async function scalar(db, sql, args = []) { return Object.values(await one(db, sql, args, true))[0]; }
export async function insert(db, table, values) {
  const keys = Object.keys(values);
  if (![table, ...keys].every(x => /^[a-z_]+$/.test(x))) throw Error('Invalid internal identifier');
  await rows(db, `INSERT INTO ${table} (${keys.join(',')}) VALUES (${keys.map(() => '?').join(',')})`, Object.values(values));
  return values;
}
export async function settings(db) { return Object.fromEntries((await rows(db, 'SELECT * FROM settings')).map(r => [r.key, JSON.parse(r.value)])); }
export function teacher(user) { if (user.role !== 'teacher') throw new Problem('Доступно только преподавателю.', 403); }
export function owns(user, id) { if (user.role !== 'teacher' && user.id !== id) throw new Problem('Нет доступа к данным другого ученика.', 403); }
export const studentExists = (db, id) => one(db, "SELECT id,name,email FROM users WHERE id=? AND role='student'", [id], true);
export const balance = (db, id) => scalar(db, 'SELECT COALESCE(SUM(delta),0) AS value FROM ledger WHERE student_id=?', [id]);
export const audit = (db, user, action, entity) => insert(db, 'audit', { id: uid(), actor_id: user?.id || null, action, entity_id: entity, created_at: now() });
export const publicUser = user => Object.fromEntries(['id', 'email', 'name', 'role', 'timezone', 'telegram_enabled', 'email_enabled'].map(k => [k, user[k]]));
export async function notify(db, id, title, body, route, eventKey) {
  const key = id + ':' + eventKey;
  if (await one(db, 'SELECT id FROM notifications WHERE event_key=?', [key])) return;
  await insert(db, 'notifications', { id: uid(), user_id: id, title, body, route, event_key: key, created_at: now() });
  const user = await one(db, 'SELECT * FROM users WHERE id=?', [id], true);
  for (const [channel, enabled] of [['telegram', user.telegram_enabled && user.telegram_chat], ['email', user.email_enabled]]) {
    if (enabled) await insert(db, 'outbox', { id: uid(), user_id: id, channel, subject: title, body, event_key: key + ':' + channel });
  }
}
export async function notifyTeacher(db, title, body, route, key) {
  for (const user of await rows(db, "SELECT id FROM users WHERE role='teacher' AND active=1")) await notify(db, user.id, title, body, route, key);
}
export async function makeToken(db, id, kind, duration = 86400) {
  const raw = randomBytes(32).toString('base64url');
  await insert(db, 'tokens', { token_hash: digest(raw), user_id: id, kind, expires_at: seconds() + duration, payload: '{}' });
  return raw;
}
export async function tokenRecord(db, raw, kind) {
  if (typeof raw !== 'string') throw new Problem('Недействительная ссылка.');
  const result = await one(db, 'SELECT * FROM tokens WHERE token_hash=? AND kind=? AND expires_at>?', [digest(raw), kind, seconds()]);
  if (!result) throw new Problem('Ссылка недействительна или устарела. Запросите новую.');
  return result;
}
export async function rateLimit(db, key, limit = 12, window = 900) {
  const result = await one(db, 'INSERT INTO rate_limits VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN rate_limits.expires_at<? THEN 1 ELSE rate_limits.count+1 END,expires_at=CASE WHEN rate_limits.expires_at<? THEN excluded.expires_at ELSE rate_limits.expires_at END RETURNING count', [key, 1, seconds() + window, seconds(), seconds()]);
  if (result.count > limit) throw new Problem('Слишком много попыток. Повторите позже.', 429);
}
