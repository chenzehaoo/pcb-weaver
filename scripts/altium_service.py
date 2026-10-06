"""Durable local Altium developer-beta jobs; never routes or releases a board."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid

from altium_beta import Beta


ROOT = Path(__file__).resolve().parents[1]
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


class Blocked(RuntimeError):
    pass


class AltiumService:
    def __init__(self, root=None, beta_factory=Beta, native_enabled=None):
        self.root = Path(root or os.environ.get('ALTIUM_SERVICE_ROOT', ROOT / 'data/altium-service')).resolve()
        self.beta_factory = beta_factory
        try:
            beta = beta_factory()
        except Exception:
            beta = None
        if beta is not None:
            self._assert_isolated(beta)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'jobs.sqlite3'
        self.native_enabled = (os.environ.get('ALTIUM_SERVICE_NATIVE_LAUNCH') == '1'
                               if native_enabled is None else native_enabled)
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, action TEXT NOT NULL,
                project_path TEXT NOT NULL, board_path TEXT, run_drc INTEGER NOT NULL,
                source_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                worker_id TEXT, lease_until REAL, result_json TEXT, error TEXT
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _fingerprint(inspection):
        payload = json.dumps(inspection, sort_keys=True, ensure_ascii=False).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def _assert_isolated(self, beta):
        for source_root in getattr(beta, 'roots', ()):
            source_root = Path(source_root).resolve()
            if self.root.is_relative_to(source_root) or source_root.is_relative_to(self.root):
                raise ValueError('Service database must be independent of source project roots')

    def _beta(self):
        beta = self.beta_factory()
        self._assert_isolated(beta)
        return beta

    @staticmethod
    def _job(row):
        data = dict(row)
        data['run_drc'] = bool(data['run_drc'])
        data['result'] = json.loads(data.pop('result_json')) if data['result_json'] else None
        return data

    def status(self):
        with self._db() as db:
            counts = {row['state']: row['total'] for row in db.execute(
                'SELECT state, COUNT(*) AS total FROM jobs GROUP BY state')}
        result = {'service': 'altium-local-developer-beta', 'transport': 'stdio',
                  'database': str(self.database), 'native_launch_enabled': self.native_enabled,
                  'automatic_routing': False, 'manufacturing_release': False,
                  'job_counts': counts}
        try:
            result['altium'] = self._beta().ready()
        except Exception as error:
            result['configuration_error'] = str(error)
        return result

    def submit(self, action, project_path, board_path=None, run_drc=False, request_id=None):
        if action not in ('inspect', 'check') or type(run_drc) is not bool:
            raise ValueError('Unsupported Altium job request')
        if action == 'inspect' and run_drc:
            raise ValueError('Static inspection cannot run native DRC')
        if request_id is not None and (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)):
            raise ValueError('Invalid request ID')
        inspection = self._beta().inspect(project_path, board_path)
        fingerprint = self._fingerprint(inspection)
        project = inspection['project']
        board = inspection['board']
        now = time.time()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                previous = db.execute('SELECT * FROM jobs WHERE request_id=?', (request_id,)).fetchone()
                if previous:
                    if (previous['action'], previous['project_path'], previous['board_path'],
                            bool(previous['run_drc']), previous['source_fingerprint']) != (
                            action, project, board, run_drc, fingerprint):
                        raise ValueError('Request ID was already used for a different input')
                    return self._job(previous)
            identifier = uuid.uuid4().hex
            db.execute('''INSERT INTO jobs
                (id, request_id, action, project_path, board_path, run_drc,
                 source_fingerprint, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)''',
                (identifier, request_id, action, project, board, int(run_drc), fingerprint, now, now))
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())

    def get_job(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError('Invalid job ID')
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown job ID')
        return self._job(row)

    def list_jobs(self, offset=0, limit=20):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid pagination')
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            rows = db.execute('''SELECT id, request_id, action, project_path, board_path,
                run_drc, state, created_at, updated_at, error FROM jobs
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
        return {'total': total, 'offset': offset, 'limit': limit,
                'items': [{**dict(row), 'run_drc': bool(row['run_drc'])} for row in rows]}

    def cancel(self, job_id):
        self.get_job(job_id)
        with self._db() as db:
            updated = db.execute("UPDATE jobs SET state='cancelled', updated_at=? "
                                 "WHERE id=? AND state='queued'", (time.time(), job_id)).rowcount
        if updated != 1:
            raise ValueError('Only a queued job can be cancelled')
        return self.get_job(job_id)

    def recover_expired(self):
        now = time.time()
        with self._db() as db:
            return db.execute("UPDATE jobs SET state='blocked', updated_at=?, "
                              "error='Worker lease expired; inspect native state before resubmitting', "
                              "worker_id=NULL, lease_until=NULL "
                              "WHERE state='running' AND lease_until<?", (now, now)).rowcount

    def _claim(self, worker_id):
        now = time.time()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM jobs WHERE state='queued' "
                             'ORDER BY created_at, id LIMIT 1').fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', worker_id=?, lease_until=?, updated_at=? "
                       'WHERE id=?', (worker_id, now + 60, now, row['id']))
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def _heartbeat(self, job_id, worker_id, stop):
        while not stop.wait(5):
            with self._db() as db:
                updated = db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND worker_id=? "
                                     "AND state='running'", (time.time() + 60, job_id, worker_id)).rowcount
            if not updated:
                return

    def _native_guard(self, beta):
        if not self.native_enabled:
            raise Blocked('Native launch is disabled; set ALTIUM_SERVICE_NATIVE_LAUNCH=1 explicitly')
        if os.name != 'nt':
            raise Blocked('Native Altium checks require Windows')
        if beta.lock.exists():
            raise Blocked('An earlier native Altium operation is busy or uncertain')
        check = subprocess.run(['powershell', '-NoProfile', '-Command',
                                'if (Get-Process X2 -ErrorAction SilentlyContinue) { exit 2 } else { exit 0 }'],
                               capture_output=True, timeout=15)
        if check.returncode == 2:
            raise Blocked('Altium is open; this beta cannot dispatch into its existing instance')
        if check.returncode != 0:
            raise Blocked('Could not verify Altium process state')

    def _execute(self, job):
        beta = self._beta()
        inspection = beta.inspect(job['project_path'], job['board_path'])
        if self._fingerprint(inspection) != job['source_fingerprint']:
            raise Blocked('Project or document changed after submission; submit a new job')
        if job['action'] == 'inspect':
            return {'inspection': inspection, 'native_executed': False}
        self._native_guard(beta)
        return {'native': beta.check(job['project_path'], job['board_path'], job['run_drc']),
                'native_executed': True}

    def run_once(self):
        self.recover_expired()
        worker_id = uuid.uuid4().hex
        job = self._claim(worker_id)
        if job is None:
            return None
        stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job['id'], worker_id, stop), daemon=True)
        heartbeat.start()
        try:
            result = self._execute(job)
            state, error = 'completed', None
        except Blocked as exc:
            result, state, error = None, 'blocked', str(exc)
        except Exception as exc:
            result, error = None, str(exc)
            state = 'blocked' if job['action'] == 'check' else 'failed'
        finally:
            stop.set()
            heartbeat.join(timeout=6)
        with self._db() as db:
            changed = db.execute('''UPDATE jobs SET state=?, result_json=?, error=?,
                updated_at=?, worker_id=NULL, lease_until=NULL
                WHERE id=? AND worker_id=? AND state='running' ''',
                (state, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 error, time.time(), job['id'], worker_id)).rowcount
        if changed != 1:
            raise RuntimeError('Job lease was lost; inspect persisted state')
        return self.get_job(job['id'])
