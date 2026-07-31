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

APP_SRC = (ROOT / "app.py").read_text()
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
    """_vetting_pick 을 **실제로 호출**해 (latest, obs_src, enr) 계약을 확인한다.

    DB 없이 돌리기 위해 query/_vetting_with_counts 만 갈아끼우고, 선정·정렬 코드 경로는
    진짜 앱 코드를 그대로 태운다 (로직 재구현 검증이면 drift 를 못 잡는다).
    """

    def _pick(self, rows):
        with mock.patch.object(appmod, "query", return_value=rows), \
             mock.patch.object(appmod, "_vetting_with_counts", side_effect=lambda r: dict(r)):
            return appmod._vetting_pick(1)

    def test_latest_is_dateless_next_plan_and_is_first_row(self):
        latest, obs_src, enr = self._pick([
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
        ])
        self.assertEqual(latest["id"], 9)          # 날짜 없는 계획이 상단
        self.assertEqual(enr[0]["id"], 9)          # 행 순서 첫 줄 == latest (불변식)
        self.assertEqual(obs_src["id"], 3)         # OBS 수치는 직전 Report 에서

    def test_multiple_next_plans_latest_is_newest_id(self):
        latest, _obs, enr = self._pick([
            {"id": 5, "inspection_date": "2026-09-01", "valid": "Next Plan"},
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
        ])
        self.assertEqual(latest["id"], 9)
        self.assertEqual(ids(enr), [9, 5, 3])

    def test_only_reports_keeps_date_desc(self):
        latest, obs_src, enr = self._pick([
            {"id": 3, "inspection_date": "2026-05-01", "valid": "Last Result"},
            {"id": 1, "inspection_date": "2025-01-01", "valid": "Last Result"},
        ])
        self.assertEqual(latest["id"], 3)
        self.assertEqual(obs_src["id"], 3)         # 계획이 없으면 obs_src == latest
        self.assertEqual(ids(enr), [3, 1])

    def test_only_next_plan_falls_back_to_itself_for_obs(self):
        latest, obs_src, _enr = self._pick([
            {"id": 9, "inspection_date": "", "valid": "Next Plan"},
        ])
        self.assertEqual(latest["id"], 9)
        self.assertEqual(obs_src["id"], 9)

    def test_no_vettings(self):
        self.assertEqual(self._pick([]), (None, None, []))


class VettingOrderConsumerGuardTests(unittest.TestCase):
    """정렬 정본을 우회하는 코드가 되살아나지 않게 잠근다."""

    def test_api_vettings_uses_display_order_helper(self):
        self.assertIn("by_vessel[vid] = _vetting_display_order(by_vessel[vid])", APP_SRC)

    def test_no_bare_date_only_sort_left_for_groups(self):
        self.assertNotIn(
            "by_vessel[vid].sort(key=lambda x: (x.get('inspection_date') or ''), reverse=True)",
            APP_SRC,
        )

    def test_web_summary_does_not_call_a_plan_the_last_report(self):
        # 'Last:' 는 실제로 받은 Report 만 가리켜야 한다. vts[0] 직접 사용 금지.
        self.assertNotIn("const latest = vts[0];", VT_JS)
        self.assertIn("const lastReport = vts.find(v => (v.valid || '') !== 'Next Plan');", VT_JS)
        self.assertIn("Last: 이력 없음 · 계획만", VT_JS)


if __name__ == "__main__":
    unittest.main()
