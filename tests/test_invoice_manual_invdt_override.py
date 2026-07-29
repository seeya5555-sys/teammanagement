import json
import os
import tempfile
import unittest

import app as appmod


class InvoiceManualInvDtOverrideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config['DATABASE']
        db = os.path.join(self.tmp.name, 'test.db')
        appmod.DATABASE = db
        appmod.app.config['DATABASE'] = db
        with appmod.app.app_context():
            appmod.init_db(False)
            appmod._ensure_api_table()
            appmod.execute("INSERT OR REPLACE INTO api_settings(k,v) VALUES('api_key',?)", ('secret',))
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id'] = 1
            s['role'] = 'admin'
            s['username'] = 'tester'

    def tearDown(self):
        appmod.DATABASE = self.old_db
        appmod.app.config['DATABASE'] = self.old_cfg
        self.tmp.cleanup()

    def _payload(self, inv_dt='20260714', raw_card=None):
        raw = raw_card or {
            'inv_cd': 'ATGVCI2607290001',
            'inv_no': 'INV-001',
            'inv_dt': inv_dt,
            'amt': 123.45,
            'cur': 'USD',
            'subject': 'Initial subject',
        }
        return {
            'inv_cd': 'ATGVCI2607290001',
            'vsl_cd': 'ATGV',
            'vsl_nm': 'ATLANTIC GREEN',
            'vndr_cd': 'V001',
            'vndr_nm': 'Vendor',
            'amt': 123.45,
            'cur_cd': 'USD',
            'vat': 0,
            'inv_no': 'INV-001',
            'inv_dt': inv_dt,
            'cur_sup': 'SS0094',
            'cur_pic': None,
            'cur_pay_dt': '20260731',
            'set_pic': 'SS0059',
            'set_sup': 'SS0094',
            'set_pay_dt': '20260731',
            'exp_cd': '030606',
            'exp_nm': 'Overtime',
            'subject': raw.get('subject'),
            'inv_no_match': 1,
            'amt_match': 1,
            'date_match': 0,
            'gate': 'HOLD',
            'raw_card': raw,
        }

    def _create(self, payload=None):
        r = self.client.post('/api/ext/invoice/drafts', json=payload or self._payload(), headers={'X-API-Key': 'secret'})
        self.assertEqual(201, r.status_code)
        return r.get_json()['id']

    def _row(self, did):
        with appmod.app.app_context():
            row = appmod.query('SELECT * FROM invoice_draft WHERE id=?', (did,), one=True)
            return dict(row)

    def test_manual_inv_dt_edit_keeps_hold_and_audit(self):
        did = self._create()
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_dt': '20260701'})
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertEqual('20260701', body['inv_dt'])
        self.assertEqual('HOLD', body['gate'])
        row = self._row(did)
        self.assertEqual('20260701', row['inv_dt'])
        self.assertEqual('HOLD', row['gate'])
        rc = json.loads(row['raw_card'])
        self.assertEqual('20260714', rc['original_inv_dt'])
        self.assertEqual('20260701', rc['inv_dt_override'])
        self.assertEqual('tester', rc['inv_dt_override_by'])
        self.assertTrue(rc['date_match'])

    def test_same_inv_dt_edit_is_noop_and_keeps_audit_clean(self):
        did = self._create()
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_dt': '20260714'})
        self.assertEqual(200, r.status_code)
        body = r.get_json()
        self.assertTrue(body['noop'])
        self.assertEqual('20260714', body['inv_dt'])
        self.assertEqual('HOLD', body['gate'])
        rc = json.loads(self._row(did)['raw_card'])
        self.assertNotIn('inv_dt_override', rc)
        self.assertNotIn('original_inv_dt', rc)

    def test_pending_reingest_preserves_manual_inv_dt_override_audit(self):
        did = self._create()
        self.assertEqual(200, self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_dt': '20260701'}).status_code)
        newer = self._payload(inv_dt='20260714', raw_card={
            'inv_cd': 'ATGVCI2607290001',
            'inv_no': 'INV-001',
            'inv_dt': '20260714',
            'amt': 123.45,
            'cur': 'USD',
            'subject': 'Reingested subject',
        })
        r = self.client.post('/api/ext/invoice/drafts', json=newer, headers={'X-API-Key': 'secret'})
        self.assertEqual(200, r.status_code)
        row = self._row(did)
        self.assertEqual('20260701', row['inv_dt'])
        self.assertEqual('HOLD', row['gate'])
        self.assertEqual('Reingested subject', row['subject'])
        rc = json.loads(row['raw_card'])
        self.assertEqual('20260714', rc['original_inv_dt'])
        self.assertEqual('20260701', rc['inv_dt_override'])
        self.assertEqual('20260701', rc['inv_dt'])


if __name__ == '__main__':
    unittest.main()
