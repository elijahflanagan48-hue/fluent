"""Postgres runtime adapter; local/demo installations keep using SQLite.

Schema creation is an administrative, one-off operation (`manage.py cloud-schema`).
The web process uses a separate least-privilege login, never the postgres owner.
"""
import os
import re
import sqlite3
from pathlib import Path


class Record(dict):
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_factory(cursor):
    names = [col.name for col in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values))


def placeholders(sql):
    # Only application-authored SQL is accepted. Preserve quoted question marks;
    # percent signs must be escaped for psycopg's parameter protocol.
    parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    return ''.join(part.replace('%', '%%') if i % 2 else part.replace('%', '%%').replace('?', '%s')
                   for i, part in enumerate(parts))


class Database:
    dialect = 'postgres'

    def __init__(self, url):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        info = conninfo_to_dict(url)
        local = info.get('host') in ('localhost', '127.0.0.1', '::1')
        host=info.get('host','')
        # Supabase uses its own CA, published by the project's Database Settings.
        # Keep hostname verification on for both direct and pooled connections.
        root=str(Path(__file__).parent/'certs/supabase-ca.crt') if host.endswith(('.supabase.co','.pooler.supabase.com')) else 'system'
        tls = {} if local else {'sslmode': 'verify-full', 'sslrootcert': os.environ.get('PGSSLROOTCERT', root)}
        self.raw = psycopg.connect(url, row_factory=record_factory, connect_timeout=15,
                                  prepare_threshold=None, **tls)
        # A fixed private schema cannot be selected by browser requests.
        self.raw.execute('SET search_path TO fluent, pg_catalog')
        self.raw.execute("SET statement_timeout = '30s'")
        self.raw.execute("SET lock_timeout = '15s'")
        self.raw.commit()

    def execute(self, sql, args=()):
        import psycopg
        try:
            if sql.strip().upper() == 'BEGIN IMMEDIATE':
                # Preserve SQLite's serialized write semantics for lesson booking,
                # balances, one-use tokens and initial-owner creation across workers.
                return self.raw.execute('SELECT pg_advisory_xact_lock(1937036646)')
            return self.raw.execute(placeholders(sql), args) if args else self.raw.execute(sql)
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError('Conflicting record') from exc

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()

    def __enter__(self):
        self.execute('BEGIN IMMEDIATE')
        return self

    def __exit__(self, kind, value, trace):
        try:
            self.rollback() if kind else self.commit()
        finally:
            self.close()


def schema_sql():
    """Deterministic admin SQL, with no credentials and no public Data API grants."""
    source = (Path(__file__).parent / 'schema.sql').read_text()
    source = source.replace('PRAGMA foreign_keys = ON;', '')
    source = source.replace('INSERT OR IGNORE INTO schema_version VALUES(1);',
                            'INSERT INTO schema_version VALUES(1) ON CONFLICT DO NOTHING;')
    source = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', source)
    for table in ('ledger', 'outbox'):
        source = source.replace('CREATE TABLE IF NOT EXISTS ' + table + '(\n',
                                'CREATE TABLE IF NOT EXISTS ' + table + '(\n rowid BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,\n')
    tables = re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', source)
    prefix = """BEGIN;
CREATE SCHEMA IF NOT EXISTS fluent;
REVOKE ALL ON SCHEMA fluent FROM PUBLIC;
SET LOCAL search_path TO fluent, pg_catalog;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fluent_runtime') THEN
  CREATE ROLE fluent_runtime NOLOGIN;
 END IF;
END $$;
"""
    suffix = '\nGRANT USAGE ON SCHEMA fluent TO fluent_runtime;\n'
    for table in tables:
        suffix += (f'ALTER TABLE fluent.{table} ENABLE ROW LEVEL SECURITY;\n'
                   f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='fluent' AND tablename='{table}' AND policyname='server_access') THEN "
                   f'CREATE POLICY server_access ON fluent.{table} TO fluent_runtime USING (true) WITH CHECK (true); END IF; END $$;\n'
                   f'REVOKE ALL ON fluent.{table} FROM PUBLIC;\n'
                   f'GRANT SELECT,INSERT,UPDATE,DELETE ON fluent.{table} TO fluent_runtime;\n')
    suffix += 'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA fluent TO fluent_runtime;\nCOMMIT;\n'
    suffix = suffix.replace('COMMIT;', """DO $$ DECLARE role_name text; BEGIN
 FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE format('REVOKE ALL ON SCHEMA fluent FROM %I',role_name);
   EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA fluent FROM %I',role_name);
  END IF;
 END LOOP;
END $$;
COMMIT;""")
    return prefix + source + suffix
