"""Durable incident, profile ownership and coding-call admission in VF SQLite."""
import hashlib
import json
import sqlite3
import time


def _alive(owner):
    try:
        pid = int(owner.split(':', 1)[0])
        if pid <= 0:
            return False
        from video_factory.integrations.capacity import process_is_alive
        return process_is_alive(pid)
    except PermissionError:
        return True
    except (ValueError, ProcessLookupError, OSError):
        return False


class RepairLedger:
    def __init__(self, connection):
        self.connection = connection
        connection.row_factory = sqlite3.Row
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS flow_ui_incident (
          incident_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
          operation_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, error_code TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'detected', details_json TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS flow_ui_lease (
          profile_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
          owner TEXT NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS flow_ui_attempt (
          incident_id TEXT NOT NULL, number INTEGER NOT NULL,
          authorization_id TEXT NOT NULL, status TEXT NOT NULL,
          receipt_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
          PRIMARY KEY(incident_id, number));
        ''')

    def incident(self, *, workflow_id, operation_id, profile_id, error_code, fingerprint):
        if not all((workflow_id, operation_id, profile_id, error_code, fingerprint)):
            raise ValueError('incident requires workflow/operation/profile/error/fingerprint')
        key = hashlib.sha256(json.dumps([workflow_id, operation_id, profile_id, fingerprint]).encode()).hexdigest()[:32]
        now = time.time()
        with self.connection:
            self.connection.execute('INSERT OR IGNORE INTO flow_ui_incident '
                '(incident_id,workflow_id,operation_id,profile_id,fingerprint,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (key, workflow_id, operation_id, profile_id, fingerprint, error_code, now, now))
        return self.get(key)

    def get(self, incident_id):
        row = self.connection.execute('SELECT * FROM flow_ui_incident WHERE incident_id=?', (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        result = dict(row)
        result['details'] = json.loads(result.pop('details_json'))
        result['attempts'] = []
        for attempt in self.connection.execute('SELECT * FROM flow_ui_attempt WHERE incident_id=? ORDER BY number', (incident_id,)):
            item = dict(attempt)
            item['receipt'] = json.loads(item.pop('receipt_json'))
            result['attempts'].append(item)
        return result

    def list_incidents(self):
        return [self.get(row[0]) for row in self.connection.execute('SELECT incident_id FROM flow_ui_incident ORDER BY created_at')]

    def update(self, incident_id, **fields):
        current = self.get(incident_id)
        status = fields.pop('status', current['status'])
        details = {**current['details'], **fields}
        with self.connection:
            self.connection.execute('UPDATE flow_ui_incident SET status=?,details_json=?,updated_at=? WHERE incident_id=?',
                (status, json.dumps(details), time.time(), incident_id))
        return self.get(incident_id)

    def acquire(self, incident_id, owner, seconds=600):
        if not owner or not 0 < seconds <= 600:
            raise ValueError('bounded owner lease required')
        incident = self.get(incident_id)
        now = time.time()
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            row = self.connection.execute('SELECT * FROM flow_ui_lease WHERE profile_id=?', (incident['profile_id'],)).fetchone()
            if row and (row['owner'] != owner or row['incident_id'] != incident_id):
                if row['expires_at'] > now or _alive(row['owner']):
                    return False
            # Existing provider ownership wins even when a stale lease has expired:
            # uncertain provider operations need reconciliation, not lock stealing.
            exists = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_factory_provider_capacity'").fetchone()
            if exists and self.connection.execute("SELECT 1 FROM video_factory_provider_capacity WHERE provider_kind='google_flow' LIMIT 1").fetchone():
                return False
            self.connection.execute('INSERT OR REPLACE INTO flow_ui_lease VALUES(?,?,?,?)',
                (incident['profile_id'], incident_id, owner, now + seconds))
        return True

    def renew(self, incident_id, owner, seconds=600):
        if not 0 < seconds <= 600:
            raise ValueError('bounded lease required')
        with self.connection:
            changed = self.connection.execute('UPDATE flow_ui_lease SET expires_at=? WHERE incident_id=? AND owner=? AND expires_at>?',
                (time.time() + seconds, incident_id, owner, time.time())).rowcount
        return changed == 1

    def release(self, incident_id, owner):
        with self.connection:
            self.connection.execute('DELETE FROM flow_ui_lease WHERE incident_id=? AND owner=?', (incident_id, owner))

    def reserve_attempt(self, incident_id, call_limit, *, authorization_id='', owner=''):
        if not authorization_id or type(call_limit) is not int or not 1 <= call_limit <= 2:
            raise ValueError('explicit maintenance authorization and call limit <=2 required')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            lock = self.connection.execute('SELECT * FROM flow_ui_lease WHERE incident_id=? AND owner=? AND expires_at>?',
                (incident_id, owner, time.time())).fetchone()
            if not lock:
                raise ValueError('live profile lease required')
            attempts = self.connection.execute('SELECT number,status FROM flow_ui_attempt WHERE incident_id=?', (incident_id,)).fetchall()
            if any(r['status'] in {'sent', 'uncertain'} for r in attempts):
                raise ValueError('coding request sent/uncertain; reconcile before another call')
            total = self.connection.execute('SELECT COUNT(*) FROM flow_ui_attempt WHERE authorization_id=?', (authorization_id,)).fetchone()[0]
            if total >= call_limit or len(attempts) >= 2:
                raise ValueError('maintenance call limit exhausted')
            number = len(attempts) + 1
            self.connection.execute('INSERT INTO flow_ui_attempt(incident_id,number,authorization_id,status,created_at) VALUES(?,?,?,?,?)',
                (incident_id, number, authorization_id, 'sent', time.time()))
        return number

    def finish_attempt(self, incident_id, number, status, receipt, proposal=None):
        if status not in {'response_received', 'invalid', 'uncertain'}:
            raise ValueError('invalid attempt status')
        with self.connection:
            self.connection.execute('UPDATE flow_ui_attempt SET status=?,receipt_json=? WHERE incident_id=? AND number=?',
                (status, json.dumps(receipt), incident_id, number))
            if proposal is not None:
                current = self.get(incident_id)
                details = {**current['details'], 'proposal': proposal}
                self.connection.execute('UPDATE flow_ui_incident SET details_json=?,updated_at=? WHERE incident_id=?',
                    (json.dumps(details), time.time(), incident_id))
