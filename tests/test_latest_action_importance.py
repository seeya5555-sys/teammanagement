import unittest
from maritime_style import append_latest_important
from ai_gemini import _concise_full_report_remark

class LatestImportanceTests(unittest.TestCase):
    def test_preserves_metadata_and_input_even_for_backdated_append(self):
        old = [{'date': '2026-10-04', 'progress': 'unchanged', 'important': True, 'ref': 'x'}]
        new = {'date': '2026-09-01', 'progress': 'new', 'important': False}
        got = append_latest_important(old, new)
        self.assertEqual(got, [dict(old[0], important=False), dict(new, important=True)])
        self.assertTrue(old[0]['important'])
        self.assertFalse(new['important'])
    def test_consecutive_append_only_last_is_important(self):
        got = []
        for n in range(3):
            got = append_latest_important(got, {'progress': str(n)})
        self.assertEqual([False, False, True], [x['important'] for x in got])
    def test_coc_and_repair_without_statutory_conflation(self):
        got = _concise_full_report_remark('보수 완료. Condition of Class (CoC) 유효. Condition of Statutory pending.')
        self.assertEqual(got, '수리 완료. COC 유효. Condition of Statutory pending.')

    def test_no_corruption_of_unrelated_terms_or_acronym_particles(self):
        got = _concise_full_report_remark('유지보수 보수적 판단 Condition of Classification. CoC가 유효. Class Condition이 발행됨.')
        self.assertEqual(got, '유지보수 보수적 판단 Condition of Classification. COC 유효. COC 발행됨.')
