import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from backend import storage
from backend.core import Problem
from backend.postgres import placeholders, schema_sql, Record


class CloudConfigurationTest(unittest.TestCase):
    def test_postgres_parameters_preserve_literals_and_percent(self):
        self.assertEqual(placeholders("SELECT '?' AS literal, ? AS value, '100%' AS pct"),
                         "SELECT '?' AS literal, %s AS value, '100%%' AS pct")
        row=Record([('first',3),('second','ok')])
        self.assertEqual(row[0],3);self.assertEqual(row['second'],'ok')

    def test_schema_is_private_and_does_not_grant_browser_roles(self):
        sql=schema_sql()
        self.assertIn('REVOKE ALL ON SCHEMA fluent FROM PUBLIC',sql)
        self.assertIn('CREATE ROLE fluent_runtime NOLOGIN',sql)
        self.assertEqual(sql.count('ENABLE ROW LEVEL SECURITY'),26)
        self.assertNotIn('GRANT ALL',sql)
        self.assertNotIn('DROP TABLE',sql)
        self.assertIn('DOUBLE PRECISION',sql)

    def test_free_host_refuses_local_or_incomplete_storage(self):
        from backend.hosted import hosted_config
        env={'FLUENT_HOSTING':'free','FLUENT_BASE_URL':'https://studio.example','FLUENT_DEMO':'0',
             'DATABASE_URL':'','FLUENT_STORAGE':'local'}
        with patch.dict(os.environ,env):
            with self.assertRaises(SystemExit): hosted_config()

    def test_private_storage_rejects_wrong_origin_and_client_key(self):
        for origin,key in [('http://project.supabase.co','sb_secret_test'),
                           ('https://project.supabase.co.evil.example','sb_secret_test'),
                           ('https://project.supabase.co','sb_publishable_test')]:
            with patch.dict(os.environ,{'SUPABASE_URL':origin,'SUPABASE_SECRET_KEY':key}):
                with self.assertRaises(Problem): storage.config()

    def test_private_storage_does_not_follow_redirects_or_publish_keys(self):
        env={'FLUENT_STORAGE':'supabase','SUPABASE_URL':'https://project.supabase.co',
             'SUPABASE_SECRET_KEY':'sb_secret_test_not_a_real_key','SUPABASE_BUCKET':'fluent-private'}
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=b'file'
        with patch.dict(os.environ,env),patch('backend.storage.build_opener',return_value=opener) as build:
            self.assertEqual(storage.get('a'*32),b'file')
            request=opener.open.call_args.args[0]
            self.assertIn('/object/authenticated/fluent-private/',request.full_url)
            self.assertEqual(request.get_header('Apikey'),env['SUPABASE_SECRET_KEY'])
            self.assertIsInstance(build.call_args.args[0],storage.NoRedirect)
            self.assertIsNone(build.call_args.args[0].redirect_request(None,None,None,None,None,None))
            with self.assertRaises(ValueError):storage.get('../secret')

    def test_storage_error_does_not_expose_provider_response(self):
        env={'FLUENT_STORAGE':'supabase','SUPABASE_URL':'https://project.supabase.co',
             'SUPABASE_SECRET_KEY':'sb_secret_test_not_a_real_key','SUPABASE_BUCKET':'fluent-private'}
        opener=MagicMock()
        opener.open.side_effect=HTTPError('https://project.supabase.co',403,'private provider detail',{},None)
        with patch.dict(os.environ,env),patch('backend.storage.build_opener',return_value=opener):
            with self.assertRaises(Problem) as error:storage.get('a'*32)
            self.assertNotIn('private provider detail',str(error.exception))
            self.assertEqual(error.exception.status,503)


if __name__=='__main__':unittest.main()
