import io
import os
import tempfile
import unittest
from unittest.mock import patch

import app as appmod


class ReqGenSupplyChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        appmod.app.config['TESTING'] = True
        with appmod.app.app_context():
            appmod.init_db(False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = 1
            session['username'] = 'supply-test'
            session['role'] = 'admin'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def approve_all(self):
        return self.client.post('/api/reqgen/approve-all', json={
            'voyage': '001', 'port': 'KRPUS', 'req_dt': '20260802',
            'cause': 'Test cause', 'inspection': 'Test inspection',
        })

    def test_new_repair_requires_choice_and_legacy_service_still_approves(self):
        parsed = ('TEST VESSEL', [{
            'doc_type': 'MA', 'sheet': 'R1', 'equipment': 'MAIN ENGINE',
            'subj': 'TEST REPAIR',
            'header': {'VSL_NM': 'TEST VESSEL', 'CATE_NM': 'MAIN ENGINE', 'EQ_NM': 'MAIN ENGINE'},
            'lines': [{'scope': 'Renew test component', 'unit': 'JOB', 'qty': 1, 'remark': ''}],
        }], 0)
        with patch.object(appmod, '_reqgen_parse_workbook', return_value=parsed):
            upload = self.client.post('/api/reqgen/upload', data={
                'file': (io.BytesIO(b'test workbook'), 'dock.xlsx'),
                'vsl_cd': 'TEST',
            }, content_type='multipart/form-data')
        self.assertEqual(201, upload.status_code, upload.get_json())

        with appmod.app.app_context():
            new_row = appmod.query("SELECT * FROM reqgen_draft WHERE sheet='R1'", one=True)
            self.assertEqual('unselected', new_row['stock'])
            legacy_id = appmod.execute(
                "INSERT INTO reqgen_draft(batch,doc_type,sheet,vsl_cd,vsl_nm,equipment,subj,"
                "header_json,lines_json,stock) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('legacy', 'MA', 'R0', 'TEST', 'TEST VESSEL', 'MAIN ENGINE', 'LEGACY',
                 '{"CATE_NM":"MAIN ENGINE","EQ_NM":"MAIN ENGINE"}', '[]', 'service'))

        first = self.approve_all()
        self.assertEqual(200, first.status_code, first.get_json())
        self.assertEqual(1, first.get_json()['approved'])
        self.assertEqual(['R1'], first.get_json()['blocked_stock'])
        with appmod.app.app_context():
            legacy = appmod.query('SELECT * FROM reqgen_draft WHERE id=?', (legacy_id,), one=True)
            self.assertEqual('approved', legacy['status'])
            self.assertIn('supplied by service company', legacy['header_json'])

        patched = self.client.patch(f"/api/reqgen/drafts/{new_row['id']}", json={'stock': 'vendor'})
        self.assertEqual(200, patched.status_code, patched.get_json())
        second = self.approve_all()
        self.assertEqual(200, second.status_code, second.get_json())
        self.assertEqual(1, second.get_json()['approved'])
        with appmod.app.app_context():
            selected = appmod.query('SELECT * FROM reqgen_draft WHERE id=?', (new_row['id'],), one=True)
            self.assertEqual('approved', selected['status'])
            self.assertIn('supplied by service company', selected['header_json'])


if __name__ == '__main__':
    unittest.main()
