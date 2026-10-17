PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('teacher','student')), password_hash TEXT,
 timezone TEXT NOT NULL DEFAULT 'Europe/Moscow', active INTEGER NOT NULL DEFAULT 1,
 telegram_chat TEXT, telegram_enabled INTEGER NOT NULL DEFAULT 0,
 email_enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students(
 user_id TEXT PRIMARY KEY REFERENCES users(id), level TEXT NOT NULL DEFAULT 'B1',
 focus TEXT NOT NULL DEFAULT '', private_notes TEXT NOT NULL DEFAULT '',
 referral_code TEXT UNIQUE NOT NULL, referrer_id TEXT REFERENCES users(id),
 referral_rewarded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens(
 token_hash TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id),
 kind TEXT NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packages(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, lessons INTEGER NOT NULL CHECK(lessons>0),
 amount INTEGER NOT NULL CHECK(amount>0), active INTEGER NOT NULL DEFAULT 1,
 sbp_url TEXT NOT NULL DEFAULT '', qr_file_id TEXT, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS lessons(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 starts_at TEXT NOT NULL, duration INTEGER NOT NULL CHECK(duration>0),
 status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','completed','cancelled','late_cancelled')),
 meeting_url TEXT NOT NULL DEFAULT '', zoom_id TEXT, note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lessons_time ON lessons(starts_at);
CREATE TABLE IF NOT EXISTS slots(id TEXT PRIMARY KEY, starts_at TEXT NOT NULL UNIQUE, duration INTEGER NOT NULL CHECK(duration>0));
CREATE TABLE IF NOT EXISTS payments(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), package_id TEXT REFERENCES packages(id),
 lessons INTEGER NOT NULL CHECK(lessons>0), amount INTEGER NOT NULL CHECK(amount>0),
 currency TEXT NOT NULL DEFAULT 'RUB', status TEXT NOT NULL DEFAULT 'pending',
 provider TEXT NOT NULL DEFAULT 'manual_sbp', provider_id TEXT UNIQUE,
 payment_url TEXT NOT NULL DEFAULT '', qr_svg TEXT NOT NULL DEFAULT '',
 request_key TEXT UNIQUE NOT NULL, proof_file_id TEXT, reference TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, paid_at TEXT, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ledger(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), delta INTEGER NOT NULL,
 reason TEXT NOT NULL, source TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
 instructions TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS homework(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'Writing', instructions TEXT NOT NULL, due_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted','reviewed')),
 answer TEXT NOT NULL DEFAULT '', feedback TEXT NOT NULL DEFAULT '', score INTEGER,
 submitted_at TEXT, reviewed_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), student_id TEXT REFERENCES users(id),
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, filename TEXT NOT NULL,
 mime TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials(
 id TEXT PRIMARY KEY, student_id TEXT REFERENCES users(id), title TEXT NOT NULL,
 body TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goals(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 target INTEGER NOT NULL CHECK(target>0), progress INTEGER NOT NULL DEFAULT 0,
 unit TEXT NOT NULL DEFAULT 'шагов', deadline TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_notes(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id),
 skill TEXT NOT NULL, score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
 note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), author_id TEXT NOT NULL REFERENCES users(id),
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports(
 id TEXT PRIMARY KEY, student_id TEXT NOT NULL REFERENCES users(id), month TEXT NOT NULL,
 body TEXT NOT NULL, published_at TEXT, created_at TEXT NOT NULL, UNIQUE(student_id, month)
);
CREATE TABLE IF NOT EXISTS notifications(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 body TEXT NOT NULL, route TEXT NOT NULL DEFAULT 'overview', event_key TEXT UNIQUE NOT NULL,
 created_at TEXT NOT NULL, read_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), channel TEXT NOT NULL,
 body TEXT NOT NULL, subject TEXT NOT NULL, event_key TEXT UNIQUE NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS integrations(user_id TEXT NOT NULL REFERENCES users(id), provider TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(user_id,provider));
CREATE TABLE IF NOT EXISTS calendar_events(user_id TEXT NOT NULL REFERENCES users(id), lesson_id TEXT NOT NULL REFERENCES lessons(id), event_id TEXT NOT NULL, PRIMARY KEY(user_id,lesson_id));
CREATE TABLE IF NOT EXISTS sync_state(provider TEXT NOT NULL, entity_id TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(provider,entity_id));
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS payment_reference ON payments(reference) WHERE reference<>'' AND status='paid';
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user_id);
CREATE INDEX IF NOT EXISTS lessons_student ON lessons(student_id,starts_at);
CREATE INDEX IF NOT EXISTS payments_student ON payments(student_id,created_at);
CREATE INDEX IF NOT EXISTS ledger_student ON ledger(student_id,created_at);
CREATE INDEX IF NOT EXISTS homework_student ON homework(student_id,due_at);
CREATE INDEX IF NOT EXISTS files_owner ON files(owner_id);
CREATE INDEX IF NOT EXISTS files_student ON files(student_id);
CREATE INDEX IF NOT EXISTS files_entity ON files(entity_type,entity_id);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id,created_at);
CREATE INDEX IF NOT EXISTS messages_student ON messages(student_id,created_at);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(state,next_attempt);
CREATE INDEX IF NOT EXISTS outbox_user ON outbox(user_id);
CREATE INDEX IF NOT EXISTS students_referrer ON students(referrer_id);
CREATE INDEX IF NOT EXISTS materials_student ON materials(student_id);
CREATE INDEX IF NOT EXISTS goals_student ON goals(student_id);
CREATE INDEX IF NOT EXISTS progress_student ON progress_notes(student_id);
CREATE INDEX IF NOT EXISTS calendar_events_lesson ON calendar_events(lesson_id);
CREATE INDEX IF NOT EXISTS payments_package ON payments(package_id);
