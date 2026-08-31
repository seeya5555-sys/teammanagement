import unittest

import routes_calendar_dock as routes


class RemittanceContractTests(unittest.TestCase):
    def test_overdue_requires_strict_valid_eight_digit_date(self):
        self.assertTrue(routes._remittance_overdue('20260830', '20260831'))
        self.assertFalse(routes._remittance_overdue('20260831', '20260831'))
        self.assertFalse(routes._remittance_overdue('20260901', '20260831'))
        self.assertFalse(routes._remittance_overdue('', '20260831'))
        self.assertFalse(routes._remittance_overdue('2026-08-30T00:00:00', '20260831'))

    def test_read_only_first_stage_contract_is_present(self):
        with open(routes.__file__, encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn("source_type not in ('fundreq', 'invoice')", source)
        self.assertIn("payment_state not in ('unpaid', 'paid', 'unknown')", source)
        self.assertIn("row.get('payment_state') == 'unpaid' and row['overdue']", source)
        self.assertIn("/api/ext/remittance/drafts", source)
        self.assertNotIn("svms.save", source)


if __name__ == '__main__':
    unittest.main()
