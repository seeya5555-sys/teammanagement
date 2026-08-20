"""Dock Daily Report contracts (web MVP).

These tests use the same temporary-DB pattern as the existing Flask tests and
avoid any external Dock Manager/SVMS service.
"""
import json
import os
import tempfile
import unittest

import app as appmod


class DockDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'dock-daily.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            uid = appmod.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active) VALUES(?,?,?,?,1)",
                ('dockdaily-test', appmod.generate_password_hash('test'), 'Dock Daily', 'admin'))
            vessel = appmod.execute(
                "INSERT INTO vessels(name, vsl_cd, imo) VALUES(?,?,?)",
                ('DOCK DAILY TEST', 'D001', 'IMO-TEST'))
            self.uid, self.vessel = uid, vessel
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=self.uid, username='dockdaily-test', display_name='Dock Daily',
                     role='admin', permanent=False)

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def test_project_seeds_fixed_sections_and_report_is_unique(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'})
        self.assertEqual(201, p.status_code)
        pid = p.get_json()['id']
        with appmod.app.app_context():
            rows = appmod.query('SELECT section_key FROM dock_daily_section_def WHERE project_id=? ORDER BY sort_order', (pid,))
            self.assertEqual(['shipyard', 'survey', 'vendor', 'remark'], [r['section_key'] for r in rows])
        one = self.client.post(f'/api/dock-daily/projects/{pid}/reports/generate', json={'report_date': '2026-08-20'})
        two = self.client.post(f'/api/dock-daily/projects/{pid}/reports/generate', json={'report_date': '2026-08-20'})
        self.assertEqual(one.get_json()['id'], two.get_json()['id'])
        self.assertEqual(1, len(self.client.get(f'/api/dock-daily/projects/{pid}/reports').get_json()))

    def test_page_uses_trmt_shell_and_single_project_modal(self):
        page = self.client.get('/dock-daily')
        self.assertEqual(200, page.status_code)
        html = page.get_data(as_text=True)
        self.assertIn('class="topnav"', html)
        self.assertIn('id="dd-project-modal"', html)
        self.assertIn('id="ddp-vessel"', html)
        self.assertIn('필수 입력은 선박과 프로젝트명 2개입니다.', html)
        self.assertNotIn('{% block body %}', html)

        with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'dock_daily.js'), encoding='utf-8') as f:
            script = f.read()
        self.assertNotIn("prompt('vessel_id", script)
        self.assertNotIn("confirm('이번 입거", script)
        self.assertIn("projectForm.onsubmit", script)

    def test_report_includes_ios_itinerary_and_legacy_direct_dates(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel,
            'title': 'Test DD',
            'berthing_date': '2026-08-20',
            'dock_in_date': '2026-08-21',
            'dock_out_date': '2026-09-01',
            'departure_date': '2026-09-02',
        }).get_json()
        report = self.client.post(
            f"/api/dock-daily/projects/{p['id']}/reports/generate",
            json={'report_date': '2026-08-20'},
        ).get_json()

        self.assertEqual({
            'berthing': '2026-08-20',
            'dry_dock_in': '2026-08-21',
            'dry_dock_out': '2026-09-01',
            'departure': '2026-09-02',
        }, report['itinerary'])
        self.assertEqual('2026-08-20', report['berthing_date'])
        self.assertEqual('2026-08-21', report['dock_in_date'])
        self.assertEqual('2026-09-01', report['dock_out_date'])
        self.assertEqual('2026-09-02', report['departure_date'])

    def test_revision_conflict_and_final_lock(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate", json={'report_date': '2026-08-20'}).get_json()
        body = {'revision': r['revision'], 'operations': [{'section_key': 'shipyard', 'block_type': 'paragraph', 'content': {'body': 'ok'}}]}
        updated = self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body)
        self.assertEqual(200, updated.status_code)
        self.assertEqual(409, self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body).status_code)
        final = updated.get_json()
        final['status'] = 'final'
        locked = self.client.put(f"/api/dock-daily/reports/{r['id']}", json={'revision': final['revision'], 'status': 'final'})
        self.assertEqual(200, locked.status_code)
        self.assertEqual(409, self.client.put(f"/api/dock-daily/reports/{r['id']}", json=body).status_code)

    def test_runner_idempotency_and_partial_fail_closed(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Test DD', 'auto_generate': True,
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'dock_manager_project_ids': ['v_DM17'],
        }).get_json()
        event = {'source_table': 'jobs', 'source_id': '1', 'source_subkey': 'job:1:remark:2026-08-20',
                 'date': '2026-08-20', 'source_updated_at': '2026-08-20T17:00:00+09:00',
                 'kind': 'job_remark', 'title': 'Job', 'body': 'Done', 'suggested_section': 'shipyard'}
        with appmod.app.app_context():
            key = 'dock-daily-test-key'
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", (key,))
        url = f"/api/ext/dock-daily/projects/{p['id']}/merge"
        bad = self.client.post(url, json={'report_date': '2026-08-20', 'events': [event], 'partial': True}, headers={'X-API-Key': key})
        self.assertEqual(409, bad.status_code)
        payload = {'report_date': '2026-08-20', 'events': [event], 'complete': True, 'partial': False}
        first = self.client.post(url, json=payload, headers={'X-API-Key': key})
        second = self.client.post(url, json=payload, headers={'X-API-Key': key})
        self.assertEqual(200, first.status_code); self.assertEqual(0, second.get_json()['applied'])
        removed = self.client.post(url, json={
            'report_date': '2026-08-20', 'events': [], 'complete': True, 'partial': False,
        }, headers={'X-API-Key': key})
        self.assertEqual(200, removed.status_code)
        self.assertEqual(1, removed.get_json()['missing'])
        with appmod.app.app_context():
            self.assertEqual(0, appmod.query('SELECT COUNT(*) n FROM dock_daily_block', one=True)['n'])
        projects = self.client.get('/api/ext/dock-daily/projects', headers={'X-API-Key': key})
        self.assertEqual(200, projects.status_code)
        self.assertEqual(['v_DM17'], projects.get_json()['projects'][0]['dock_manager_project_ids'])

    def test_only_special_sections_are_configurable(self):
        p = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'EGCS DD',
            'special_sections': [{'section_key': 'egcs', 'label': 'EGCS Retrofit', 'enabled': True}],
        }).get_json()
        changed = self.client.patch(f"/api/dock-daily/projects/{p['id']}", json={'sections': [
            {'section_key': 'shipyard', 'label': 'Changed', 'enabled': False},
            {'section_key': 'egcs', 'label': 'EGCS Special', 'enabled': False},
        ]}).get_json()
        sections = {section['section_key']: section for section in changed['sections']}
        self.assertEqual('Shipyard', sections['shipyard']['label'])
        self.assertEqual(1, sections['shipyard']['enabled'])
        self.assertEqual('EGCS Special', sections['egcs']['label'])
        self.assertEqual(0, sections['egcs']['enabled'])

    def test_auto_project_requires_valid_final_active_window(self):
        missing = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Invalid auto', 'auto_generate': True,
        })
        self.assertEqual(400, missing.status_code)

        project = self.client.post('/api/dock-daily/projects', json={
            'vessel_id': self.vessel, 'title': 'Valid auto',
            'active_from': '2026-08-01', 'active_to': '2026-08-31',
            'auto_generate': True,
        }).get_json()
        inverted = self.client.patch(f"/api/dock-daily/projects/{project['id']}", json={
            'active_from': '2026-09-01',
        })
        self.assertEqual(400, inverted.status_code)
        cleared = self.client.patch(f"/api/dock-daily/projects/{project['id']}", json={
            'active_to': None,
        })
        self.assertEqual(400, cleared.status_code)
        unchanged = self.client.get('/api/dock-daily/projects').get_json()[0]
        self.assertEqual('2026-08-01', unchanged['active_from'])
        self.assertEqual('2026-08-31', unchanged['active_to'])

    def test_svms_preview_reports_utf8_bytes_and_publish_disabled(self):
        p = self.client.post('/api/dock-daily/projects', json={'vessel_id': self.vessel, 'title': 'Test DD'}).get_json()
        r = self.client.post(f"/api/dock-daily/projects/{p['id']}/reports/generate", json={'report_date': '2026-08-20'}).get_json()
        self.client.put(f"/api/dock-daily/reports/{r['id']}", json={'revision': r['revision'], 'operations': [{'section_key': 'vendor', 'block_type': 'paragraph', 'content': {'body': '한글 😀'}}]})
        preview = self.client.get(f"/api/dock-daily/reports/{r['id']}/svms-preview").get_json()
        self.assertEqual(len(preview['fields']['RMK_VNDR'].encode('utf-8')), preview['byte_counts']['RMK_VNDR'])
        self.assertEqual(503, self.client.post(f"/api/dock-daily/reports/{r['id']}/svms-publish").status_code)


if __name__ == '__main__':
    unittest.main()
