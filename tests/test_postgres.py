"""Run the complete application suite against a disposable localhost Postgres.

Never accepts a hosted/production DSN. GitHub Actions provisions fluent_test.
"""
import os
import secrets
import unittest
from unittest.mock import patch
from test_app import StudioTest, Client, PASSWORD


@unittest.skipUnless(os.environ.get('FLUENT_TEST_POSTGRES_DSN'), 'Disposable Postgres not configured')
class PostgresStudioTest(StudioTest):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from backend.postgres import schema_sql
        cls.admin_url=os.environ['FLUENT_TEST_POSTGRES_DSN']
        info=conninfo_to_dict(cls.admin_url)
        if info.get('host') not in ('127.0.0.1','localhost') or info.get('dbname')!='fluent_test':
            raise RuntimeError('Postgres tests require disposable localhost/fluent_test')
        password=secrets.token_urlsafe(32)
        with psycopg.connect(cls.admin_url,autocommit=True) as admin:
            admin.execute(schema_sql())
            from psycopg import sql
            admin.execute(sql.SQL('ALTER ROLE fluent_runtime LOGIN PASSWORD {}').format(sql.Literal(password)))
        cls.database_url=make_conninfo(cls.admin_url,user='fluent_runtime',password=password)
        super().setUpClass()

    def setUp(self):
        self.empty_database()
        super().setUp()

    def empty_database(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.admin_url,autocommit=True) as admin:
            tables=admin.execute("SELECT tablename FROM pg_tables WHERE schemaname='fluent' AND tablename<>'schema_version'").fetchall()
            admin.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
                sql.SQL(',').join(sql.Identifier('fluent',r[0]) for r in tables)))

    def test_browser_setup_requires_secret_and_is_single_use(self):
        self.empty_database()
        from backend.core import initialize
        with patch.dict(os.environ,{'FLUENT_SETUP_TOKEN':'s'*48}):
            initialize(); client=Client()
            self.assertTrue(self.assert_ok(client.get('/api/auth/info'))['setup_enabled'])
            payload={'setup_token':'wrong','name':'Owner','email':'owner@example.test','password':PASSWORD}
            self.assertEqual(client.post('/api/auth/setup',payload)[0],403)
            payload['setup_token']='s'*48
            self.assertEqual(self.assert_ok(client.post('/api/auth/setup',payload))['user']['role'],'teacher')
            self.assertEqual(Client().post('/api/auth/setup',payload)[0],409)

    def test_runtime_cannot_read_public_or_change_schema(self):
        from backend.core import connect
        import psycopg
        with connect() as db:
            role=db.execute('SELECT current_user AS name').fetchone()['name']
            self.assertEqual(role,'fluent_runtime')
            self.assertFalse(db.execute("SELECT has_schema_privilege(current_user,'fluent','CREATE') AS allowed").fetchone()['allowed'])
        with connect() as db:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                db.execute('CREATE TABLE fluent.forbidden(id integer)')
            db.rollback()


# Do not let unittest discover the imported base a second time in this module.
del StudioTest
