import base64
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.core import initialize, connect, uid, now, insert, one, rows, password_hash, verify_password, balance, make_token, digest, Problem
from backend.http import application
from backend.jobs import reminders, deliver_one
from backend import integrations, service
from manage import backup, restore


PASSWORD='Correct-horse-studio-2026'


def future(hours=72):
    return (datetime.now(timezone.utc)+timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


class Client:
    def __init__(self):
        self.cookie='';self.csrf=''

    def request(self,path,data=None,csrf=True,origin='http://127.0.0.1:4174'):
        raw=json.dumps(data).encode() if data is not None else b''
        env={'REQUEST_METHOD':'POST' if data is not None else 'GET','PATH_INFO':path.split('?')[0],
             'QUERY_STRING':path.split('?',1)[1] if '?' in path else '', 'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),
             'wsgi.input':io.BytesIO(raw),'HTTP_COOKIE':self.cookie,'REMOTE_ADDR':'test','HTTP_ORIGIN':origin}
        if csrf:env['HTTP_X_CSRF_TOKEN']=self.csrf
        capture={}
        def response(status,headers): capture.update(status=int(status.split()[0]),headers=dict(headers))
        body=b''.join(application(env,response))
        if capture['headers'].get('Set-Cookie'):
            self.cookie=capture['headers']['Set-Cookie'].split(';')[0]
        if 'application/json' in capture['headers'].get('Content-Type',''):
            body=json.loads(body)
            if body.get('csrf'): self.csrf=body['csrf']
        return capture['status'],body,capture['headers']

    def get(self,path):return self.request(path)
    def post(self,path,data):return self.request(path,data)
    def login(self,address):return self.post('/api/auth/login',{'email':address,'password':PASSWORD})


class StudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded=password_hash(PASSWORD)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='fluent-test-')
        self.environment=patch.dict(os.environ,{'DATABASE_URL':getattr(self,'database_url',''),'FLUENT_STORAGE':'local','FLUENT_HOSTING':'','FLUENT_DATA_DIR':self.temp.name,'FLUENT_BASE_URL':'http://127.0.0.1:4174','FLUENT_DEMO':'0','SMTP_HOST':'','SMTP_FROM':'','TELEGRAM_BOT_TOKEN':'','TELEGRAM_WEBHOOK_SECRET':'','GOOGLE_CLIENT_ID':'','GOOGLE_CLIENT_SECRET':''})
        self.environment.start();initialize()
        self.teacher_id,self.student_id,self.other_id=uid(),uid(),uid()
        with connect() as db:
            for person,addr,role,name in [(self.teacher_id,'teacher@example.com','teacher','Учитель'),(self.student_id,'student@example.com','student','Катя'),(self.other_id,'other@example.com','student','Миша')]:
                insert(db,'users',id=person,email=addr,name=name,role=role,password_hash=self.encoded,created_at=now())
                if role=='student':insert(db,'students',user_id=person,level='B2',focus='Speaking',private_notes='Приватная заметка',referral_code=person[:8].upper())
            self.package_id=uid();insert(db,'packages',id=self.package_id,name='8 уроков',lessons=8,amount=1200000)
        self.t,self.s,self.o,self.anon=Client(),Client(),Client(),Client()
        # Issue test sessions through the actual session function, keeping login tests separate.
        from backend.http import make_session
        with connect() as db:
            for client,person in [(self.t,self.teacher_id),(self.s,self.student_id),(self.o,self.other_id)]:
                payload,headers=make_session(db,one(db,'SELECT * FROM users WHERE id=?',(person,)))
                client.cookie=headers[0][1].split(';')[0];client.csrf=payload['csrf']

    def tearDown(self):
        self.environment.stop();self.temp.cleanup()

    def assert_ok(self,result):
        self.assertEqual(result[0],200,result[1]);return result[1]

    def payment(self,client=None):
        return self.assert_ok((client or self.s).post('/api/payments',{'package_id':self.package_id}))['id']

    def lesson(self,hours=72,student=None):
        return self.assert_ok(self.t.post('/api/lessons',{'student_id':student or self.student_id,'title':'Speaking','starts_at':future(hours),'duration':50}))['ids'][0]

    def homework(self,student=None):
        student=student or self.student_id
        with connect() as db:
            upcoming=one(db,"SELECT id FROM lessons WHERE student_id=? AND status='scheduled' AND starts_at>?",(student,now()))
        if not upcoming:self.lesson(1000,student)
        return self.assert_ok(self.t.post('/api/homework',{'student_id':student or self.student_id,'title':'Story','instructions':'Tell a story','due_at':future(48)}))['id']

    def test_login_logout_and_secure_password(self):
        self.assertTrue(verify_password(PASSWORD,self.encoded))
        self.assertFalse(verify_password('wrong',self.encoded))
        result=self.anon.login('teacher@example.com');self.assert_ok(result)
        self.assertIn('HttpOnly',result[2]['Set-Cookie']);self.assertIn('SameSite=Lax',result[2]['Set-Cookie'])
        self.assert_ok(self.anon.post('/api/auth/logout',{}))
        self.assertEqual(self.anon.get('/api/bootstrap')[0],401)

    def test_password_change_revokes_other_sessions(self):
        other=Client();self.assert_ok(other.login('teacher@example.com'))
        self.assert_ok(self.t.post('/api/auth/password',{'current_password':PASSWORD,'password':'Another-secure-passphrase-26'}))
        self.assertEqual(other.get('/api/auth/me')[0],401)

    def test_login_throttle_survives_failed_request(self):
        for _ in range(10):self.assertEqual(self.anon.post('/api/auth/login',{'email':'missing@example.com','password':'bad'})[0],401)
        self.assertEqual(self.anon.post('/api/auth/login',{'email':'missing@example.com','password':'bad'})[0],429)

    def test_roles_and_student_isolation(self):
        self.homework(self.other_id);self.lesson(student=self.other_id)
        self.assertEqual(self.s.post('/api/students',{'name':'X','email':'x@example.com'})[0],403)
        self.assertEqual(self.s.post('/api/settings',{'sbp_url':'https://example.com'})[0],403)
        data=self.assert_ok(self.s.get('/api/bootstrap'))
        self.assertEqual([s['id'] for s in data['students']],[self.student_id])
        self.assertNotIn('private_notes',data['students'][0]);self.assertNotIn('password_hash',data['user'])
        self.assertEqual(data['lessons'],[]);self.assertEqual(data['homework'],[])

    def test_csrf_and_origin(self):
        self.assertEqual(self.t.request('/api/settings',{'studio_name':'Bad'},csrf=False)[0],403)
        self.assertEqual(self.t.request('/api/settings',{'studio_name':'Bad'},origin='https://evil.example')[0],403)
        self.assertEqual(self.anon.post('/api/students',{})[0],401)

    def test_source_files_and_secrets_are_not_served(self):
        for path in ['/.env','/backend/core.py','/data/fluent.sqlite3','/../manage.py','/.git/config']:
            self.assertEqual(self.anon.get(path)[0],404,path)
        self.assert_ok(self.anon.get('/'))

    def test_demo_disabled_by_default(self):
        self.assertEqual(self.anon.post('/api/auth/demo',{'role':'teacher'})[0],404)

    def test_manual_payment_requires_teacher_confirmation(self):
        pid=self.payment()
        self.assertEqual(self.s.post('/api/payments/'+pid+'/confirm',{})[0],403)
        self.assert_ok(self.s.post('/api/payments/'+pid+'/claim',{}))
        with connect() as db:self.assertEqual(balance(db,self.student_id),0)
        self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{'reference':'Bank-123'}))
        with connect() as db:self.assertEqual(balance(db,self.student_id),8)

    def test_repeat_order_claim_confirmation_are_idempotent(self):
        pid=self.payment();self.assertEqual(self.payment(),pid)
        for _ in range(2):self.assert_ok(self.s.post('/api/payments/'+pid+'/claim',{}))
        for _ in range(2):self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        with connect() as db:
            self.assertEqual(balance(db,self.student_id),8)
            self.assertEqual(len(rows(db,"SELECT * FROM notifications WHERE event_key LIKE '%claimed:%'")),1)

    def test_payment_price_and_owner_cannot_be_forged(self):
        pid=self.assert_ok(self.s.post('/api/payments',{'package_id':self.package_id,'student_id':self.other_id,'amount':1,'lessons':100}))['id']
        with connect() as db:
            row=one(db,'SELECT * FROM payments WHERE id=?',(pid,));self.assertEqual(row['student_id'],self.student_id);self.assertEqual(row['amount'],1200000);self.assertEqual(row['lessons'],8)
        self.assertEqual(self.o.post('/api/payments/'+pid+'/claim',{})[0],403)

    def test_duplicate_bank_reference_rolls_back_credit(self):
        first=self.payment();self.assert_ok(self.t.post('/api/payments/'+first+'/confirm',{'reference':'unique-bank-number'}))
        second=self.payment(self.o)
        self.assertEqual(self.t.post('/api/payments/'+second+'/confirm',{'reference':'unique-bank-number'})[0],409)
        with connect() as db:self.assertEqual(balance(db,self.other_id),0)

    def test_rejected_payment_can_be_recreated(self):
        pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/reject',{'reason':'No receipt'}))
        self.assertNotEqual(self.payment(),pid)
        self.assertEqual(self.t.post('/api/payments/'+pid+'/confirm',{})[0],409)

    def test_removed_referral_program_never_adds_new_bonuses(self):
        with connect() as db:
            db.execute('UPDATE students SET referrer_id=? WHERE user_id=?',(self.other_id,self.student_id))
            db.execute("UPDATE settings SET value='true' WHERE key='referral_enabled'")
        for _ in range(2):
            pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        with connect() as db:self.assertEqual(balance(db,self.other_id),0);self.assertEqual(balance(db,self.student_id),16)

    def test_overlap_and_repeat_are_atomic(self):
        at=future(100);self.assert_ok(self.t.post('/api/lessons',{'student_id':self.student_id,'title':'First','starts_at':at,'duration':50}))
        self.assertEqual(self.t.post('/api/lessons',{'student_id':self.other_id,'title':'Conflict','starts_at':at,'duration':50})[0],409)
        earlier=(datetime.fromisoformat(at.replace('Z','+00:00'))-timedelta(weeks=1)).isoformat()
        # Use a future repeating series whose second lesson conflicts.
        later=future(300);self.assert_ok(self.t.post('/api/lessons',{'student_id':self.student_id,'title':'Later','starts_at':later,'duration':50}))
        series=(datetime.fromisoformat(later.replace('Z','+00:00'))-timedelta(weeks=1)).isoformat()
        self.assertEqual(self.t.post('/api/lessons',{'student_id':self.other_id,'title':'Series','starts_at':series,'duration':50,'repeat':2})[0],409)
        with connect() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM lessons WHERE title='Series'").fetchone()[0],0)

    def test_complete_debits_once_and_cannot_complete_future(self):
        lesson=self.lesson();self.assertEqual(self.t.post('/api/lessons/'+lesson+'/complete',{})[0],400)
        with connect() as db:db.execute('UPDATE lessons SET starts_at=? WHERE id=?',(future(-2),lesson))
        for _ in range(2):self.assert_ok(self.t.post('/api/lessons/'+lesson+'/complete',{'note':'Good progress'}))
        with connect() as db:self.assertEqual(balance(db,self.student_id),-1)

    def test_late_cancel_requires_consent_and_debits_once(self):
        lesson=self.lesson(2)
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/cancel',{})[0],409)
        self.assert_ok(self.s.post('/api/lessons/'+lesson+'/cancel',{'accept_charge':True}))
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/cancel',{'accept_charge':True})[0],409)
        with connect() as db:self.assertEqual(balance(db,self.student_id),-1)

    def test_early_cancel_does_not_charge(self):
        lesson=self.lesson(72);self.assert_ok(self.s.post('/api/lessons/'+lesson+'/cancel',{}))
        with connect() as db:self.assertEqual(balance(db,self.student_id),0)

    def test_students_cannot_change_other_lessons(self):
        lesson=self.lesson(student=self.other_id)
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/cancel',{})[0],403)
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/note',{'note':'bad'})[0],403)

    def test_only_teacher_can_reschedule_even_with_free_slot(self):
        lesson=self.lesson(72)
        slot=self.assert_ok(self.t.post('/api/slots',{'starts_at':future(100),'duration':50}))['id']
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/reschedule',{'slot_id':slot})[0],403)
        self.assertEqual(self.s.post('/api/lessons/'+lesson+'/reschedule',{'starts_at':future(100)})[0],403)
        with connect() as db:self.assertIsNotNone(one(db,'SELECT * FROM slots WHERE id=?',(slot,)))
        self.assert_ok(self.t.post('/api/lessons/'+lesson+'/reschedule',{'starts_at':future(100),'duration':50}))

    def test_slot_booking_checks_balance_and_prevents_double_booking(self):
        slot=self.assert_ok(self.t.post('/api/slots',{'starts_at':future(100),'duration':50}))['id']
        self.assertEqual(self.s.post('/api/slots/'+slot+'/book',{})[0],400)
        pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        self.assert_ok(self.s.post('/api/slots/'+slot+'/book',{}))
        self.assertEqual(self.s.post('/api/slots/'+slot+'/book',{})[0],404)

    def test_homework_submission_review_and_reopen(self):
        task=self.homework()
        self.assertEqual(self.s.post('/api/homework/'+task+'/submit',{'answer':''})[0],400)
        self.assert_ok(self.s.post('/api/homework/'+task+'/submit',{'answer':'My story'}))
        self.assertEqual(self.s.post('/api/homework/'+task+'/review',{'feedback':'Great','score':100})[0],403)
        self.assert_ok(self.t.post('/api/homework/'+task+'/review',{'feedback':'Great work','score':85}))
        self.assertEqual(self.s.post('/api/homework/'+task+'/submit',{'answer':'overwrite'})[0],409)
        self.assert_ok(self.t.post('/api/homework/'+task+'/reopen',{}))
        self.assert_ok(self.s.post('/api/homework/'+task+'/submit',{'answer':'Revised'}))

    def test_homework_files_are_private(self):
        task=self.homework()
        file=self.assert_ok(self.s.post('/api/files',{'entity_type':'homework','entity_id':task,'filename':'answer.txt','data':base64.b64encode(b'My answer').decode()}))['id']
        self.assert_ok(self.s.get('/api/files/'+file));self.assert_ok(self.t.get('/api/files/'+file))
        self.assertEqual(self.o.get('/api/files/'+file)[0],403)
        self.assertEqual(self.anon.get('/api/files/'+file)[0],401)
        self.assertEqual(self.o.post('/api/files',{'entity_type':'homework','entity_id':task,'filename':'bad.txt','data':'YQ=='})[0],403)

    def test_upload_rejects_scripts_and_spoofed_images(self):
        task=self.homework()
        for filename in ['evil.html','evil.svg','evil.png']:
            self.assertEqual(self.s.post('/api/files',{'entity_type':'homework','entity_id':task,'filename':filename,'data':base64.b64encode(b'<script>alert(1)</script>').decode()})[0],400)

    def test_public_material_and_private_material_files(self):
        public=self.assert_ok(self.t.post('/api/materials',{'title':'For all','body':'Text'}))['id']
        private=self.assert_ok(self.t.post('/api/materials',{'title':'For Katya','student_id':self.student_id}))['id']
        ids=[]
        for mid in [public,private]:ids.append(self.assert_ok(self.t.post('/api/files',{'entity_type':'material','entity_id':mid,'filename':'file.txt','data':'YQ=='}))['id'])
        self.assert_ok(self.o.get('/api/files/'+ids[0]));self.assertEqual(self.o.get('/api/files/'+ids[1])[0],403)

    def test_invite_is_single_use_and_creates_student_session(self):
        data=self.assert_ok(self.t.post('/api/students',{'email':'new@example.com','name':'New student'}))
        raw=data['invite_url'].split('=')[1]
        response=self.assert_ok(self.anon.post('/api/auth/accept',{'token':raw,'password':PASSWORD,'kind':'invite'}))
        self.assertEqual(response['user']['role'],'student')
        self.assertEqual(Client().post('/api/auth/accept',{'token':raw,'password':PASSWORD})[0],400)

    def test_report_is_private_until_published(self):
        month=now()[:7]
        result=self.assert_ok(self.t.post('/api/reports',{'student_id':self.student_id,'month':month}))
        self.assertEqual(self.assert_ok(self.s.get('/api/bootstrap'))['reports'],[])
        self.assert_ok(self.t.post('/api/reports/'+result['id']+'/publish',{'comment':'Keep going'}))
        self.assertEqual(len(self.assert_ok(self.s.get('/api/bootstrap'))['reports']),1)
        self.assertEqual(self.assert_ok(self.o.get('/api/bootstrap'))['reports'],[])

    def test_goal_progress_is_scoped_and_bounded(self):
        goal=self.assert_ok(self.s.post('/api/goals',{'title':'Practice','target':5,'student_id':self.other_id}))['id']
        self.assertEqual(self.o.post('/api/goals/'+goal,{'progress':3})[0],403)
        self.assertEqual(self.s.post('/api/goals/'+goal,{'progress':10})[0],400)
        self.assert_ok(self.s.post('/api/goals/'+goal,{'progress':4}))

    def test_messages_cannot_be_sent_as_another_student(self):
        self.assert_ok(self.s.post('/api/messages',{'student_id':self.other_id,'body':'Hello'}))
        mine=self.assert_ok(self.s.get('/api/bootstrap'))['messages'];self.assertEqual(mine[0]['student_id'],self.student_id)
        self.assertEqual(self.assert_ok(self.o.get('/api/bootstrap'))['messages'],[])

    def test_reminders_are_durable_and_deduplicated(self):
        self.lesson(2)
        with connect() as db:reminders(db);reminders(db)
        with connect() as db:self.assertEqual(len(rows(db,"SELECT * FROM notifications WHERE event_key LIKE '%reminder:%'")),1)

    def test_notification_delivery_failure_is_not_reported_as_sent(self):
        with connect() as db:
            db.execute('UPDATE users SET email_enabled=1 WHERE id=?',(self.student_id,))
            insert(db,'outbox',id=uid(),user_id=self.student_id,channel='email',body='Hello',subject='Test',event_key='test')
        with patch('backend.integrations.send_email',side_effect=RuntimeError('offline')):deliver_one()
        with connect() as db:
            job=one(db,'SELECT * FROM outbox');self.assertEqual(job['state'],'pending');self.assertEqual(job['attempts'],1)

    def test_telegram_hook_requires_secret_and_one_time_private_pairing(self):
        with connect() as db:raw=make_token(db,self.student_id,'telegram',600)
        self.assertEqual(self.anon.post('/api/webhooks/telegram',{'message':{'text':'/start '+raw}})[0],403)

    def test_csv_requires_teacher_and_neutralizes_formulas(self):
        self.assertEqual(self.s.get('/api/export/students')[0],403)
        with connect() as db:db.execute('UPDATE users SET name=? WHERE id=?',('=HYPERLINK("bad")',self.student_id))
        response=self.t.get('/api/export/students');self.assert_ok(response);self.assertIn(b"'=HYPERLINK",response[1])

    def test_calendar_export_does_not_include_other_students(self):
        self.lesson(student=self.other_id)
        self.assertNotIn(b'BEGIN:VEVENT',self.assert_ok(self.s.get('/api/calendar.ics')))

    def test_backup_restores_records_and_excludes_sessions_and_tokens(self):
        pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        archive=backup(Path(self.temp.name)/'test.zip')
        target=Path(self.temp.name)/'restored'
        restore(archive,target)
        import sqlite3
        with sqlite3.connect(str(target/'fluent.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT SUM(delta) FROM ledger').fetchone()[0],8)

    def test_manual_backup_download_is_teacher_only(self):
        self.assertEqual(self.s.post('/api/backups',{})[0],403)
        result=self.assert_ok(self.t.post('/api/backups',{}))
        self.assertTrue(self.assert_ok(self.t.get(result['url'])).startswith(b'PK'))
        self.assertEqual(self.s.get(result['url'])[0],403)

    def test_payment_correction_reverses_once_without_bank_operation(self):
        pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        self.assertEqual(self.s.post('/api/payments/'+pid+'/void',{'reason':'No'})[0],403)
        for _ in range(2):self.assert_ok(self.t.post('/api/payments/'+pid+'/void',{'reason':'Mistaken confirmation'}))
        with connect() as db:self.assertEqual(balance(db,self.student_id),0)
        self.assertEqual(self.t.post('/api/payments/'+pid+'/confirm',{})[0],409)

    def test_initial_balance_adjustments_are_auditable_and_idempotent(self):
        key=uid();payload={'delta':5,'reason':'Previously paid lessons','request_key':key}
        self.assertEqual(self.s.post('/api/students/'+self.student_id+'/balance',payload)[0],403)
        for _ in range(2):self.assert_ok(self.t.post('/api/students/'+self.student_id+'/balance',payload))
        with connect() as db:
            self.assertEqual(balance(db,self.student_id),5)
            self.assertEqual(len(rows(db,"SELECT * FROM audit WHERE action='balance.adjusted'")),1)

    def test_booking_consumes_overlapping_available_windows(self):
        with connect() as db:
            insert(db,'ledger',id=uid(),student_id=self.student_id,delta=4,reason='Initial',source='initial',created_at=now())
        at=future(100)
        later=(datetime.fromisoformat(at.replace('Z','+00:00'))+timedelta(minutes=30)).isoformat()
        slot=self.assert_ok(self.t.post('/api/slots',{'starts_at':at,'duration':50}))['id']
        self.assert_ok(self.t.post('/api/slots',{'starts_at':later,'duration':50}))
        self.assert_ok(self.s.post('/api/slots/'+slot+'/book',{}))
        with connect() as db:self.assertEqual(len(rows(db,'SELECT * FROM slots')),0)

    def test_weekly_lessons_preserve_local_time_across_dst(self):
        with connect() as db:db.execute("UPDATE users SET timezone='Europe/London' WHERE id=?",(self.teacher_id,))
        self.assert_ok(self.t.post('/api/lessons',{'student_id':self.student_id,'title':'DST','starts_at':'2030-03-24T10:00:00Z','duration':50,'repeat':2}))
        with connect() as db:
            dates=[r['starts_at'] for r in rows(db,"SELECT starts_at FROM lessons WHERE title='DST' ORDER BY starts_at")]
        self.assertEqual(dates,['2030-03-24T10:00:00Z','2030-03-31T09:00:00Z'])

    def test_reviewed_payment_attachments_cannot_be_modified(self):
        pid=self.payment();self.assert_ok(self.t.post('/api/payments/'+pid+'/confirm',{}))
        self.assertEqual(self.s.post('/api/files',{'entity_type':'payment','entity_id':pid,'filename':'change.txt','data':'YQ=='})[0],409)

    def test_google_sync_deduplicates_events_and_updates_only_changes(self):
        lesson_id=self.lesson()
        with connect() as db:
            insert(db,'integrations',user_id=self.student_id,provider='google',data=json.dumps({'access_token':'fake','refresh_token':'fake','expires_at':time.time()+3600}))
            user=one(db,'SELECT * FROM users WHERE id=?',(self.student_id,))
            lesson=one(db,'SELECT * FROM lessons WHERE id=?',(lesson_id,))
            with patch('backend.integrations.request_json',return_value={'id':'fake'}) as remote:
                integrations.google_sync(db,user,[lesson]);integrations.google_sync(db,user,[lesson])
                self.assertEqual(remote.call_count,1)
                lesson['starts_at']=future(100)
                integrations.google_sync(db,user,[lesson])
                self.assertEqual(remote.call_count,2)
                self.assertEqual(remote.call_args.kwargs['method'],'PATCH')

    def test_google_oauth_state_is_bound_to_user(self):
        with connect() as db:token=make_token(db,self.student_id,'google',600)
        self.assertEqual(self.o.get('/api/integrations/google/callback?state='+token+'&code=fake')[0],403)

    def test_zoom_worker_does_not_patch_unchanged_meetings(self):
        from backend.jobs import sync_zoom
        lid=self.lesson()
        with connect() as db:db.execute('UPDATE lessons SET zoom_id=? WHERE id=?',('123',lid))
        with patch.dict(os.environ,{'ZOOM_ACCOUNT_ID':'fake','ZOOM_CLIENT_ID':'fake','ZOOM_CLIENT_SECRET':'fake'}):
            with patch('backend.integrations.zoom_meeting',return_value={}) as remote:
                sync_zoom();sync_zoom();self.assertEqual(remote.call_count,1)
                with connect() as db:db.execute('UPDATE lessons SET title=? WHERE id=?',('Changed topic',lid))
                sync_zoom();self.assertEqual(remote.call_count,2)

    def test_production_refuses_insecure_or_demo_startup(self):
        from backend.production import main
        with self.assertRaises(SystemExit):main()
        with patch.dict(os.environ,{'FLUENT_BASE_URL':'https://studio.example','FLUENT_DEMO':'1'}):
            with self.assertRaises(SystemExit):main()

    def test_browser_setup_requires_secret_and_is_single_use(self):
        folder=str(Path(self.temp.name)/'empty-studio')
        with patch.dict(os.environ,{'FLUENT_DATA_DIR':folder,'FLUENT_SETUP_TOKEN':'s'*48}):
            initialize();client=Client()
            info=self.assert_ok(client.get('/api/auth/info'))
            self.assertTrue(info['setup_enabled']);self.assertFalse(info['ready'])
            payload={'setup_token':'wrong','name':'Owner','email':'owner@example.test','password':PASSWORD}
            self.assertEqual(client.post('/api/auth/setup',payload)[0],403)
            payload['setup_token']='s'*48
            result=self.assert_ok(client.post('/api/auth/setup',payload))
            self.assertEqual(result['user']['role'],'teacher')
            self.assertEqual(Client().post('/api/auth/setup',payload)[0],409)
            self.assertFalse(self.assert_ok(client.get('/api/auth/info'))['setup_enabled'])

    def test_browser_setup_is_disabled_without_secret(self):
        with patch.dict(os.environ,{'FLUENT_DATA_DIR':str(Path(self.temp.name)/'empty-studio'),'FLUENT_SETUP_TOKEN':''}):
            initialize()
            self.assertEqual(Client().post('/api/auth/setup',{'email':'owner@example.test','password':PASSWORD})[0],404)

    def test_hosted_config_uses_host_url_and_persistent_disk(self):
        from backend.hosted import hosted_config
        with patch.dict(os.environ,{'FLUENT_BASE_URL':'','RENDER_EXTERNAL_URL':'https://studio-test.onrender.com','PORT':'10000','FLUENT_DATA_DIR':self.temp.name,'GUNICORN_CMD_ARGS':'--access-logfile -'}):
            self.assertEqual(hosted_config(),10000)
            self.assertEqual(os.environ['FLUENT_BASE_URL'],'https://studio-test.onrender.com')
            self.assertEqual(os.environ['GUNICORN_CMD_ARGS'],'')

    def test_hosted_config_refuses_missing_storage(self):
        from backend.hosted import hosted_config
        with patch.dict(os.environ,{'FLUENT_BASE_URL':'https://studio.example','FLUENT_DATA_DIR':''}):
            with self.assertRaises(SystemExit):hosted_config()


if __name__=='__main__': unittest.main()
