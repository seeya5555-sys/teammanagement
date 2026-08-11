"""reqgen 파서 단위코드 회귀 — 2026-08-05 S33(AUX BOILER) 저장 실패.

엑셀 UNIT 칸 'Pieces'(6자)가 그대로 SVMS 로 가서 `SP_SET_REQ_INFO` 가 ORA-06502 로 죽었다.
SVMS 실측 경계: PUNIT_CD 4자까지 허용, 5자부터 거절(코드마스터 검증 없음 = 길이만 본다).
저장을 실제로 막는 fail-closed 가드는 맥 러너(`automation/svms-soa-opex/reqgen_save.py`)에 있고,
여기(파서)는 카드 화면에 코드로 보이게 접는 역할.
"""
import unittest

import app as appmod
from source_bundle import shared_ns


class ReqGenUnitCodeTests(unittest.TestCase):
    def test_human_words_fold_to_svms_codes(self):
        cases = {
            'Pieces': 'EA',     # 실사고 값
            'pcs': 'EA',
            'PCS.': 'EA',
            'Each': 'EA',
            'Sets': 'SET',
            'Meters': 'M',
            'Sheet': 'SHT',
        }
        for raw, code in cases.items():
            self.assertEqual(shared_ns._reqgen_unit_cd(raw), code, raw)

    def test_blank_is_none_and_unknown_is_kept_verbatim(self):
        self.assertIsNone(shared_ns._reqgen_unit_cd(None))
        self.assertIsNone(shared_ns._reqgen_unit_cd('   '))
        # 모르는 값을 조용히 4자로 자르면 뜻이 바뀐 단위가 발주까지 흘러간다 → 원문 유지하고 러너가 막는다.
        self.assertEqual(shared_ns._reqgen_unit_cd('Nozzle'), 'Nozzle')

    def test_every_mapped_code_fits_svms_limit(self):
        too_long = sorted({v for v in shared_ns._REQGEN_UNIT_MAP.values() if len(v) > 4})
        self.assertEqual(too_long, [], f'4자 초과 코드는 ORA-06502 유발: {too_long}')


if __name__ == '__main__':
    unittest.main()
