"""Vetting 행 표시 순서 — 'Next Plan' 은 검사일 미입력이어도 항상 맨 위.

손유석 지시 2026-07-31: 새 Vetting 을 만들고 'Next Plan' 으로 바꾸면 날짜를 안 넣어도
목록 최상단에 있어야 한다(검사일 내림차순만 쓰면 빈 날짜가 맨 밑으로 밀렸음).
웹 상세 테이블과 iOS 앱 대표행이 같은 서버 순서를 쓰므로 여기 1곳이 정본이다.
"""
from pathlib import Path
from unittest import mock
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as appmod  # noqa: E402
from source_bundle import read_app_sources, shared_ns  # noqa: E402

APP_SRC = read_app_sources()
VT_JS = (ROOT / "static" / "js" / "vt.js").read_text()


def ids(rows):
    return [r["id"] for r in rows]


class VettingDisplayOrderTests(unittest.TestCase):
    def test_dateless_next_plan_beats_dated_report(self):
        rows = [
            {"id": 1, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 2, "inspection_date": "", "valid": "Next Plan"},
        ]
        self.assertEqual(ids(appmod._vetting_display_order(rows)), [2, 1])

    def test_dated_next_plan_also_stays_on_top(self):
        rows = [
            {"id": 1, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 2, "inspection_date": "2026-01-09", "valid": "Next Plan"},
        ]
        self.assertEqual(ids(appmod._vetting_display_order(rows)), [2, 1])

    def test_multiple_next_plans_newest_id_first(self):
        rows = [
            {"id": 5, "inspection_date": "2026-09-01", "valid": "Next Plan"},
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
        ]
        self.assertEqual(ids(appmod._vetting_display_order(rows)), [9, 5, 3])

    def test_reports_keep_date_desc_then_id_desc(self):
        rows = [
            {"id": 1, "inspection_date": "2025-02-02", "valid": "Last Result"},
            {"id": 4, "inspection_date": "2026-05-01", "valid": ""},
            {"id": 7, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 2, "inspection_date": "", "valid": "Last Result"},
        ]
        self.assertEqual(ids(appmod._vetting_display_order(rows)), [7, 4, 1, 2])

    def test_blank_status_is_not_treated_as_next_plan(self):
        # 새로 추가한 빈 행(상태 미정)은 계획으로 승격되지 않는다 — 'Next Plan' 으로
        # 바꿨을 때만 위로 올라가야 형이 의도한 대로 동작한다.
        rows = [
            {"id": 1, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 2, "inspection_date": None, "valid": None},
        ]
        self.assertEqual(ids(appmod._vetting_display_order(rows)), [1, 2])

    def test_empty(self):
        self.assertEqual(appmod._vetting_display_order([]), [])


class VettingPickInvariantTests(unittest.TestCase):
    """_vetting_pick 을 **실제로 호출**해 (latest, enr) 계약을 확인한다.

    DB 없이 돌리기 위해 query/_vetting_with_counts 만 갈아끼우고, 선정·정렬 코드 경로는
    진짜 앱 코드를 그대로 태운다 (로직 재구현 검증이면 drift 를 못 잡는다).

    손유석 지시 2026-08-11: 요약행의 OBS/OPEN 은 **상단행 그 자체**의 수치다. 상단이
    Next Plan 이면 직전 Report 수치를 끌어오던 obs_src 폴백은 폐기했다.
    """

    def _pick(self, rows):
        with shared_ns.patch("query", mock.Mock(return_value=rows)), \
             shared_ns.patch("_vetting_with_counts", mock.Mock(side_effect=lambda r: dict(r))):
            return appmod._vetting_pick(1)

    def test_latest_is_dateless_next_plan_and_is_first_row(self):
        latest, enr = self._pick([
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
        ])
        self.assertEqual(latest["id"], 9)          # 날짜 없는 계획이 상단
        self.assertEqual(enr[0]["id"], 9)          # 행 순서 첫 줄 == latest (불변식)

    def test_obs_numbers_come_from_the_next_plan_row_itself(self):
        # 형이 화면에서 지목한 그 케이스: 상단이 Next Plan(0/0)인데 직전 Report(5/0)
        # 숫자가 떠 있었다. 이제 상단행 값이 그대로 나와야 한다.
        latest, _enr = self._pick([
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result",
             "observation_count": 5, "open_count": 0, "close_count": 5},
            {"id": 9, "inspection_date": "", "valid": "Next Plan",
             "observation_count": 0, "open_count": 0, "close_count": 0},
        ])
        self.assertEqual(latest["id"], 9)
        self.assertEqual(latest["observation_count"], 0)
        self.assertEqual(latest["open_count"], 0)

    def test_multiple_next_plans_latest_is_newest_id(self):
        latest, enr = self._pick([
            {"id": 5, "inspection_date": "2026-09-01", "valid": "Next Plan"},
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
        ])
        self.assertEqual(latest["id"], 9)
        self.assertEqual(ids(enr), [9, 5, 3])

    def test_only_reports_keeps_date_desc(self):
        latest, enr = self._pick([
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 1, "inspection_date": "2025-01-01", "valid": "Last Result"},
        ])
        self.assertEqual(latest["id"], 3)
        self.assertEqual(ids(enr), [3, 1])

    def test_only_next_plan(self):
        latest, enr = self._pick([
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
        ])
        self.assertEqual(latest["id"], 9)
        self.assertEqual(ids(enr), [9])

    def test_no_vettings(self):
        self.assertEqual(self._pick([]), (None, []))


class VettingOrderConsumerGuardTests(unittest.TestCase):
    """정렬 정본을 우회하는 코드가 되살아나지 않게 잠근다."""

    def test_api_vettings_uses_display_order_helper(self):
        self.assertIn("by_vessel[vid] = _vetting_display_order(by_vessel[vid])", APP_SRC)

    def test_no_bare_date_only_sort_left_for_groups(self):
        self.assertNotIn(
            "by_vessel[vid].sort(key=lambda x: (x.get('inspection_date') or ''), reverse=True)",
            APP_SRC,
        )

    def test_obs_fallback_is_not_resurrected(self):
        # 서버·웹 어느 쪽이든 "Next Plan 이면 직전 Report 수치" 폴백이 돌아오면
        # 요약행 한 줄 안에서 다시 출처가 갈린다(형이 2026-08-11 에 지목한 오독).
        # 이름만 보면 폐기 사유를 적어둔 주석까지 걸리므로, 되살아난 **코드**만 잡는다.
        self.assertNotIn("obs_src =", APP_SRC)
        self.assertNotIn("obs_src.get", APP_SRC)
        self.assertNotIn("obsSrc", VT_JS)

    def test_digest_payloads_take_numbers_from_the_top_row(self):
        for key in ("'obs_total': latest.get('observation_count') or 0",
                    "'obs_open': latest.get('open_count') or 0"):
            self.assertIn(key, APP_SRC)
        self.assertIn("const total = (latest.observation_count != null)", VT_JS)
        self.assertIn("const openCnt = (latest.open_count != null)", VT_JS)

    def test_web_summary_does_not_call_a_plan_the_last_report(self):
        # 'Last:' 는 실제로 받은 Report 만 가리켜야 한다. vts[0] 직접 사용 금지.
        self.assertNotIn("const latest = vts[0];", VT_JS)
        self.assertIn("const lastReport = vts.find(v => (v.valid || '') !== 'Next Plan');", VT_JS)
        self.assertIn("Last: 이력 없음 · 계획만", VT_JS)


if __name__ == "__main__":
    unittest.main()
