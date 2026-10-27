"""Class Status 추출에서 BV Obs/Observation 행 결정적 제외 회귀 테스트 (Kuwait Prosperity 형식)."""
import copy

import helpers_shared as h

KUWAIT_TEXT = """Conditions of Class / Statutory Recommendations
None
Planned Inspection Items
Statutory Items
SGI0/2026/J5353-AP1O 21 May 2026 Obs 1 Shipowner to provide pending approved nitrate test result to be available not
later than 20/08/2026. Due 20 Aug 2026 Action Date 05 Aug 2026.
Generated on 01 Oct 2026 Page 2 / 3
"""

REC_TEXT = """Conditions of Class / Statutory Recommendations
Statutory Recommendations
SGI0/2026/J5353-AP1R 21 May 2026 Rec 1 Shipowner to provide pending approved nitrate test result to be available not
later than 20/08/2026. Due 20 Aug 2026
Conditions of Class - Hull
VLC0/2025/J5134-H1C 05 Jun 2025 CoC 1 Shipowner to provide pending approved nitrate test result for ballast tank coating
Planned Inspection Items
Statutory Items
SGI0/2026/J5353-AP1O 21 May 2026 Obs 1 Shipowner to provide pending approved nitrate test result to be available not
later than 20/08/2026. Due 20 Aug 2026 Action Date 05 Aug 2026.
"""

NO_OBS_TEXT = """Conditions of Class / Statutory Recommendations
Conditions of Class - Hull
VLC0/2025/J5134-H1C 05 Jun 2025 CoC 1 Definitive repairs to be carried out in DEEP TANK during next dry-dock. 05 Jun 2028
"""

DESC = 'Shipowner to provide pending approved nitrate test result to be available not later than 20/08/2026.'


def _d(coc, stat):
    return {'coc': [{'description': x} for x in coc], 'statutory': [{'description': x} for x in stat]}


def test_kuwait_obs_row_dropped():
    out, n = h._cls_drop_observations(_d([], [DESC, 'Statutory ' + DESC]), KUWAIT_TEXT)
    assert n == 2
    assert out['statutory'] == [] and out['coc'] == []


def test_rec_and_coc_with_similar_wording_kept():
    data = _d(['Shipowner to provide pending approved nitrate test result for ballast tank coating'], [DESC])
    out, n = h._cls_drop_observations(copy.deepcopy(data), REC_TEXT)
    assert n == 0
    assert out == data


def test_no_obs_rows_unchanged():
    data = _d(['Definitive repairs to be carried out in DEEP TANK during next dry-dock.'], [DESC])
    out, n = h._cls_drop_observations(copy.deepcopy(data), NO_OBS_TEXT)
    assert n == 0 and out == data


def test_unrelated_item_kept_when_obs_present():
    data = _d(['Hull plating to be examined at next annual survey in way of No.3 cargo hold'], [])
    out, n = h._cls_drop_observations(copy.deepcopy(data), KUWAIT_TEXT)
    assert n == 0 and out == data
