import json
import unittest

from test_invoice_manual_invdt_override import InvoiceManualInvDtOverrideTests as _Base
import app as appmod


class InvoiceManualInvNoOverrideTests(_Base):
    """INV_NO 수동교정 — 검증·audit·되돌림·재적재 보존/폐기."""

    def test_inv_no_edit_sets_override_and_audit(self):
        did = self._create()
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': '  V-001  '})
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertEqual('V-001', r.get_json()['inv_no'])
        row = self._row(did)
        self.assertEqual('V-001', row['inv_no'])
        self.assertEqual(1, row['inv_no_match'])
        self.assertEqual('HOLD', row['gate'])                 # gate 자동승격 없음
        rc = json.loads(row['raw_card'])
        self.assertEqual('INV-001', rc['original_inv_no'])
        self.assertEqual('V-001', rc['inv_no_override'])
        self.assertEqual('tester', rc['inv_no_override_by'])
        self.assertEqual(1, rc['original_inv_no_match'])

    def test_second_edit_keeps_first_original(self):
        did = self._create()
        self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-002'})
        rc = json.loads(self._row(did)['raw_card'])
        self.assertEqual('INV-001', rc['original_inv_no'])
        self.assertEqual('V-002', rc['inv_no_override'])

    def test_revert_to_original_clears_override(self):
        did = self._create()
        self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'INV-001'})
        self.assertEqual(200, r.status_code)
        row = self._row(did)
        self.assertEqual('INV-001', row['inv_no'])
        self.assertEqual(1, row['inv_no_match'])               # prep 판정 복원
        rc = json.loads(row['raw_card'])
        for k in ('original_inv_no', 'inv_no_override', 'inv_no_override_by', 'original_inv_no_match'):
            self.assertNotIn(k, rc)

    def test_invalid_inv_no_rejected(self):
        did = self._create()
        for bad in ('', '   ', 'A' * 51, 'AB\nCD', 'AB\tCD'):
            r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': bad})
            self.assertEqual(400, r.status_code, bad)
            self.assertEqual('inv_no', r.get_json()['field'])
        self.assertEqual('INV-001', self._row(did)['inv_no'])

    def test_same_value_is_noop(self):
        did = self._create()
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'INV-001'})
        self.assertTrue(r.get_json().get('noop'))
        self.assertNotIn('inv_no_override', json.loads(self._row(did)['raw_card']))

    def test_non_pending_cannot_edit(self):
        did = self._create()
        with appmod.app.app_context():
            appmod.execute("UPDATE invoice_draft SET status='approved' WHERE id=?", (did,))
        r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        self.assertEqual(409, r.status_code)

    def test_reingest_same_svms_value_preserves_override(self):
        did = self._create()
        self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        r = self.client.post('/api/ext/invoice/drafts', json=self._payload(), headers={'X-API-Key': 'secret'})
        self.assertEqual(200, r.status_code)
        row = self._row(did)
        self.assertEqual('V-001', row['inv_no'])
        rc = json.loads(row['raw_card'])
        self.assertEqual('INV-001', rc['original_inv_no'])
        self.assertEqual('V-001', rc['inv_no_override'])

    def test_ingest_cannot_inject_override_audit(self):
        p = self._payload()
        p['raw_card'] = dict(p['raw_card'], original_inv_no='INV-001', inv_no_override='EVIL-1',
                             inv_no_override_by='x', original_inv_no_match=1)
        did = self._create(p)
        rc = json.loads(self._row(did)['raw_card'])
        for k in ('original_inv_no', 'inv_no_override', 'inv_no_override_by', 'original_inv_no_match'):
            self.assertNotIn(k, rc)

    def test_stale_raw_card_edit_rejected(self):
        """동시 편집: 편집 SELECT 이후 raw_card 가 바뀌면 덮어쓰지 않고 409."""
        did = self._create()
        import routes_calendar_dock as R
        real_query = R.query
        def racing_query(sql, args=(), one=False):
            res = real_query(sql, args, one)
            if sql.startswith('SELECT raw_card, status, inv_dt, inv_no'):
                appmod.execute("UPDATE invoice_draft SET raw_card=? WHERE id=?",
                               (json.dumps({'inv_no': 'INV-001', 'subject': 'other edit'}), did))
            return res
        R.query = racing_query
        try:
            r = self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        finally:
            R.query = real_query
        self.assertEqual(409, r.status_code)
        row = self._row(did)
        self.assertEqual('INV-001', row['inv_no'])
        self.assertEqual('other edit', json.loads(row['raw_card'])['subject'])

    def test_reingest_changed_svms_value_drops_override(self):
        did = self._create()
        self.client.post(f'/api/invoice/drafts/{did}/edit', json={'inv_no': 'V-001'})
        p = self._payload()
        p['inv_no'] = 'INV-009'
        p['raw_card'] = dict(p['raw_card'], inv_no='INV-009')
        self.assertEqual(200, self.client.post('/api/ext/invoice/drafts', json=p,
                                               headers={'X-API-Key': 'secret'}).status_code)
        row = self._row(did)
        self.assertEqual('INV-009', row['inv_no'])
        self.assertNotIn('inv_no_override', json.loads(row['raw_card']))


for _n in [n for n in dir(_Base) if n.startswith('test_')]:
    setattr(InvoiceManualInvNoOverrideTests, _n, None)

if __name__ == '__main__':
    unittest.main()
