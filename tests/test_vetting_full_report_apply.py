import io
import json
import os
import tempfile
import unittest

import app as appmod
import ai_gemini as routes


class VettingFullReportApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        self.old_gemini = routes._gemini_call_json
        db = os.path.join(self.tmp.name, 'full-report.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            appmod._auto_migrate()
            vessel = appmod.execute(
                "INSERT INTO vessels(name,short_name,active) VALUES(?,?,1)",
                ('GHANA PROSPERITY', 'GHANA'),
            )
            self.vetting = appmod.execute(
                "INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)",
                (vessel, 'LVKX-0383-3966-7845'),
            )
            self.f1 = appmod.execute(
                "INSERT INTO vt_findings(vetting_id,no,item,description,user_remark,status) "
                "VALUES(?,?,?,?,?,?)",
                (self.vetting, 1, '(Process)Not as expected', 'Condition of Class outstanding',
                 '수동 메모 보존', 'Open'),
            )
            self.f2 = appmod.execute(
                "INSERT INTO vt_findings(vetting_id,no,item,description,user_remark,status) "
                "VALUES(?,?,?,?,?,?)",
                (self.vetting, 2, '(Human)Junior Deck Officer', 'Liferaft card omitted',
                 '', 'Open'),
            )
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as sess:
            sess.update(user_id=1, username='admin', role='admin', supervisor_id=None)
        self.csrf = self.client.get('/api/csrf-token').get_json()['token']

    def tearDown(self):
        routes._gemini_call_json = self.old_gemini
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def _result(self, *, report='LVKX-0383-3966-7845', items=None, report_type='Full'):
        return {
            'report_type': report_type,
            'report_number': report,
            'items': items if items is not None else [
                {'finding_id': self.f1, 'matched': True, 'status': 'Open',
                 'remark': 'COC 종결을 위한 UT 및 MPI가 예정되어 있어 모니터링 중임.',
                 'evidence': 'pending re-examination by Class'},
                {'finding_id': self.f2, 'matched': True, 'status': 'Closed',
                 'remark': 'Liferaft identification card를 즉시 갱신하고 Master가 확인함.',
                 'evidence': 'card was immediately updated'},
            ],
        }

    def _post(self):
        return self.client.post(
            f'/api/vettings/{self.vetting}/apply-full-report',
            data={'file': (io.BytesIO(b'%PDF-1.4\nfull\n%%EOF'), 'full.pdf')},
            content_type='multipart/form-data',
            headers={'X-CSRF-Token': self.csrf},
        )

    def test_applies_all_findings_and_preserves_manual_remark_idempotently(self):
        routes._gemini_call_json = lambda *args, **kwargs: self._result()
        first = self._post()
        self.assertEqual(200, first.status_code, first.get_data(as_text=True))
        self.assertEqual({'updated': 2, 'open': 1, 'closed': 1}, {
            k: first.get_json()[k] for k in ('updated', 'open', 'closed')})
        second = self._post()
        self.assertEqual(200, second.status_code, second.get_data(as_text=True))
        with appmod.app.app_context():
            rows = appmod.query(
                'SELECT id,status,user_remark FROM vt_findings WHERE vetting_id=? ORDER BY no',
                (self.vetting,),
            )
            audits = appmod.query(
                'SELECT * FROM vt_full_report_audit WHERE vetting_id=? ORDER BY id',
                (self.vetting,),
            )
        self.assertEqual(['Open', 'Closed'], [r['status'] for r in rows])
        self.assertIn('수동 메모 보존', rows[0]['user_remark'])
        self.assertEqual(1, rows[0]['user_remark'].count(routes._FULL_REPORT_MARKER))
        self.assertIn('즉시 갱신', rows[1]['user_remark'])
        self.assertEqual(2, len(audits))
        self.assertEqual(2, len(json.loads(audits[-1]['before_json'])))
        self.assertEqual(2, len(json.loads(audits[-1]['after_json'])))
        self.assertEqual('admin', audits[-1]['applied_by'])

    def test_report_mismatch_changes_nothing(self):
        routes._gemini_call_json = lambda *args, **kwargs: self._result(report='OTHER-REPORT')
        response = self._post()
        self.assertEqual(422, response.status_code)
        self.assertEqual('REPORT_MISMATCH', response.get_json()['reason'])
        with appmod.app.app_context():
            rows = appmod.query(
                'SELECT status,user_remark FROM vt_findings WHERE vetting_id=? ORDER BY no',
                (self.vetting,),
            )
        self.assertEqual([('Open', '수동 메모 보존'), ('Open', '')],
                         [(r['status'], r['user_remark']) for r in rows])

    def test_partial_match_is_atomic_and_not_applied(self):
        routes._gemini_call_json = lambda *args, **kwargs: self._result(items=[
            {'finding_id': self.f1, 'matched': True, 'status': 'Closed',
             'remark': '완료됨.', 'evidence': 'completed'},
        ])
        response = self._post()
        self.assertEqual(422, response.status_code)
        self.assertEqual('INCOMPLETE_MATCH', response.get_json()['reason'])
        with appmod.app.app_context():
            rows = appmod.query(
                'SELECT status FROM vt_findings WHERE vetting_id=? ORDER BY no',
                (self.vetting,),
            )
        self.assertEqual(['Open', 'Open'], [r['status'] for r in rows])

    def test_rejects_non_full_report_and_non_pdf(self):
        routes._gemini_call_json = lambda *args, **kwargs: self._result(report_type='Initial')
        response = self._post()
        self.assertEqual('NOT_FULL_REPORT', response.get_json()['reason'])
        bad = self.client.post(
            f'/api/vettings/{self.vetting}/apply-full-report',
            data={'file': (io.BytesIO(b'doc'), 'full.docx')},
            content_type='multipart/form-data',
            headers={'X-CSRF-Token': self.csrf},
        )
        self.assertEqual(422, bad.status_code)
        self.assertEqual('BAD_TYPE', bad.get_json()['reason'])

    def test_rejects_empty_bad_magic_and_duplicate_match(self):
        empty = self.client.post(
            f'/api/vettings/{self.vetting}/apply-full-report',
            data={'file': (io.BytesIO(b''), 'full.pdf')},
            content_type='multipart/form-data', headers={'X-CSRF-Token': self.csrf})
        self.assertEqual('EMPTY_FILE', empty.get_json()['reason'])
        bad = self.client.post(
            f'/api/vettings/{self.vetting}/apply-full-report',
            data={'file': (io.BytesIO(b'not pdf'), 'full.pdf')},
            content_type='multipart/form-data', headers={'X-CSRF-Token': self.csrf})
        self.assertEqual('BAD_PDF', bad.get_json()['reason'])
        routes._gemini_call_json = lambda *args, **kwargs: self._result(items=[
            {'finding_id': self.f1, 'matched': True, 'status': 'Closed',
             'remark': '완료됨.', 'evidence': 'completed'},
            {'finding_id': self.f1, 'matched': True, 'status': 'Closed',
             'remark': '중복됨.', 'evidence': 'duplicate'},
        ])
        duplicate = self._post()
        self.assertEqual('INCOMPLETE_MATCH', duplicate.get_json()['reason'])

    def test_requires_existing_findings_and_file_part(self):
        with appmod.app.app_context():
            vessel = appmod.execute(
                "INSERT INTO vessels(name,short_name,active) VALUES(?,?,1)",
                ('EMPTY VESSEL', 'EMPTY'),
            )
            empty_vetting = appmod.execute(
                "INSERT INTO vettings(vessel_id,report_number) VALUES(?,?)",
                (vessel, 'EMPTY-REPORT'),
            )
        no_findings = self.client.post(
            f'/api/vettings/{empty_vetting}/apply-full-report',
            data={'file': (io.BytesIO(b'%PDF-1.4'), 'full.pdf')},
            content_type='multipart/form-data', headers={'X-CSRF-Token': self.csrf})
        self.assertEqual('NO_FINDINGS', no_findings.get_json()['reason'])
        no_file = self.client.post(
            f'/api/vettings/{self.vetting}/apply-full-report',
            data={}, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(400, no_file.status_code)
        self.assertEqual('NO_FILE', no_file.get_json()['reason'])


if __name__ == '__main__':
    unittest.main()
