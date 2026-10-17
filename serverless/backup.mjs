import { DatabaseSync } from 'node:sqlite';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { zipSync } from 'fflate';
import { rows, now, Problem } from './core.mjs';
import * as storage from './storage.mjs';

// Portable SQLite archives remain compatible with manage.py restore.
export async function backup(db, includeFiles = false) {
  const folder=await mkdtemp(join(tmpdir(),'fluent-snapshot-')),filename=join(folder,'fluent.sqlite3');
  let copy;
  try {
    copy=new DatabaseSync(filename);
    copy.exec(await readFile(join(process.cwd(),'backend/schema.sql'),'utf8'));
    const tables=copy.prepare("SELECT name FROM sqlite_master WHERE type='table'").all();
    for (const {name} of tables) {
      if (['schema_version','sessions','tokens','integrations','calendar_events','sync_state'].includes(name)) continue;
      if (!/^[a-z_]+$/.test(name)) throw Error('Invalid backup table');
      const columns=copy.prepare(`PRAGMA table_info(${name})`).all().map(c=>c.name);
      const records=await rows(db,`SELECT ${columns.join(',')} FROM ${name}`),statement=copy.prepare(`INSERT INTO ${name} (${columns.join(',')}) VALUES (${columns.map(()=>'?').join(',')})`);
      for (const record of records) statement.run(...columns.map(c=>record[c]));
    }
    const files=copy.prepare('SELECT id FROM files').all(); copy.close(); copy=null;
    const entries={'fluent.sqlite3':new Uint8Array(await readFile(filename))};
    let size=entries['fluent.sqlite3'].length;
    if (includeFiles) for (const {id} of files) { const bytes=await storage.get(id); size+=bytes.length; if (size>128*1024**2) throw new Problem('Полная копия превышает 128 МБ. Вложения можно скачать отдельно.',413); entries['uploads/'+id]=new Uint8Array(bytes); }
    const archive=zipSync(entries,{level:6}),name='fluent-'+now().replaceAll(':','-')+'.zip';
    if (archive.length>128*1024**2) throw new Problem('Копия превышает 128 МБ.',413);
    await storage.put('backups/'+name,archive,'application/zip');
    // Retention removes only fluent backup files, never student attachments.
    await storage.retainBackups(); return name;
  } finally { if (copy) copy.close(); await rm(folder,{recursive:true,force:true}); }
}
