"""Class Status 추출에서 Memoranda 항목 결정적 제외 회귀 테스트 (Peru Prosperity 2026-10-04 BV 보고서 형식)."""
import helpers_shared as h

BV_TEXT = """Conditions of Class / Statutory Recommendations
Conditions of Class - Hull
VLC0/2025/J5134-H1C Definitive repairs to be carried out in DEEP TANK in Fr 189 (Bulkhead 05 Jun 2028
between deep tank and PS chain locker) during next dry-dock.
Class Memoranda
Issued Description of Memoranda
22 Apr 2026 MAIN ENGINE SHOULD NOT BE OPERATED CONTINUOUSLY BETWEEN 34 RPM AND 42 RPM. (Copied from Previous
Class AH issued on 28 Oct 2009)
19 Aug 2025 Definitive repairs to be carried out in DEEP TANK in Fr 189 (Bulkhead between deep tank and PS chain locker) during next dry-dock.
Generated on 04 Oct 2026 Page 2 / 3
Statutory Memoranda
22 Apr 2026 Statutory MULTIPLE LOAD LINE CERTIFICATES HAVE BEEN ISSUED TO THIS SHIP AS FOLLOWS:
AT CHANGE OF FLAG IT SHOULD BE VERIFIED THAT THE NEW FLAG PERMITS MULTIPLE LOAD LINE
CERTIFICATES BEFORE MORE THAN ONE LOAD LINE CERTIFICATE IS ISSUED. (Copied from Previous Class SSTO
issued on 18 NOV 2025)
22 Apr 2026 AFTER 1 JANUARY 2026 AND NO LATER THAN THE NEXT RENEWAL SURVEY, THE CARGO SHIP SAFETY
EQUIPMENT CERTIFICATE (FORM 2208) SHALL BE REISSUED.
Planned Inspection Items
Statutory Items
HKG0/2026/J5049-BW1O 22 Apr 2026 Obs 1 Updated and approval Ballast Water Management Plan 21 Jul 2026
"""


def _d(coc, stat):
    return {'coc': [{'description': x} for x in coc], 'statutory': [{'description': x} for x in stat]}


def test_memoranda_items_dropped_real_coc_kept():
    data = _d(
        ['Definitive repairs to be carried out in DEEP TANK in Fr 189 (Bulkhead between deep tank and PS chain locker) during next dry-dock.',
         'MAIN ENGINE SHOULD NOT BE OPERATED CONTINUOUSLY BETWEEN 34 RPM AND 42 RPM. (Copied from Previous Class AH issued on 28 Oct 2009)'],
        ['Statutory MULTIPLE LOAD LINE CERTIFICATES HAVE BEEN ISSUED TO THIS SHIP AS FOLLOWS:',
         'AFTER 1 JANUARY 2026 AND NO LATER THAN THE NEXT RENEWAL SURVEY, THE CARGO SHIP SAFETY EQUIPMENT CERTIFICATE (FORM 2208) SHALL BE REISSUED.'])
    out, n = h._cls_drop_memoranda(data, BV_TEXT)
    assert n == 3
    assert [x['description'][:20] for x in out['coc']] == ['Definitive repairs t']
    assert out['statutory'] == []


def test_no_memoranda_section_is_noop():
    data = _d(['HULL PLATING DENTED TO BE REPAIRED BEFORE NEXT SURVEY'], [])
    out, n = h._cls_drop_memoranda(data, 'Conditions of Class\nHULL PLATING DENTED TO BE REPAIRED BEFORE NEXT SURVEY\n')
    assert n == 0 and len(out['coc']) == 1


def test_wrapped_uppercase_line_does_not_end_memo_section():
    memo, _other = h._cls_split_memoranda(BV_TEXT)
    assert 'AFTER 1 JANUARY 2026' in memo
    assert 'Ballast Water Management Plan' not in memo


def test_uppercase_real_section_after_memoranda_is_kept():
    text = ("Class Memoranda\n22 Apr 2026 MAIN ENGINE SHOULD NOT BE OPERATED CONTINUOUSLY BETWEEN 34 RPM\n"
            "CONDITIONS OF CLASS - HULL\nSHELL PLATING AT FR 120 PORT SIDE TO BE RENEWED BEFORE NEXT DRYDOCK\n")
    data = _d(['SHELL PLATING AT FR 120 PORT SIDE TO BE RENEWED BEFORE NEXT DRYDOCK',
               'MAIN ENGINE SHOULD NOT BE OPERATED CONTINUOUSLY BETWEEN 34 RPM'], [])
    out, n = h._cls_drop_memoranda(data, text)
    assert n == 1 and out['coc'][0]['description'].startswith('SHELL PLATING')
