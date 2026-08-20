import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as appmod


class VesselManagerSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'admin'
            session['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def test_create_update_and_list_manager_supervisor(self):
        created = self.client.post('/api/vessels', json={
            'name': 'TEST VESSEL',
            'manager': 'CSM',
            'manager_supervisor': '홍길동',
            'supervisor_ids': [],
        })
        self.assertEqual(201, created.status_code)
        vid = created.get_json()['id']

        listed = self.client.get('/api/vessels/all').get_json()
        vessel = next(v for v in listed if v['id'] == vid)
        self.assertEqual('홍길동', vessel['manager_supervisor'])

        updated = self.client.put(f'/api/vessels/{vid}', json={
            'manager_supervisor': '  김관리  ',
        })
        self.assertEqual(200, updated.status_code)
        listed = self.client.get('/api/vessels/all').get_json()
        vessel = next(v for v in listed if v['id'] == vid)
        self.assertEqual('김관리', vessel['manager_supervisor'])

        preserved = self.client.put(f'/api/vessels/{vid}', json={'manager': 'NEW CSM'})
        self.assertEqual(200, preserved.status_code)
        listed = self.client.get('/api/vessels/all').get_json()
        vessel = next(v for v in listed if v['id'] == vid)
        self.assertEqual('김관리', vessel['manager_supervisor'])

    def test_edit_modal_exposes_manual_name_field(self):
        response = self.client.get('/')
        self.assertEqual(200, response.status_code)
        self.assertIn(b'vedit-manager-supervisor', response.data)

    def test_admin_vessel_list_shows_name_and_missing_fallback(self):
        source = (Path(__file__).resolve().parents[1] / 'static/js/app.js').read_text()
        self.assertIn(
            "관리사 감독: ${v.manager_supervisor || '없음'}",
            source,
        )

    def test_dashboard_issue_feed_omits_heavy_daily_fields(self):
        with appmod.app.app_context():
            sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('Perf Supervisor',))
            vid = appmod.execute("INSERT INTO vessels(name) VALUES(?)", ('PERF VESSEL',))
            appmod.execute(
                "INSERT INTO issues(supervisor_id,vessel_id,issue_date,item_topic,description,actions,priority,status) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, vid, '2026-08-20', 'PERF', 'x' * 20000,
                 '[{"progress":"' + ('y' * 20000) + '"}]', 'Urgent', 'Open'))

        light = self.client.get(f'/api/widget/issues?supervisor_id={sid}')
        full = self.client.get(f'/api/issues?supervisor_id={sid}')
        self.assertEqual(200, light.status_code)
        self.assertEqual(200, full.status_code)
        row = next(r for r in light.get_json() if r['item_topic'] == 'PERF')
        self.assertEqual({'id', 'vessel', 'priority', 'status', 'item_topic', 'due_date'}, set(row))
        self.assertLess(len(light.data), len(full.data) / 4)

        # Non-admin scope is server-owned; a query parameter must not expose a
        # different supervisor's feed.
        with appmod.app.app_context():
            own_sid = appmod.execute("INSERT INTO supervisors(name) VALUES(?)", ('Own Supervisor',))
        with self.client.session_transaction() as session:
            session['role'] = 'member'
            session['supervisor_id'] = own_sid
        scoped = self.client.get(f'/api/widget/issues?supervisor_id={sid}')
        self.assertEqual([], scoped.get_json())

    def test_auto_migrate_adds_non_null_column_to_legacy_vessels(self):
        legacy_db = os.path.join(self.tmp.name, 'legacy.db')
        with sqlite3.connect(legacy_db) as db:
            db.execute('CREATE TABLE vessels (id INTEGER PRIMARY KEY, name TEXT, manager TEXT)')
            db.execute("INSERT INTO vessels (name, manager) VALUES ('LEGACY', 'CSM')")

        current_db = appmod.DATABASE
        current_cfg = appmod.app.config['DATABASE']
        appmod.DATABASE = legacy_db
        appmod.app.config['DATABASE'] = legacy_db
        try:
            appmod._auto_migrate()
        finally:
            appmod.DATABASE = current_db
            appmod.app.config['DATABASE'] = current_cfg

        with sqlite3.connect(legacy_db) as db:
            row = db.execute(
                'SELECT manager_supervisor FROM vessels WHERE name=?', ('LEGACY',)
            ).fetchone()
        self.assertEqual('', row[0])


if __name__ == '__main__':
    unittest.main()
