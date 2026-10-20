"""조치계획 계획형('~예정') 보정 — 형 지시 2026-10-20."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRMT_TESTING', '1')
import ai_gemini as a


class PlanForm(unittest.TestCase):
    def test_cases(self):
        cases = {
            'Crew matrix 및 HVPQ 기재 오류 수정 및 정확한 데이터 입력 필요.': 'Crew matrix 및 HVPQ 기재 오류 수정 및 정확한 데이터 입력 조치 예정.',
            '비상등 불량 수리 필요.': '비상등 불량 수리 예정.',
            '수리 필요. 교체 필요.': '수리 예정. 교체 예정.',
            '수리 필요\n교체 필요': '수리 예정\n교체 예정',
            '교체 필요, 추가 점검 필요.': '교체 예정, 추가 점검 예정.',
            '시정 불필요.': '시정 불필요.',
            '[확인 필요] 회신 확인 필요': '[확인 필요] 회신 확인 필요',
            '필요 시 추가 수리.': '필요 시 추가 수리.',
            'Spare 보급 후 신환 예정.': 'Spare 보급 후 신환 예정.',
        }
        for src, want in cases.items():
            self.assertEqual(a._plan_form(src), want, src)

    def test_en_only(self):
        self.assertEqual(a._en_only('지난 운전 후 재(ash)가 제거됨'), '지난 운전 후 ash가 제거됨')
        self.assertEqual(a._en_only('필수 장비(critical equipment)에 대한'), '필수 critical equipment에 대한')
        self.assertEqual(a._en_only('점검 후 평가(risk assessment) 미실시'), '점검 후 risk assessment 미실시')
        self.assertEqual(a._en_only('주기관(M/E) 및 재 (ash)'), 'M/E 및 ash')
        self.assertEqual(a._en_only('Main deck 부식 (No.3)'), 'Main deck 부식 (No.3)')

    def test_concise_applies(self):
        self.assertEqual(a._concise_full_report_remark('수리 필요.\n교체 필요.'), '수리 예정. 교체 예정.')


if __name__ == '__main__':
    unittest.main()
