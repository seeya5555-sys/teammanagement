import io
import json
import os
import tempfile
import unittest

import app as appmod
import ai_gemini as routes
import helpers_shared


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
                "INSERT INTO vettings(vessel_id,report_number,inspection_date) VALUES(?,?,?)",
                (vessel, 'LVKX-0383-3966-7845', '2026-08-06'),
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

    def _result(self, *, report='LVKX-0383-3966-7845', items=None,
                new_items=None, report_type='Full'):
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
            'new_items': new_items or [],
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
        self.assertEqual(0, first.get_json()['created'])
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

    def test_unmatched_existing_is_preserved_and_full_only_item_is_added_once(self):
        new_item = {
            'item': '(Process)Not as expected - procedure and/or document deficient',
            'description': 'The total number of persons was not identified on Form E.',
            'translation': 'Form E에 lifesaving appliances 총 정원 미기재됨.',
            'status': 'Closed',
            'action_remark': 'Form E를 수정하고 정확한 총 정원을 반영 완료함.',
            'evidence': 'Form E was corrected and reissued.',
        }
        first_items = [
            {'finding_id': self.f1, 'matched': True, 'status': 'Open',
             'remark': 'COC 모니터링 중임.', 'evidence': 'pending Class survey'},
            {'finding_id': self.f2, 'matched': False, 'status': 'Open',
             'remark': 'not found', 'evidence': 'not found'},
        ]
        routes._gemini_call_json = lambda *args, **kwargs: self._result(
            items=first_items, new_items=[new_item, {
                **new_item,
                'item': '(Process)Largely as expected - procedure and/or document present',
                'description': 'A minor informational comment that is not an Observation.',
            }])
        first = self._post()
        self.assertEqual(200, first.status_code, first.get_data(as_text=True))
        payload = first.get_json()
        self.assertEqual((1, 1, 1),
                         (payload['updated'], payload['created'], payload['unmatched']))
        with appmod.app.app_context():
            rows = appmod.query(
                'SELECT id,no,description,status,user_remark FROM vt_findings '
                'WHERE vetting_id=? ORDER BY no', (self.vetting,))
        self.assertEqual(3, len(rows))
        self.assertEqual('Open', rows[1]['status'])       # Full에 없던 기존행 보존
        self.assertEqual('Closed', rows[2]['status'])
        self.assertIn('Form E를 수정', rows[2]['user_remark'])

        new_id = rows[2]['id']
        second_items = first_items + [
            {'finding_id': new_id, 'matched': True, 'status': 'Closed',
             'remark': new_item['action_remark'], 'evidence': new_item['evidence']},
        ]
        routes._gemini_call_json = lambda *args, **kwargs: self._result(
            items=second_items, new_items=[new_item])
        second = self._post()
        self.assertEqual(200, second.status_code, second.get_data(as_text=True))
        self.assertEqual(0, second.get_json()['created'])
        with appmod.app.app_context():
            count = appmod.query(
                'SELECT COUNT(*) n FROM vt_findings WHERE vetting_id=?',
                (self.vetting,), one=True)['n']
        self.assertEqual(3, count)

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
        self.assertEqual('INVALID_MATCH_SET', duplicate.get_json()['reason'])

        routes._gemini_call_json = lambda *args, **kwargs: self._result(items=[
            {'finding_id': self.f1, 'matched': True, 'status': 'Closed',
             'remark': '완료됨.', 'evidence': 'completed'},
            {'finding_id': self.f2, 'matched': True, 'status': 'Closed',
             'remark': '완료됨.', 'evidence': 'completed'},
            {'finding_id': 999999, 'matched': True, 'status': 'Closed',
             'remark': '환각.', 'evidence': 'hallucinated'},
        ])
        hallucinated = self._post()
        self.assertEqual('INVALID_MATCH_SET', hallucinated.get_json()['reason'])

    def test_similar_wording_is_deduplicated(self):
        self.assertTrue(routes._finding_text_similar(
            'It was noted that the total number of persons was not identified on Form E.',
            'The total number of persons for life-saving appliances was not identified in the Form E.',
        ))
        self.assertFalse(routes._finding_text_similar(
            'The total number of persons was not identified on Form E.',
            'The SIMOPS plan omitted cargo discharge and stores supply.',
        ))

    def test_legacy_auto_block_is_hidden_from_overall_remark(self):
        legacy = (
            '8/6 PETROVIETNAM DISCHARGE SIRE OBS 잔여 3건 조치 중\n'
            '1. M/E NO.6 Cylinder cover Condition of Class 미종결 - '
            f'{routes._FULL_REPORT_MARKER}\n긴 자동 조치 설명\n'
            f'{routes._FULL_REPORT_END_MARKER}\n그 외 Minor 지적 2건'
        )
        with appmod.app.app_context():
            appmod.execute('UPDATE vettings SET overall_remark=? WHERE id=?',
                           (legacy, self.vetting))
        shown = self.client.get(f'/api/vettings/{self.vetting}').get_json()['overall_remark']
        self.assertNotIn('자동반영', shown)
        self.assertNotIn('긴 자동 조치 설명', shown)
        self.assertIn('1. M/E NO.6 Cylinder cover Condition of Class 미종결', shown)
        self.assertIn('그 외 Minor 지적 2건', shown)
        self.assertEqual('수동 메모', helpers_shared._clean_vetting_overall_remark('수동 메모'))
        incomplete = f'수동 메모\n{routes._FULL_REPORT_MARKER}\n미완료 블록'
        self.assertEqual(incomplete, helpers_shared._clean_vetting_overall_remark(incomplete))

    def test_obs_summary_keeps_finding_dash_concise_remark_style(self):
        marked = routes._replace_full_report_remark(
            '', 'starting valve seating 수리 후 Condition of Class 모니터링 중임.')
        with appmod.app.app_context():
            appmod.execute(
                'UPDATE vt_findings SET priority=1, remark=?, user_remark=? WHERE id=?',
                ('M/E NO.6 Cylinder cover Condition of Class 미종결', marked, self.f1))
        routes._gemini_call_json = lambda *args, **kwargs: {
            'items': [{'i': 0, 'short': 'M/E NO.6 Cylinder cover Condition of Class 미종결'}]
        }
        response = self.client.post(
            f'/api/vettings/{self.vetting}/obs-summary',
            headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        summary = response.get_json()['summary']
        self.assertIn(
            '1. M/E NO.6 Cylinder cover Condition of Class 미종결 - '
            'starting valve seating 수리 후 Condition of Class 모니터링 중임.',
            summary,
        )
        self.assertNotIn('자동반영', summary)

    def test_obs_summary_concises_unmarked_manual_remark(self):
        with appmod.app.app_context():
            appmod.execute(
                'UPDATE vt_findings SET priority=1, remark=?, user_remark=? WHERE id=?',
                ('ECDIS 점검 필요', 'ECDIS software update 완료함. 불필요한 원인 설명임.', self.f1))
        routes._gemini_call_json = lambda *args, **kwargs: {
            'items': [{'i': 0, 'short': 'ECDIS 점검 필요'}]
        }
        response = self.client.post(
            f'/api/vettings/{self.vetting}/obs-summary',
            headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        summary = response.get_json()['summary']
        self.assertIn('1. ECDIS 점검 필요 - ECDIS software update 완료함.', summary)
        self.assertNotIn('불필요한 원인 설명', summary)

    def test_summary_remark_hides_marker_with_crlf_and_manual_prefix(self):
        value = (
            '수동 메모 보존\r\n\r\n'
            f'  {routes._FULL_REPORT_MARKER}  \r\n'
            'UT/MPI 재검사 완료함. Root Cause 장문 설명임.\r\n'
            f'  {routes._FULL_REPORT_END_MARKER}  '
        )
        shown = routes._summary_full_report_remark(value)
        self.assertEqual('UT/MPI 재검사 완료함.', shown)
        self.assertNotIn('[', shown)

    def test_full_report_action_remark_is_one_sentence_and_length_bounded(self):
        concise = routes._concise_full_report_remark(
            'starting valve seating 수리 완료함. Root Cause 장문 설명은 종합소견에 불필요함.')
        self.assertEqual('starting valve seating 수리 완료함.', concise)
        long_text = 'Condition of Class에 따라 UT/MPI 재검사 및 모니터링 예정이며 ' + ('추가 설명 ' * 20)
        bounded = routes._concise_full_report_remark(long_text)
        self.assertLessEqual(len(bounded), 90)
        self.assertIn('Condition of Class', bounded)
        self.assertIn('UT/MPI', bounded)

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
