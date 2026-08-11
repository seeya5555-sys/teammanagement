"""돈경로 일괄작업 행위 계약 — 90일 Blueprint 이동 전 필수.

대상 2개는 돈경로 43개 중 **테스트에 문자열조차 안 나오던** 라우트이고,
동시에 실패했을 때 피해가 가장 큰 쌍이다:

  POST   /api/invoice/drafts/approve-bulk   체크된 건 일괄 승인
  DELETE /api/invoice/drafts/decided        처리완료 건 일괄 삭제

여기서 지켜야 하는 것은 응답 모양이 아니라 **상태 게이트**다.
  · approve-bulk 는 `status IN ('pending','rejecting')` 만 승인해야 한다.
    이미 approved/submitting/submitted 인 건이 다시 승인되면 **이중집행**이다.
  · clear-decided 는 종결 상태(submitted/rejected/failed/reject_failed) 만 지워야 한다.
    pending/approved/rejecting/submitting 이 함께 지워지면 **진행 중인 돈건이 소멸**한다.

두 게이트 모두 SQL `WHERE` 절 안에만 존재한다 — 라우트를 Blueprint 로 옮기거나
쿼리를 리팩터할 때 조건 하나만 빠져도 조용히 통과한다. 그래서 상태별 전이를
개별로 못박는다. 응답 카운트가 아니라 **DB 최종 상태**를 단정하는 게 요점이다.
"""
import os
import tempfile
import unittest

import app as appmod


FINAL_STATES = ("submitted", "rejected", "failed", "reject_failed")
LIVE_STATES = ("pending", "approved", "rejecting", "submitting")


class MoneyBulkContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = appmod.DATABASE
        self.old_cfg = appmod.app.config["DATABASE"]
        self.old_testing = appmod.app.config.get("TESTING")
        db = os.path.join(self.tmp.name, "money_bulk.db")
        appmod.DATABASE = db
        appmod.app.config["DATABASE"] = db
        appmod.app.config["TESTING"] = True
        with appmod.app.app_context():
            appmod.init_db(drop=False)
        self.client = appmod.app.test_client()
        with self.client.session_transaction() as session:
            session.update(
                user_id=1, username="bulk-admin", display_name="Bulk Admin",
                role="admin", supervisor_id=None,
            )

    def tearDown(self):
        # TESTING 도 원복 — 전역 단일 app 이라 켠 채로 두면 뒤 테스트가 실행순서에 의존한다.
        appmod.DATABASE = self.old_db
        appmod.app.config["DATABASE"] = self.old_cfg
        appmod.app.config["TESTING"] = self.old_testing
        self.tmp.cleanup()

    # ── fixtures ──────────────────────────────────────────────
    def _draft(self, status="pending", raw_card="{}", inv_cd=None):
        with appmod.app.app_context():
            return appmod.execute(
                "INSERT INTO invoice_draft(inv_cd, status, raw_card, created_at) "
                "VALUES(?,?,?,datetime('now','localtime'))",
                (inv_cd or f"INV-{status}-{os.urandom(3).hex()}", status, raw_card),
            )

    def _row(self, did):
        with appmod.app.app_context():
            return appmod.query("SELECT * FROM invoice_draft WHERE id=?", (did,), one=True)

    def _statuses(self):
        with appmod.app.app_context():
            return {r["id"]: r["status"]
                    for r in appmod.query("SELECT id, status FROM invoice_draft")}

    # ── approve-bulk ──────────────────────────────────────────
    def test_approve_bulk_approves_only_pending_and_rejecting(self):
        pending = self._draft("pending")
        rejecting = self._draft("rejecting")
        response = self.client.post("/api/invoice/drafts/approve-bulk",
                                    json={"ids": [pending, rejecting]})
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual(2, body["approved"])
        self.assertEqual(0, body["skipped"])
        self.assertEqual({pending, rejecting}, set(body["approved_ids"]))
        for did in (pending, rejecting):
            self.assertEqual("approved", self._row(did)["status"])

    def test_approve_bulk_never_reapproves_decided_or_inflight(self):
        """이미 결정·진행 중인 건이 다시 승인되면 이중집행이다.

        승인 허용은 pending·rejecting 뿐이므로, 나머지 상태 전부를 명시적으로 깐다
        (`reject_failed` 포함 — 반려 실패 잔상이 승인으로 되살아나면 안 된다).
        """
        guarded = {state: self._draft(state)
                   for state in set(LIVE_STATES + FINAL_STATES) - {"pending", "rejecting"}}
        before = self._statuses()
        response = self.client.post("/api/invoice/drafts/approve-bulk",
                                    json={"ids": sorted(guarded.values())})
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertEqual(0, body["approved"], f"이중집행 위험: {body}")
        self.assertEqual(len(guarded), body["skipped"])
        self.assertEqual([], body["approved_ids"])
        self.assertEqual(before, self._statuses(), "상태가 변경됨 — 게이트가 열렸음")

    def test_approve_bulk_skips_rows_without_raw_card(self):
        """raw_card 없는 건은 상신 원본이 없다 = 승인 대상이 아니다."""
        empty = self._draft("pending", raw_card=None)
        blank = self._draft("pending", raw_card="")
        response = self.client.post("/api/invoice/drafts/approve-bulk",
                                    json={"ids": [empty, blank]})
        body = response.get_json()
        self.assertEqual(0, body["approved"])
        self.assertEqual(2, body["skipped"])
        for did in (empty, blank):
            self.assertEqual("pending", self._row(did)["status"])

    def test_approve_bulk_skips_unknown_ids_without_error(self):
        response = self.client.post("/api/invoice/drafts/approve-bulk",
                                    json={"ids": [999999, 1000000]})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"approved": 0, "skipped": 2, "approved_ids": []},
                         response.get_json())

    def test_approve_bulk_records_audit_trail(self):
        """누가·언제 승인했는지가 남아야 한다 — 돈경로 감사 근거."""
        did = self._draft("pending")
        self.client.post("/api/invoice/drafts/approve-bulk", json={"ids": [did]})
        row = self._row(did)
        self.assertEqual("bulk-admin", row["decided_by"])
        self.assertTrue(row["decided_at"], "decided_at 이 비었음")

    def test_approve_bulk_mixed_batch_counts_are_exact(self):
        ok_one, ok_two = self._draft("pending"), self._draft("rejecting")
        blocked = self._draft("submitted")
        no_card = self._draft("pending", raw_card=None)
        body = self.client.post(
            "/api/invoice/drafts/approve-bulk",
            json={"ids": [ok_one, blocked, no_card, ok_two, 999999]},
        ).get_json()
        self.assertEqual(2, body["approved"])
        self.assertEqual(3, body["skipped"])
        self.assertEqual({ok_one, ok_two}, set(body["approved_ids"]))
        self.assertEqual("submitted", self._row(blocked)["status"])
        self.assertEqual("pending", self._row(no_card)["status"])

    def test_approve_bulk_empty_and_missing_ids_are_noops(self):
        did = self._draft("pending")
        for payload in ({"ids": []}, {}, None):
            body = self.client.post("/api/invoice/drafts/approve-bulk",
                                    json=payload).get_json()
            self.assertEqual({"approved": 0, "skipped": 0, "approved_ids": []}, body)
        self.assertEqual("pending", self._row(did)["status"])

    def test_approve_bulk_duplicate_ids_do_not_double_count(self):
        """같은 id 를 두 번 담아 보내도 승인은 1건이어야 한다.

        UI 실수나 재시도로 중복 id 가 섞이는 것은 실제로 일어난다. 카운트가 부풀면
        형이 화면에서 보는 승인건수가 실제 DB 와 어긋난다.
        """
        did = self._draft("pending")
        body = self.client.post("/api/invoice/drafts/approve-bulk",
                                json={"ids": [did, did, did]}).get_json()
        self.assertEqual(1, body["approved"], f"중복 id 가 중복 집계됨: {body}")
        self.assertEqual([did], body["approved_ids"])
        self.assertEqual("approved", self._row(did)["status"])

    def test_approve_bulk_resend_is_not_reexecuted(self):
        """같은 요청을 다시 보내면 두 번째는 승인 0 이어야 한다(멱등)."""
        did = self._draft("pending")
        first = self.client.post("/api/invoice/drafts/approve-bulk",
                                 json={"ids": [did]}).get_json()
        second = self.client.post("/api/invoice/drafts/approve-bulk",
                                  json={"ids": [did]}).get_json()
        self.assertEqual(1, first["approved"])
        self.assertEqual(0, second["approved"], f"재전송이 재집행됨: {second}")
        self.assertEqual(1, second["skipped"])
        self.assertEqual("approved", self._row(did)["status"])

    # ── clear decided ─────────────────────────────────────────
    def test_clear_decided_removes_only_final_states(self):
        final = {state: self._draft(state) for state in FINAL_STATES}
        live = {state: self._draft(state) for state in LIVE_STATES}
        response = self.client.delete("/api/invoice/drafts/decided")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True, "deleted": len(final)}, response.get_json())
        remaining = self._statuses()
        for state, did in final.items():
            self.assertNotIn(did, remaining, f"{state} 가 삭제되지 않음")
        for state, did in live.items():
            self.assertIn(did, remaining, f"진행 중인 {state} 건이 삭제됨 — 돈건 소멸")
            self.assertEqual(state, remaining[did])

    def test_clear_decided_is_noop_when_nothing_is_final(self):
        live = {state: self._draft(state) for state in LIVE_STATES}
        body = self.client.delete("/api/invoice/drafts/decided").get_json()
        self.assertEqual({"ok": True, "deleted": 0}, body)
        self.assertEqual(set(live.values()), set(self._statuses()))

    def test_clear_decided_is_idempotent(self):
        self._draft("submitted")
        self._draft("pending")
        first = self.client.delete("/api/invoice/drafts/decided").get_json()
        second = self.client.delete("/api/invoice/drafts/decided").get_json()
        self.assertEqual(1, first["deleted"])
        self.assertEqual(0, second["deleted"])
        self.assertEqual(1, len(self._statuses()), "pending 이 살아있어야 한다")


if __name__ == "__main__":
    unittest.main()
